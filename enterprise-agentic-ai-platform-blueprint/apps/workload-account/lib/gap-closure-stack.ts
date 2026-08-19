/**
 * GapClosureStack — deploys every new v0.4.0 construct in one CFN stack so
 * Phase J's live-AWS verification matrix can exercise every change made in
 * Phases A–I.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md (live-AWS half).
 *
 * Stack is intentionally self-contained — does not depend on any prior
 * Phase-1..10 stack so it can be deployed against a fresh AWS account
 * with only the two CSV credentials. Inputs (gateway URL, inference profile
 * arn, etc.) are passed via CloudFormation parameters with deploy-time
 * placeholder defaults so the stack synthesises clean for unit tests.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, Stack, StackProps } from 'aws-cdk-lib';
import { UserPool, UserPoolClient } from 'aws-cdk-lib/aws-cognito';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { EvaluationGatesConstruct } from '@agenticai/evaluation-gates';
import { OnlineEvaluationConstruct } from '@agenticai/online-evaluation';
import { ConformityAssessmentConstruct } from '@agenticai/eu-ai-act-compliance';
import {
  AgentVersionTableConstruct,
  RollbackStateMachineConstruct,
} from '@agenticai/agent-lifecycle';
import { McpProbeConstruct } from '@agenticai/agent-protocols';
import { InferenceCircuitBreakerConstruct, KillSwitchConstruct } from '@agenticai/agent-resilience';
import { TenantQuotaTableConstruct } from '@agenticai/tenant-quota-guard';
import { CatalogueDriftDetectorConstruct } from '@agenticai/catalogue-drift-detector';
import { ChargebackConstruct, ShowbackConstruct } from '@agenticai/cost-allocation';
import { HumanInTheLoopConstruct } from '@agenticai/hitl';

export interface GapClosureStackProps extends StackProps {
  readonly envName: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly blueprintId: string;
  readonly providerName: string;
  readonly contactEmail: string;
  readonly humanOversightContact: string;
  readonly approverRoleArn: string;
  readonly chargebackEmail: string;
  readonly mcpGatewayUrl: string;
  readonly cognitoUserPoolId: string;
  readonly cognitoUserPoolClientId: string;
  readonly workloadIdentityName: string;
  readonly gatewayTargetId: string;
  readonly inferenceProfileArn: string;
}

export class GapClosureStack extends Stack {
  constructor(scope: Construct, id: string, props: GapClosureStackProps) {
    super(scope, id, props);

    // ---- Phase A: evaluation gates ----
    const evalGates = new EvaluationGatesConstruct(this, 'EvalGates', {
      envName: props.envName,
      bucketSuffix: this.node.tryGetContext('agenticai/aiActBucketSuffix') as string | undefined,
    });

    // ---- Phase B: online evaluation ----
    const online = new OnlineEvaluationConstruct(this, 'OnlineEval', {
      envName: props.envName,
      tenantId: props.tenantId,
      agentId: props.agentId,
      failuresTopic: evalGates.failuresTopic,
    });

    // ---- Phase C: EU AI Act ----
    const aiAct = new ConformityAssessmentConstruct(this, 'AiAct', {
      envName: props.envName,
      tenantId: props.tenantId,
      agentId: props.agentId,
      blueprintId: props.blueprintId,
      providerName: props.providerName,
      contactEmail: props.contactEmail,
      modelIds: ['anthropic.claude-sonnet-4-5-20250929-v1:0', 'anthropic.claude-haiku-4-5-20251001-v1:0'],
      humanOversightContact: props.humanOversightContact,
      platformVersion: 'v0.4.0',
      bucketSuffix: this.node.tryGetContext('agenticai/aiActBucketSuffix') as string | undefined,
    });

    // ---- Phase D: agent lifecycle ----
    const versions = new AgentVersionTableConstruct(this, 'Versions', { envName: props.envName });
    const rollback = new RollbackStateMachineConstruct(this, 'Rollback', {
      envName: props.envName,
      tenantId: props.tenantId,
      agentId: props.agentId,
      versionTable: versions.table,
      failuresTopic: evalGates.failuresTopic,
    });
    // Z7-B: auto-rollback hook — OnlineEval regression alarm flips the alias.
    rollback.wireToRegressionAlarm(online.compositeAlarm);

    // ---- Phase E: MCP probe ----
    // M-A fix: refuse to deploy the probe with the placeholder URL — it would
    // sit in continuous ALARM with no upstream to probe. Operators must
    // supply `agenticai/mcpGatewayUrl` once a real workstream gateway is up.
    if (props.mcpGatewayUrl === 'https://gateway.example.com/a2a') {
      throw new Error(
        "MCP probe gateway URL is the placeholder 'https://gateway.example.com/a2a'. " +
          "Pass `agenticai/mcpGatewayUrl=<real-https-url>` once D03WorkstreamGatewayStack is deployed. " +
          "If you genuinely want to deploy without a gateway, omit the McpProbeConstruct.",
      );
    }
    const probe = new McpProbeConstruct(this, 'McpProbe', {
      envName: props.envName,
      tenantId: props.tenantId,
      agentId: props.agentId,
      gatewayUrl: props.mcpGatewayUrl,
      failuresTopic: evalGates.failuresTopic,
    });

    // ---- Phase F: kill-switch ----
    // Provision a stub Cognito UserPool + Client so the kill-switch state
    // machine has a real target during the live verification. In production
    // this is the AgentCoreIdentityConstruct's UserPool — passed in via
    // cognitoUserPoolId props (kept here for that injection path).
    const stubPool = new UserPool(this, 'KillSwitchStubPool', {
      userPoolName: `agenticai-killswitch-stub-${props.envName}-${props.tenantId}-${props.agentId}`,
      selfSignUpEnabled: false,
      deletionProtection: false,
      passwordPolicy: {
        minLength: 12,
        requireDigits: true,
        requireLowercase: true,
        requireUppercase: true,
        requireSymbols: true,
      },
    });
    NagSuppressions.addResourceSuppressions(
      stubPool,
      [
        { id: 'AwsSolutions-COG2', reason: 'SEC-014: stub pool exists only as a Step Functions live-target. No real users; MFA is a customer opt-in via the production AgentCoreIdentityConstruct path.' },
        { id: 'AwsSolutions-COG8', reason: 'SEC-014: Cognito Plus tier is a per-MAU billing surcharge; the stub pool has 0 users and is exercised only by the kill-switch live test.' },
      ],
      true,
    );
    // H-F fix: explicitly disable OAuth flows + callback URLs so the stub
    // client is locked down at create time. Without this, CDK defaults
    // populate `implicit + code` flows + `https://example.com` callback.
    const stubClient = new UserPoolClient(this, 'KillSwitchStubClient', {
      userPool: stubPool,
      userPoolClientName: `agenticai-killswitch-stub-client-${props.envName}-${props.tenantId}-${props.agentId}`,
      authFlows: { userSrp: true },
      generateSecret: false,
      preventUserExistenceErrors: true,
      disableOAuth: true,
      // H-B fix (CFN drift): kill-switch mutates ExplicitAuthFlows on this
      // client. To keep CDK in sync, pre-set ExplicitAuthFlows to the
      // post-kill-switch state ([USER_SRP_AUTH] only — minimum the L2 will
      // accept). The kill-switch then sets it to []; CDK re-deploys will
      // re-set [USER_SRP_AUTH], not the old default. Drift stays bounded
      // to one attribute and the post-redeploy state is safe (USER_SRP
      // alone, no OAuth, no callbacks). Document in README that
      // re-running kill-switch after a deploy is recommended.
    });
    const effectiveUserPoolId = props.cognitoUserPoolId.startsWith('us-east-1_AAAAAAAAA')
      ? stubPool.userPoolId
      : props.cognitoUserPoolId;
    const effectiveClientId = props.cognitoUserPoolClientId === 'placeholderClientId'
      ? stubClient.userPoolClientId
      : props.cognitoUserPoolClientId;
    const killSwitch = new KillSwitchConstruct(this, 'KillSwitch', {
      envName: props.envName,
      tenantId: props.tenantId,
      agentId: props.agentId,
      cognitoUserPoolId: effectiveUserPoolId,
      cognitoUserPoolClientId: effectiveClientId,
      workloadIdentityName: props.workloadIdentityName,
      gatewayTargetId: props.gatewayTargetId,
      inferenceProfileArn: props.inferenceProfileArn,
      auditTopic: evalGates.failuresTopic,
    });

    // ---- Phase G: chargeback (CSV emit) + showback (QuickSight dataset) ----
    const chargeback = new ChargebackConstruct(this, 'Chargeback', {
      envName: props.envName,
      curAthenaDatabase: 'cur',
      curAthenaTable: 'cost_and_usage',
      chargebackEmailDistribution: [props.chargebackEmail],
    });
    // G-4: ShowbackConstruct. QuickSight requires (a) QS onboarded in the
    // account, (b) the supplied principal to be a real QS user/group,
    // (c) Athena permissions on a CUR table that exists. Gate on an
    // explicit context flag so the gap-closure stack synth-cleanly when
    // the customer hasn't yet onboarded QuickSight. Set
    // `agenticai/showbackEnabled=true` AND
    // `agenticai/showbackReaderPrincipalArn=arn:aws:quicksight:...:user/...`
    // to opt in.
    const showbackEnabled = this.node.tryGetContext('agenticai/showbackEnabled') === 'true' ||
      this.node.tryGetContext('agenticai/showbackEnabled') === true;
    const qsReaderArn = this.node.tryGetContext('agenticai/showbackReaderPrincipalArn') as string | undefined;
    const showback = showbackEnabled
      ? new ShowbackConstruct(this, 'Showback', {
          envName: props.envName,
          curAthenaDatabase: 'cur',
          curAthenaTable: 'cost_and_usage',
          readerPrincipalArn: qsReaderArn,
        })
      : undefined;

    // ---- G-5a: per-tenant quota guard table (was unused in v0.4.0) ----
    const tenantQuota = new TenantQuotaTableConstruct(this, 'TenantQuota', {
      envName: props.envName,
    });

    // ---- Z7-A: inference circuit breaker (real runtime) ----
    const circuit = new InferenceCircuitBreakerConstruct(this, 'InferenceCircuit', {
      envName: props.envName,
      tenantId: props.tenantId,
      agentId: props.agentId,
      failuresTopic: evalGates.failuresTopic,
      tenantQuotaTable: tenantQuota.table,
    });

    // ---- Phase H: HITL ----
    const hitl = new HumanInTheLoopConstruct(this, 'Hitl', {
      envName: props.envName,
      tenantId: props.tenantId,
      agentId: props.agentId,
      approver: {
        approverRoleArn: props.approverRoleArn,
        tenantId: props.tenantId,
        agentId: props.agentId,
        confidenceThreshold: 0.7,
      },
    });

    // Outputs needed by the live verification matrix.
    new CfnOutput(this, 'EvalCorpusBucket', { value: evalGates.corpusBucket.bucketName, description: 'Phase A corpus bucket.' });
    new CfnOutput(this, 'EvalRunHistoryTable', { value: evalGates.runHistoryTable.tableName, description: 'Phase A run-history DDB.' });
    new CfnOutput(this, 'OnlineEvalTable', { value: online.samplesTable.tableName, description: 'Phase B samples DDB.' });
    new CfnOutput(this, 'OnlineEvalLambdaArn', { value: online.watchdog.functionArn, description: 'Phase B watchdog Lambda.' });
    new CfnOutput(this, 'AiActBucket', { value: aiAct.recordKeepingBucket.bucketName, description: 'Phase C COMPLIANCE 7y bucket.' });
    new CfnOutput(this, 'AgentVersionsTable', { value: versions.table.tableName, description: 'Phase D versions DDB.' });
    new CfnOutput(this, 'McpProbeLambdaArn', { value: probe.probe.functionArn, description: 'Phase E MCP probe Lambda.' });
    new CfnOutput(this, 'KillSwitchStateMachineArn', { value: killSwitch.stateMachine.stateMachineArn, description: 'Phase F kill-switch SF.' });
    new CfnOutput(this, 'KillSwitchAuditTable', { value: killSwitch.auditTable.tableName, description: 'Phase F audit DDB.' });
    new CfnOutput(this, 'ChargebackBucket', { value: chargeback.bucket.bucketName, description: 'Phase G chargeback bucket.' });
    new CfnOutput(this, 'ChargebackLambdaArn', { value: chargeback.runner.functionArn, description: 'Phase G chargeback Lambda.' });
    new CfnOutput(this, 'HitlStateMachineArn', { value: hitl.stateMachine.stateMachineArn, description: 'Phase H HITL SF.' });
    new CfnOutput(this, 'HitlEscalationQueueUrl', { value: hitl.escalationQueue.queueUrl, description: 'Phase H escalation SQS.' });
    new CfnOutput(this, 'FailuresTopicArn', { value: evalGates.failuresTopic.topicArn, description: 'Shared SNS failures topic for all phases.' });
    new CfnOutput(this, 'InferenceCircuitBreakerLambdaArn', { value: circuit.fn.functionArn, description: 'Z7-A inference circuit breaker Lambda.' });
    if (showback) {
      new CfnOutput(this, 'ShowbackDataSetId', { value: showback.dataSet.dataSetId!, description: 'G-4 QuickSight showback dataset id.' });
    }

    // ---- G-5d: catalogue drift detector ----
    const drift = new CatalogueDriftDetectorConstruct(this, 'CatalogueDrift', {
      envName: props.envName,
      tenantId: props.tenantId,
      agentId: props.agentId,
      failuresTopic: evalGates.failuresTopic,
      catalogueIds: ['tool-echo', 'tool-ping'],
    });
    new CfnOutput(this, 'CatalogueDriftLambdaArn', { value: drift.fn.functionArn, description: 'G-5d catalogue drift detector Lambda.' });
    new CfnOutput(this, 'TenantQuotaTable', { value: tenantQuota.table.tableName, description: 'G-5a tenant quota DDB.' });

    // Stack-level cdk-nag suppressions for the new v0.4.0 constructs.
    // Each construct already carries inline `addResourceSuppressions` for the
    // controls specific to that construct; the cross-cutting ones below are
    // surfaced once at the stack edge so the live deploy synthesises clean.
    NagSuppressions.addStackSuppressions(
      this,
      [
        { id: 'NIST.800.53.R5-IAMNoInlinePolicy', reason: 'SEC-005: CDK-generated default IAM policies for Step Functions, Lambdas and custom resources use inline statements bound to the role they grant. The construct surface uses `grant*` helpers (resource-scoped) wherever the API supports it.' },
        { id: 'AwsSolutions-IAM4', reason: 'SEC-024: AWSLambdaBasicExecutionRole is the AWS-managed policy required for CW Logs streaming on every CDK Lambda; replacing it with a per-function customer-managed policy duplicates the AWS-maintained baseline without any control gain.' },
        { id: 'AwsSolutions-IAM5', reason: 'SEC-025: Wildcards appear only on AgentCore service actions that do not yet support resource-level conditions (DeleteWorkloadIdentity, UpdateGatewayTarget, athena:Start*, ses:SendEmail). cdk-grant* helpers also generate kms wildcards (kms:ReEncrypt*, kms:GenerateDataKey*) that bind to the construct\'s own CMK only.' },
        { id: 'AwsSolutions-L1', reason: 'SEC-006: Inline Node.js 20 runtimes pin to NODEJS_20_X — the long-term-support runtime as of v0.4.0. Bumped on aws-cdk-lib upgrades.' },
        { id: 'AwsSolutions-SF1', reason: 'SEC-026: Step Functions executions have CloudWatch logging enabled at LogLevel ALL on every state machine in this stack.' },
        { id: 'AwsSolutions-SF2', reason: 'SEC-026: X-Ray tracing is enabled on every state machine in this stack.' },
        { id: 'AwsSolutions-SQS3', reason: 'SEC-027: Per-tenant escalation queues already attach a DLQ via deadLetterQueue; the construct surface enforces this.' },
        { id: 'NIST.800.53.R5-SQSQueueSSEEnabled', reason: 'SEC-026: All HITL queues are KMS-encrypted via QueueEncryption.KMS.' },
        { id: 'NIST.800.53.R5-SQSQueueDLQ', reason: 'SEC-027: HITL queue ships with DLQ; DLQ itself does not need a DLQ.' },
        { id: 'NIST.800.53.R5-SNSEncryptedKMS', reason: 'SEC-026: SNS topics are CMK-encrypted via masterKey.' },
        { id: 'NIST.800.53.R5-CloudWatchLogGroupEncrypted', reason: 'SEC-026: Log groups owned by this stack are CMK-encrypted; CDK auto-generated log groups (e.g. for CFN custom resources) inherit the account default encryption.' },
        { id: 'NIST.800.53.R5-S3BucketLoggingEnabled', reason: 'SEC-001: Eval/AI-Act/chargeback buckets are themselves audit destinations; cross-replication is deferred to v2 DR roadmap.' },
        { id: 'NIST.800.53.R5-S3BucketReplicationEnabled', reason: 'SEC-002: CRR deferred to v2.' },
        { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: CDK custom-resource SDK Lambdas surface failures via stack events; DLQ would duplicate the failure path.' },
        { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: stack Lambdas call AWS control plane via managed endpoints; in-VPC deploy is a v2 enhancement gated on the platform inference VPCE wiring.' },
        { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: cadence-driven Lambdas have bounded concurrency via EventBridge; CFN custom resource Lambdas are provisioning-time only.' },
      ],
      true,
    );
  }
}
