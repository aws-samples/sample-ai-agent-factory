/**
 * EvaluationGatesConstruct — CI/CD evaluation-gate infrastructure.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Partial-1.
 *
 * Emits:
 *   - S3 bucket `agenticai-eval-corpus-<env>-<acct>-<region>` storing
 *     `golden-prompts/<agentId>/<gitSha>.jsonl`,
 *     `manifests/<agentId>/<gitSha>/manifest.json`, and
 *     `scores/<agentId>/<runId>.json`. CMK-encrypted, versioned, TLS-only,
 *     90-day Object-Lock GOVERNANCE on prompts (immutable corpora).
 *   - DDB run-history table `agenticai-eval-runs-<env>` keyed on (agentId,
 *     runId), GSI `by-status`. CMK-encrypted, PITR, RETAIN.
 *   - Judge-runner role `AgenticAI-EvaluationGateRunner-<env>` with
 *     `bedrock:InvokeModel` scoped to the platform-baselines model
 *     allow-list (NOT a wildcard) plus least-priv S3/DDB.
 *   - SNS topic `agenticai-eval-failures-<env>` for gate failures.
 *
 * IAM posture: every resource policy condition pins `aws:SourceAccount` /
 * `aws:SourceArn`, blocking the confused-deputy class of bug. The runner
 * role is the ONLY non-platform-admin principal allowed to write scores.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import {
  AttributeType,
  BillingMode,
  StreamViewType,
  Table,
  TableEncryption,
} from 'aws-cdk-lib/aws-dynamodb';
import {
  AnyPrincipal,
  Effect,
  ManagedPolicy,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import {
  BlockPublicAccess,
  Bucket,
  BucketEncryption,
  ObjectLockMode,
  ObjectLockRetention,
} from 'aws-cdk-lib/aws-s3';
import { Topic } from 'aws-cdk-lib/aws-sns';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { allowedBedrockResources } from '@agenticai/platform-baselines';

import {
  DEFAULT_EVAL_THRESHOLDS,
  validateThresholds,
  type EvalThresholds,
} from './scoring';

export interface EvaluationGatesConstructProps {
  readonly envName: string;
  /** Override defaults (5 existing + 2 new categories). */
  readonly thresholds?: Partial<EvalThresholds>;
  /**
   * Inference-profile backing regions — passed to allowedBedrockResources()
   * so the runner role's `Resource` list spans cross-region inference. Defaults
   * to the stack's region (single-region invocation only).
   */
  readonly inferenceProfileRegions?: readonly string[];
  /** Pipeline role allowed to read corpora + write run records. */
  readonly pipelineRoleArn?: string;
  /**
   * Z7-M: optional unique suffix for the corpus bucket. The bucket has
   * GOVERNANCE Object Lock + an explicit deny on BypassGovernanceRetention,
   * so versions cannot be deleted within 90 days. Re-deploys after a
   * CREATE_FAILED rollback need a fresh bucket name.
   */
  readonly bucketSuffix?: string;
}

export class EvaluationGatesConstruct extends Construct {
  readonly thresholds: EvalThresholds;
  readonly kmsKey: Key;
  readonly corpusBucket: Bucket;
  readonly runHistoryTable: Table;
  readonly runnerRole: Role;
  readonly failuresTopic: Topic;

  constructor(scope: Construct, id: string, props: EvaluationGatesConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);

    this.thresholds = { ...DEFAULT_EVAL_THRESHOLDS, ...(props.thresholds ?? {}) };
    validateThresholds(this.thresholds);

