/**
 * KillSwitchConstruct — instant agent revocation.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Partial-4.
 *
 * Step Functions STANDARD state machine that, in parallel, on a single
 * trigger:
 *   1. Disables the Cognito UserPoolClient (drop explicitAuthFlows).
 *   2. Deletes the AgentCore Identity Workload Identity for the agent.
 *   3. Disables the workstream Gateway target.
 *   4. Updates the Application Inference Profile alias to a deny-all
 *      placeholder.
 *   5. Writes an audit row to the kill-switch DDB and publishes SNS.
 *
 * Trigger surface: SSM Run Document `AgenticAI-Trigger-KillSwitch-<env>-<tenant>-<agent>`
 * — auditable via CloudTrail, callable by tenant operators only.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import {
  AttributeType,
  BillingMode,
  Table,
  TableEncryption,
} from 'aws-cdk-lib/aws-dynamodb';
import { Effect, PolicyStatement, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Code, Function as LambdaFunction, Runtime } from 'aws-cdk-lib/aws-lambda';
import { IKey, Key } from 'aws-cdk-lib/aws-kms';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { ITopic } from 'aws-cdk-lib/aws-sns';
import {
  DefinitionBody,
  LogLevel,
  Parallel,
  StateMachine,
  StateMachineType,
  Succeed,
  TaskInput,
} from 'aws-cdk-lib/aws-stepfunctions';
import { CallAwsService, LambdaInvoke, SnsPublish } from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { CfnDocument } from 'aws-cdk-lib/aws-ssm';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface KillSwitchConstructProps {
  readonly envName: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly cognitoUserPoolId: string;
  readonly cognitoUserPoolClientId: string;
  readonly workloadIdentityName: string;
  readonly gatewayTargetId: string;
  readonly inferenceProfileArn: string;
  readonly auditTopic: ITopic;
  readonly kmsKey?: IKey;
}

export class KillSwitchConstruct extends Construct {
  readonly stateMachine: StateMachine;
  readonly auditTable: Table;
  readonly role: Role;
  readonly logGroup: LogGroup;
  readonly triggerDocument: CfnDocument;
  readonly kmsKey: IKey;

  constructor(scope: Construct, id: string, props: KillSwitchConstructProps) {
    super(scope, id);

    this.kmsKey =
      props.kmsKey ??
      new Key(this, 'Key', {
        alias: `alias/agenticai/kill-switch-${props.envName}-${props.tenantId}-${props.agentId}`,
        description: `Kill-switch CMK (${props.envName}/${props.tenantId}/${props.agentId}).`,
        enableKeyRotation: true,
        pendingWindow: Duration.days(30),
        removalPolicy: RemovalPolicy.RETAIN,
      });

    // ---- Audit DDB ----
    this.auditTable = new Table(this, 'AuditTable', {
      tableName: `agenticai-kill-switch-audit-${props.envName}-${props.tenantId}-${props.agentId}`,
      partitionKey: { name: 'pk', type: AttributeType.STRING },
      sortKey: { name: 'triggeredAt', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      encryption: TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.kmsKey,
      pointInTimeRecovery: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    NagSuppressions.addResourceSuppressions(
      this.auditTable,
      [{ id: 'NIST.800.53.R5-DynamoDBInBackupPlan', reason: 'SEC-023: PITR is enabled.' }],
      true,
    );

    // ---- Step Functions role ----
    this.role = new Role(this, 'Role', {
      roleName: `AgenticAI-KillSwitch-${props.envName}-${props.tenantId}-${props.agentId}`,
      assumedBy: new ServicePrincipal('states.amazonaws.com'),
      description: 'Kill-switch Step Functions role - narrowly scoped to the four target endpoints.',
    });

    // Cognito client mutation (limit explicit auth flows).
    this.role.addToPolicy(
      new PolicyStatement({
        sid: 'CognitoClientLockdown',
        effect: Effect.ALLOW,
        actions: ['cognito-idp:UpdateUserPoolClient', 'cognito-idp:DescribeUserPoolClient'],
        resources: [
          `arn:aws:cognito-idp:*:*:userpool/${props.cognitoUserPoolId}`,
        ],
      }),
    );
    // AgentCore Identity workload identity.
    this.role.addToPolicy(
      new PolicyStatement({
        sid: 'AgentCoreIdentityRevoke',
        effect: Effect.ALLOW,
        // SEC (security review): bedrock-agentcore Delete/GetWorkloadIdentity do
        // NOT support resource-level ARNs today (live-verified) — the API
        // rejects a non-'*' Resource. This is a break-glass kill-switch role
        // assumed only by the kill-switch Step Functions state machine, whose
        // execution is itself gated (SSM Run Document / EventBridge with
        // CloudTrail audit). Compensating control: the role has no other
        // capability and every invocation writes an audit row. Revisit when
        // AgentCore adds resource-level scoping for workload identities.
        actions: [
          'bedrock-agentcore:DeleteWorkloadIdentity',
          'bedrock-agentcore:GetWorkloadIdentity',
        ],
        resources: ['*'],
      }),
    );
    // Gateway target disable.
    this.role.addToPolicy(
      new PolicyStatement({
        sid: 'AgentCoreGatewayDisable',
        effect: Effect.ALLOW,
        actions: ['bedrock-agentcore:UpdateGatewayTarget'],
        resources: [`arn:aws:bedrock-agentcore:*:*:gateway-target/*${props.gatewayTargetId}`],
      }),
    );
    // Inference profile killed-tag flip (replaces the unsupported
    // UpdateApplicationInferenceProfile path).
    this.role.addToPolicy(
      new PolicyStatement({
        sid: 'InferenceProfileTagKilled',
        effect: Effect.ALLOW,
        actions: ['bedrock:TagResource'],
        resources: [props.inferenceProfileArn],
      }),
    );
    this.auditTable.grantWriteData(this.role);
    props.auditTopic.grantPublish(this.role);
    this.kmsKey.grantEncryptDecrypt(this.role);

    // ---- Step Function definition ----
    this.logGroup = new LogGroup(this, 'Logs', {
      logGroupName: `/agenticai/kill-switch/${props.envName}/${props.tenantId}/${props.agentId}`,
      retention: RetentionDays.ONE_YEAR,
    });

    const lockCognito = new CallAwsService(this, 'LockCognitoClient', {
      service: 'cognitoidentityprovider',
      action: 'updateUserPoolClient',
      iamResources: [`arn:aws:cognito-idp:*:*:userpool/${props.cognitoUserPoolId}`],
      parameters: {
        UserPoolId: props.cognitoUserPoolId,
        ClientId: props.cognitoUserPoolClientId,
        ExplicitAuthFlows: [],
      },
      resultPath: '$.cognito',
    });

    // CRIT-C fix: replace the previous `Pass` placeholders with real SDK
    // calls. Step Functions optimised SDK integration recognises the
    // `bedrockagentcorecontrol` (control plane) service prefix — verified
    // 2026-05-15. Each branch fails closed (no Catch) so a kill-switch
    // partial failure shows up loud in the execution history rather than
    // SUCCEEDING with three NO-OP'd revocations.
    const deleteWorkloadIdAlreadyGone = new (require('aws-cdk-lib/aws-stepfunctions').Pass)(this, 'DeleteWorkloadIdentityAlreadyGone', {
      result: { value: { ok: true, reason: 'WorkloadIdentity not found - already revoked or never created' } },
      resultPath: '$.identity',
    });
    const deleteWorkloadId = new CallAwsService(this, 'DeleteWorkloadIdentity', {
      service: 'bedrockagentcorecontrol',
      action: 'deleteWorkloadIdentity',
      iamResources: ['*'],
      parameters: { Name: props.workloadIdentityName },
      resultPath: '$.identity',
    }).addCatch(deleteWorkloadIdAlreadyGone, {
      errors: ['BedrockAgentCoreControl.ResourceNotFoundException'],
      resultPath: '$.identityError',
    });

    // bedrockagentcorecontrol:updateGatewayTarget requires the full
    // TargetConfiguration in its API surface (live-verified 2026-05-15).
    // Step Functions optimised SDK schema-validates that, and we don't have
    // the full config at deploy time. Use a thin Node.js Lambda that fetches
    // the current target config and re-puts it with status DISABLED.
    const disableTargetFn = new LambdaFunction(this, 'DisableGatewayTargetFn', {
      functionName: `agenticai-killswitch-disable-target-${props.envName}-${props.tenantId}-${props.agentId}`,
      runtime: Runtime.NODEJS_20_X,
      handler: 'index.handler',
      timeout: Duration.minutes(2),
      memorySize: 256,
      environment: {
        GATEWAY_TARGET_ID: props.gatewayTargetId,
      },
      description: 'Kill-switch helper: disables the workstream Gateway target via fetch-then-update.',
      code: Code.fromInline(`
const { BedrockAgentCoreControlClient, GetGatewayTargetCommand, UpdateGatewayTargetCommand } = require('@aws-sdk/client-bedrock-agentcore-control');
const c = new BedrockAgentCoreControlClient({});
exports.handler = async () => {
  const id = process.env.GATEWAY_TARGET_ID;
  try {
    const cur = await c.send(new GetGatewayTargetCommand({ TargetId: id }));
    await c.send(new UpdateGatewayTargetCommand({
      TargetId: id,
      Name: cur.Name,
      GatewayIdentifier: cur.GatewayIdentifier,
      TargetConfiguration: cur.TargetConfiguration,
      Status: 'DISABLED',
    }));
    return { ok: true, targetId: id };
  } catch (e) {
    // Only "already gone" is a benign outcome: the desired end state
    // (no enabled gateway target) is already satisfied — e.g. dev with a
    // placeholder id, or the target was previously deleted.
    if (e && e.name === 'ResourceNotFoundException') {
      return { ok: true, targetId: id, note: 'target already gone' };
    }
    // Every other error (throttling, access denied, timeout, 5xx, ...) means
    // the disable may not have taken effect and the target could still be
    // live. Fail closed so the Step Functions execution shows red and ops is
    // alerted, matching the workload-identity / inference-profile branches.
    throw e;
  }
};
`),
    });
    disableTargetFn.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ['bedrock-agentcore:GetGatewayTarget', 'bedrock-agentcore:UpdateGatewayTarget'],
        // SEC (security review): scope to THIS workstream's gateway target ARN
        // (same pattern as the identity-policy statement above), not '*'.
        resources: [`arn:aws:bedrock-agentcore:*:*:gateway-target/*${props.gatewayTargetId}`],
      }),
    );
    const disableGatewayTarget = new LambdaInvoke(this, 'DisableGatewayTarget', {
      lambdaFunction: disableTargetFn,
      resultPath: '$.gateway',
    });

    // bedrock:UpdateApplicationInferenceProfile remains unsupported by the
    // Step Functions optimised SDK as of 2026-05-15. Use a guardrail-flip
    // path: deny-all guardrail attached via tag mutation — the inference
    // profile gets re-tagged with a marker that the Bedrock invocation
    // logging + SCP-02 + IAM identity policy already key off ("killed=true"
    // overrides the allow-list).
    const flipProfileAlreadyGone = new (require('aws-cdk-lib/aws-stepfunctions').Pass)(this, 'TagInferenceProfileAlreadyGone', {
      result: { value: { ok: true, reason: 'Inference Profile not found - already deleted or never created' } },
      resultPath: '$.profile',
    });
    const flipProfile = new CallAwsService(this, 'TagInferenceProfileKilled', {
      service: 'bedrock',
      action: 'tagResource',
      iamResources: [props.inferenceProfileArn],
      parameters: {
        ResourceARN: props.inferenceProfileArn,
        Tags: [
          { Key: 'agenticai/killed', Value: 'true' },
          { Key: 'agenticai/killed-at', 'Value.$': '$$.State.EnteredTime' },
        ],
      },
      resultPath: '$.profile',
    }).addCatch(flipProfileAlreadyGone, {
      errors: ['Bedrock.ResourceNotFoundException'],
      resultPath: '$.profileError',
    });

    const parallelRevoke = new Parallel(this, 'ParallelRevoke', {
      // Discard branch results; the audit step records the trigger metadata
      // independently, and we don't want the array shape to collide with
      // downstream `resultPath`.
      resultPath: '$.parallelResults',
    })
      .branch(lockCognito)
      .branch(deleteWorkloadId)
      .branch(disableGatewayTarget)
      .branch(flipProfile);

    // M-I fix: persist `$.reason` so audit DDB rows record the operator's
    // stated cause. The SSM trigger document already accepts a Reason
    // parameter and threads it into the SF input.
    const writeAudit = new CallAwsService(this, 'WriteAuditRecord', {
      service: 'dynamodb',
      action: 'putItem',
      iamResources: [this.auditTable.tableArn],
      parameters: {
        TableName: this.auditTable.tableName,
        Item: {
          pk: { S: `${props.tenantId}#${props.agentId}` },
          triggeredAt: { 'S.$': '$$.State.EnteredTime' },
          executionArn: { 'S.$': '$$.Execution.Id' },
          eventType: { S: 'KILL_SWITCH' },
          // States.Format coerces missing/non-string `reason` to "<no-reason>".
          reason: { 'S.$': "States.Format('{}', $.reason)" },
        },
      },
      resultPath: '$.audit',
    });

    const notify = new SnsPublish(this, 'NotifyOps', {
      topic: props.auditTopic,
      subject: `AgenticAI kill-switch fired (${props.envName} ${props.tenantId}/${props.agentId})`,
      message: TaskInput.fromText(
        `Kill-switch fired for ${props.envName} ${props.tenantId}/${props.agentId}. Cognito client, workload identity, gateway target, inference profile all revoked.`,
      ),
    });

    const definition = parallelRevoke.next(writeAudit).next(notify).next(new Succeed(this, 'Done'));

    this.stateMachine = new StateMachine(this, 'StateMachine', {
      stateMachineName: `AgenticAI-KillSwitch-${props.envName}-${props.tenantId}-${props.agentId}`,
      stateMachineType: StateMachineType.STANDARD,
      definitionBody: DefinitionBody.fromChainable(definition),
      role: this.role,
      timeout: Duration.minutes(15),
      logs: { destination: this.logGroup, level: LogLevel.ALL },
      tracingEnabled: true,
    });

    NagSuppressions.addResourceSuppressions(
      this.role,
      [
        { id: 'AwsSolutions-IAM5', reason: 'SEC-025: bedrock-agentcore:Delete/Get WorkloadIdentity does not yet support resource-level conditions; ARN-pattern scoping applied where the API supports it.' },
      ],
      true,
    );

    // ---- SSM trigger document ----
    this.triggerDocument = new CfnDocument(this, 'TriggerDocument', {
      name: `AgenticAI-Trigger-KillSwitch-${props.envName}-${props.tenantId}-${props.agentId}`,
      documentType: 'Automation',
      documentFormat: 'JSON',
      updateMethod: 'NewVersion',
      content: {
        schemaVersion: '0.3',
        description: `Trigger AgenticAI kill-switch for ${props.tenantId}/${props.agentId} (${props.envName}).`,
        assumeRole: '{{ AutomationAssumeRole }}',
        parameters: {
          AutomationAssumeRole: {
            type: 'String',
            description: 'IAM role assumed by Systems Manager Automation to invoke the state machine.',
          },
          Reason: {
            type: 'String',
            description: 'Human-readable reason. Logged into the audit DDB.',
          },
        },
        mainSteps: [
          {
            name: 'StartKillSwitch',
            action: 'aws:executeAwsApi',
            inputs: {
              Service: 'stepfunctions',
              Api: 'StartExecution',
              stateMachineArn: this.stateMachine.stateMachineArn,
              input: '{"reason": "{{ Reason }}"}',
            },
          },
        ],
      },
    });
  }
}
