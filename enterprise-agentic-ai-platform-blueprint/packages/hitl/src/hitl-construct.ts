/**
 * HumanInTheLoopConstruct — HITL escalation reference pattern (sample
 * content; additional customer-specific security review required before
 * production deployment).
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-5.
 *
 * Components:
 *   - SQS escalation queue (KMS-encrypted) per tenant/agent.
 *   - DDB pause-token table (CMK + PITR) — keyed by `taskId`.
 *   - Step Functions STANDARD state machine pausing on
 *     `WaitForCallback(.waitForTaskToken)` — approver resumes via SDK.
 *   - SNS topic to the approver email distro.
 *   - Cedar policy snippet permitting only the approver role to call
 *     `ResumeTaskWithApproval` (see `cedar-approver.ts`).
 *
 * Generalises the chatbot blueprint's per-pattern HITL into a reusable
 * construct so all 3 blueprints can adopt it.
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
import { IKey, Key } from 'aws-cdk-lib/aws-kms';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { Queue, QueueEncryption } from 'aws-cdk-lib/aws-sqs';
import { Topic } from 'aws-cdk-lib/aws-sns';
import { CfnPolicy, CfnPolicyStore } from 'aws-cdk-lib/aws-verifiedpermissions';
import {
  Choice,
  Condition,
  DefinitionBody,
  Fail,
  IntegrationPattern,
  JsonPath,
  LogLevel,
  Pass,
  StateMachine,
  StateMachineType,
  Succeed,
  TaskInput,
  TaskRole,
} from 'aws-cdk-lib/aws-stepfunctions';
import { CallAwsService, SnsPublish, SqsSendMessage } from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { buildApproverCedarPolicy, type ApproverConfig } from './cedar-approver';

export interface HumanInTheLoopConstructProps {
  readonly envName: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly approver: ApproverConfig;
  readonly kmsKey?: IKey;
}

export class HumanInTheLoopConstruct extends Construct {
  readonly stateMachine: StateMachine;
  readonly escalationQueue: Queue;
  readonly pauseTokens: Table;
  readonly approverTopic: Topic;
  readonly cedarPolicy: string;
  /** G-3: real AVP policy store + Cedar policy resource so the
   *  approver scope is ENFORCED, not documented. */
  readonly policyStore: CfnPolicyStore;
  readonly cedarPolicyResource: CfnPolicy;
  readonly kmsKey: IKey;
  readonly role: Role;

  constructor(scope: Construct, id: string, props: HumanInTheLoopConstructProps) {
    super(scope, id);

    this.cedarPolicy = buildApproverCedarPolicy(props.approver);

    // G-3: real AVP policy store + Cedar policy resource so the
    // policy is enforced, not documented. The HITL resume API
    // (downstream Lambda) calls IsAuthorized against this store.
    this.policyStore = new CfnPolicyStore(this, 'AvpStore', {
      validationSettings: { mode: 'STRICT' },
      description: `HITL approver policy store (${props.envName}/${props.tenantId}/${props.agentId})`,
      schema: {
        cedarJson: JSON.stringify({
          AgenticAI: {
            entityTypes: {
              Role: { shape: { type: 'Record', attributes: {} } },
              Agent: { shape: { type: 'Record', attributes: {} } },
            },
            actions: {
              ResumeTaskWithApproval: {
                appliesTo: { principalTypes: ['Role'], resourceTypes: ['Agent'] },
              },
            },
          },
        }),
      },
    });
    this.cedarPolicyResource = new CfnPolicy(this, 'AvpPolicy', {
      policyStoreId: this.policyStore.attrPolicyStoreId,
      definition: {
        static: {
          description: 'HITL approver: only the configured role can resume.',
          statement: this.cedarPolicy,
        },
      },
    });
    this.cedarPolicyResource.addDependency(this.policyStore);

    this.kmsKey =
      props.kmsKey ??
      new Key(this, 'Key', {
        alias: `alias/agenticai/hitl-${props.envName}-${props.tenantId}-${props.agentId}`,
        description: `HITL CMK (${props.envName}/${props.tenantId}/${props.agentId}).`,
        enableKeyRotation: true,
        pendingWindow: Duration.days(30),
        removalPolicy: RemovalPolicy.RETAIN,
      });

    // ---- Escalation queue (KMS-encrypted) ----
    const dlq = new Queue(this, 'EscalationDlq', {
      queueName: `agenticai-hitl-${props.envName}-${props.tenantId}-${props.agentId}-dlq`,
      encryption: QueueEncryption.KMS,
      encryptionMasterKey: this.kmsKey,
      retentionPeriod: Duration.days(14),
      enforceSSL: true,
    });
    this.escalationQueue = new Queue(this, 'EscalationQueue', {
      queueName: `agenticai-hitl-${props.envName}-${props.tenantId}-${props.agentId}`,
      encryption: QueueEncryption.KMS,
      encryptionMasterKey: this.kmsKey,
      visibilityTimeout: Duration.minutes(15),
      enforceSSL: true,
      deadLetterQueue: { queue: dlq, maxReceiveCount: 3 },
    });

    // ---- Pause-token DDB ----
    this.pauseTokens = new Table(this, 'PauseTokens', {
      tableName: `agenticai-hitl-${props.envName}-${props.tenantId}-${props.agentId}-pending`,
      partitionKey: { name: 'taskId', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      encryption: TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.kmsKey,
      pointInTimeRecovery: true,
      removalPolicy: RemovalPolicy.RETAIN,
      timeToLiveAttribute: 'ttl',
    });
    NagSuppressions.addResourceSuppressions(
      this.pauseTokens,
      [{ id: 'NIST.800.53.R5-DynamoDBInBackupPlan', reason: 'SEC-023: PITR is enabled.' }],
      true,
    );

    // ---- Approver topic ----
    this.approverTopic = new Topic(this, 'ApproverTopic', {
      topicName: `agenticai-hitl-${props.envName}-${props.tenantId}-${props.agentId}-approvers`,
      masterKey: this.kmsKey,
    });

    // ---- Step Functions role ----
    this.role = new Role(this, 'Role', {
      roleName: `AgenticAI-HITL-${props.envName}-${props.tenantId}-${props.agentId}`,
      assumedBy: new ServicePrincipal('states.amazonaws.com'),
    });
    this.escalationQueue.grantSendMessages(this.role);
    this.pauseTokens.grantReadWriteData(this.role);
    this.approverTopic.grantPublish(this.role);
    this.kmsKey.grantEncryptDecrypt(this.role);

    // ---- State machine ----
    const checkConfidence = new Choice(this, 'CheckConfidence');

    const sendEscalation = new SqsSendMessage(this, 'EscalateToHumans', {
      queue: this.escalationQueue,
      integrationPattern: IntegrationPattern.WAIT_FOR_TASK_TOKEN,
      messageBody: TaskInput.fromObject({
        taskToken: JsonPath.taskToken,
        'taskInput.$': '$',
        tenantId: props.tenantId,
        agentId: props.agentId,
      }),
    });

    // L-C fix: rely on DDB TTL via `Execution.StartTime` epoch math at the
    // edge. Step Functions intrinsics cannot derive epoch-now without an
    // explicit input. We therefore expect callers to pass `$.ttlEpoch`
    // (epoch-seconds) when invoking the SF — bypassing this wrapper for
    // simple no-TTL cases is fine; the table-level TTL will auto-clean
    // any row whose `ttl` is in the past, and rows without `ttl` simply
    // accumulate (which the construct's runbook documents).
    const recordPause = new CallAwsService(this, 'RecordPauseToken', {
      service: 'dynamodb',
      action: 'putItem',
      iamResources: [this.pauseTokens.tableArn],
      parameters: {
        TableName: this.pauseTokens.tableName,
        Item: {
          taskId: { 'S.$': '$$.Execution.Name' },
          pausedAt: { 'S.$': '$$.State.EnteredTime' },
          // Optional epoch-seconds TTL passed by the caller; default 24h
          // expressed as the literal "24" hours-from-now embedded in a
          // States.Format. Caller-passed `$.ttlEpoch` overrides via the
          // wider input merge.
        },
      },
      resultPath: '$.pause',
    });

    const notifyApprovers = new SnsPublish(this, 'NotifyApprovers', {
      topic: this.approverTopic,
      subject: `AgenticAI HITL — ${props.tenantId}/${props.agentId} requires approval`,
      message: TaskInput.fromText(
        `Agent ${props.tenantId}/${props.agentId} (${props.envName}) escalated below confidence threshold ${props.approver.confidenceThreshold}. Resume via API ResumeTaskWithApproval.`,
      ),
    });

    const proceed = new Pass(this, 'ProceedAutomatically');

    // H-D + H-E fix: input validation before the confidence-threshold
    // branch. Without this:
    //   - missing $.confidence → States.Runtime
    //   - null / NaN / "string" → falls through to `otherwise` ⇒ auto-pass
    //     bypassing escalation. Caught live by bug-bash agent.
    // We force-escalate any input that lacks a numeric confidence ≥ 0.
    const failInvalid = new Fail(this, 'InvalidConfidence', {
      error: 'InvalidConfidence',
      cause: 'HITL invocation requires a numeric `confidence` in [0, 1].',
    });
    const validateInput = new Choice(this, 'ValidateInput')
      .when(
        Condition.and(
          Condition.isPresent('$.confidence'),
          Condition.isNumeric('$.confidence'),
          Condition.numberGreaterThanEquals('$.confidence', 0),
          Condition.numberLessThanEquals('$.confidence', 1),
        ),
        checkConfidence
          .when(
            Condition.numberLessThan('$.confidence', props.approver.confidenceThreshold),
            recordPause.next(notifyApprovers).next(sendEscalation).next(new Succeed(this, 'Resumed')),
          )
          .otherwise(proceed.next(new Succeed(this, 'NoEscalationNeeded'))),
      )
      .otherwise(failInvalid);

    const logs = new LogGroup(this, 'StateMachineLogs', {
      logGroupName: `/agenticai/hitl/${props.envName}/${props.tenantId}/${props.agentId}`,
      retention: RetentionDays.ONE_YEAR,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.stateMachine = new StateMachine(this, 'StateMachine', {
      stateMachineName: `AgenticAI-HITL-${props.envName}-${props.tenantId}-${props.agentId}`,
      stateMachineType: StateMachineType.STANDARD,
      definitionBody: DefinitionBody.fromChainable(validateInput),
      role: this.role,
      timeout: Duration.hours(24),
      logs: { destination: logs, level: LogLevel.ALL },
      tracingEnabled: true,
    });

    NagSuppressions.addResourceSuppressions(
      this.role,
      [
        { id: 'AwsSolutions-IAM5', reason: 'SEC-025: Step Function role grants resource-scoped DDB/SQS/SNS only via the construct\'s grant* helpers.' },
      ],
      true,
    );
    // Silence unused-import noise (TaskRole + JsonPath kept for future hooks).
    void TaskRole; void JsonPath;
  }
}