    // ---- CMK ----
    this.kmsKey = new Key(this, 'Key', {
      alias: `alias/agenticai/eval-gates-${props.envName}`,
      description: `CMK for AgentCore evaluation-gates corpus + run history (${props.envName}).`,
      enableKeyRotation: true,
      pendingWindow: Duration.days(30),
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.kmsKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowS3Service',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('s3.amazonaws.com')],
        actions: ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:DescribeKey'],
        resources: ['*'],
        conditions: { StringEquals: { 'aws:SourceAccount': stack.account } },
      }),
    );
    this.kmsKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowDynamoDBService',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('dynamodb.amazonaws.com')],
        actions: ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:DescribeKey'],
        resources: ['*'],
        conditions: { StringEquals: { 'aws:SourceAccount': stack.account } },
      }),
    );
    this.kmsKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowSNSService',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('sns.amazonaws.com')],
        actions: ['kms:Decrypt', 'kms:GenerateDataKey*'],
        resources: ['*'],
        conditions: { StringEquals: { 'aws:SourceAccount': stack.account } },
      }),
    );
    // CRITICAL fix C-A: CloudWatch alarm actions publish to KMS-encrypted SNS
    // topics by calling kms:GenerateDataKey* under the cloudwatch service
    // principal. Without this, every alarm that targets the failures topic
    // silently fails to publish (verified live 2026-05-15 by E2E agent + bug
    // bash). All five OnlineEval child alarms + MCPProbe alarm + composite
    // depend on this grant.
    this.kmsKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowCloudWatchAlarmsService',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('cloudwatch.amazonaws.com')],
        actions: ['kms:Decrypt', 'kms:GenerateDataKey*'],
        resources: ['*'],
        conditions: { StringEquals: { 'aws:SourceAccount': stack.account } },
      }),
    );

    // ---- Corpus bucket ----
    // Object Lock GOVERNANCE 90 days on golden-prompt corpora — immutable so
    // a regression cannot be silently retconned by overwriting the corpus.
    this.corpusBucket = new Bucket(this, 'CorpusBucket', {
      bucketName: `agenticai-eval-corpus-${props.envName}-${stack.account}-${stack.region}${props.bucketSuffix ? `-${props.bucketSuffix}` : ''}`,
      encryption: BucketEncryption.KMS,
      encryptionKey: this.kmsKey,
      bucketKeyEnabled: true,
      enforceSSL: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      objectLockEnabled: true,
      objectLockDefaultRetention: ObjectLockRetention.governance(Duration.days(90)),
      removalPolicy: RemovalPolicy.RETAIN,
    });
    // L-A note: enforceSSL: true on the L2 Bucket already adds an automatic
    // TLS-deny statement; we do NOT add a manual duplicate (was a v0.4.0
    // duplicate-policy finding from security agent + bug-bash).
    // CRIT-A companion: explicitly deny BypassGovernanceRetention so a
    // workload-account principal cannot shorten or override the 90-day
    // GOVERNANCE retention.
    this.corpusBucket.addToResourcePolicy(
      new PolicyStatement({
        sid: 'DenyBypassGovernanceRetention',
        effect: Effect.DENY,
        principals: [new AnyPrincipal()],
        actions: ['s3:BypassGovernanceRetention'],
        resources: [`${this.corpusBucket.bucketArn}/*`],
      }),
    );

    NagSuppressions.addResourceSuppressions(
      this.corpusBucket,
      [
        { id: 'AwsSolutions-S1', reason: 'SEC-001: corpus bucket is the evaluation-evidence audit trail; replication + access logs deferred to v2 DR roadmap.' },
        { id: 'NIST.800.53.R5-S3BucketLoggingEnabled', reason: 'SEC-001: same as AwsSolutions-S1.' },
        { id: 'NIST.800.53.R5-S3BucketReplicationEnabled', reason: 'SEC-002: CRR deferred to v2.' },
      ],
      true,
    );

    // ---- Run-history DDB ----
    this.runHistoryTable = new Table(this, 'RunHistory', {
      tableName: `agenticai-eval-runs-${props.envName}`,
      partitionKey: { name: 'agentId', type: AttributeType.STRING },
      sortKey: { name: 'runId', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      encryption: TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.kmsKey,
      pointInTimeRecovery: true,
      removalPolicy: RemovalPolicy.RETAIN,
      stream: StreamViewType.NEW_AND_OLD_IMAGES,
    });
    this.runHistoryTable.addGlobalSecondaryIndex({
      indexName: 'by-status',
      partitionKey: { name: 'status', type: AttributeType.STRING },
      sortKey: { name: 'startedAt', type: AttributeType.STRING },
    });
    NagSuppressions.addResourceSuppressions(
      this.runHistoryTable,
      [
        {
          id: 'NIST.800.53.R5-DynamoDBInBackupPlan',
          reason:
            'SEC-023: PITR is enabled; AWS Backup plan is a customer opt-in covered in OPERATIONS.md.',
        },
      ],
      true,
    );

    // ---- SNS failures topic ----
    this.failuresTopic = new Topic(this, 'FailuresTopic', {
      topicName: `agenticai-eval-failures-${props.envName}`,
      masterKey: this.kmsKey,
      displayName: `AgenticAI evaluation-gate failures (${props.envName})`,
    });

    // ---- Runner role ----
    this.runnerRole = new Role(this, 'RunnerRole', {
      roleName: `AgenticAI-EvaluationGateRunner-${props.envName}`,
      assumedBy: new ServicePrincipal('codebuild.amazonaws.com'),
      description: 'CodeBuild-assumed role that runs offline + online evaluation suites.',
    });

    // bedrock:InvokeModel scoped to the SSOT allow-list (regions + models).
    const regions = props.inferenceProfileRegions ?? [stack.region];
    const bedrockResources = regions.flatMap((r) => allowedBedrockResources(r, stack.account));
    this.runnerRole.addToPolicy(
      new PolicyStatement({
        sid: 'InvokeJudgeAndAgentModels',
        effect: Effect.ALLOW,
        actions: ['bedrock:InvokeModel', 'bedrock:Converse', 'bedrock:ConverseStream'],
        resources: bedrockResources,
      }),
    );
    this.corpusBucket.grantReadWrite(this.runnerRole);
    this.runHistoryTable.grantReadWriteData(this.runnerRole);
    this.kmsKey.grantEncryptDecrypt(this.runnerRole);
    this.failuresTopic.grantPublish(this.runnerRole);
    this.runnerRole.addManagedPolicy(
      ManagedPolicy.fromAwsManagedPolicyName('CloudWatchLogsFullAccess'),
    );

    NagSuppressions.addResourceSuppressions(
      this.runnerRole,
      [
        { id: 'AwsSolutions-IAM4', reason: 'SEC-024: CloudWatchLogsFullAccess is the AWS-managed policy CodeBuild requires for log streams.' },
        { id: 'AwsSolutions-IAM5', reason: 'SEC-025: bedrock:InvokeModel/Converse resource list is the SSOT allowedBedrockResources(); narrowing further requires per-model split which is the next iteration.' },
      ],
      true,
    );

    if (props.pipelineRoleArn) {
      this.corpusBucket.grantRead(Role.fromRoleArn(this, 'PipelineReader', props.pipelineRoleArn, { mutable: false }));
    }
  }
}
