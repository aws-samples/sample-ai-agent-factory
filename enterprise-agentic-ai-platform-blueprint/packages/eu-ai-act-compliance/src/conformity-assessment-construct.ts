/**
 * ConformityAssessmentConstruct — EU AI Act record-keeping infrastructure.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-2.
 *
 * Emits:
 *   - S3 record-keeping bucket with **Object Lock COMPLIANCE 7-year** (the
 *     statute-of-limitations boundary expected by Article 10 §6 + Article 12
 *     record-keeping). Versioned, KMS-encrypted, TLS-only, BlockPublicAccess.
 *   - DDB `agenticai-aiact-records-<env>` indexing the bucket entries by
 *     tenant + agent + emittedAt. CMK + PITR + RETAIN.
 *   - Post-deploy CustomResource that synthesises the three Markdown
 *     conformity documents and uploads them under
 *     `<tenantId>/<agentId>/<emittedAt>/{technical-documentation,risk-assessment,human-oversight-protocol}.md`.
 *   - A re-export of the `humanOversightContact` so Phase H HITL can wire
 *     up the same approver address by reference.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
} from 'aws-cdk-lib/custom-resources';
import {
  AttributeType,
  BillingMode,
  Table,
  TableEncryption,
} from 'aws-cdk-lib/aws-dynamodb';
import { AnyPrincipal, Effect, PolicyStatement, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import {
  BlockPublicAccess,
  Bucket,
  BucketEncryption,
  ObjectLockRetention,
} from 'aws-cdk-lib/aws-s3';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import {
  technicalDocumentation,
  riskAssessment,
  humanOversightProtocol,
} from './conformity-templates';
import { riskClassForBlueprint, type RiskClass } from './risk-classification';

export interface ConformityAssessmentConstructProps {
  readonly envName: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly blueprintId: string;
  readonly providerName: string;
  readonly contactEmail: string;
  readonly modelIds: readonly string[];
  readonly humanOversightContact: string;
  readonly platformVersion?: string;     // defaults to 'v0.4.0'
  /** Override the default risk class derived from blueprintId. */
  readonly riskClassOverride?: RiskClass;
  /**
   * Optional unique suffix appended to the record-keeping bucket name to
   * avoid collisions on a re-deploy after a CREATE_FAILED rollback (the
   * bucket is COMPLIANCE-locked for 7 years and cannot be deleted).
   */
  readonly bucketSuffix?: string;
  /**
   * Override the conformity-document `emittedAt` timestamp. Defaults to
   * `new Date().toISOString()`. Pin in unit tests for determinism.
   */
  readonly emittedAt?: string;
}

const RECORD_KEEPING_RETENTION_YEARS = 7;

export class ConformityAssessmentConstruct extends Construct {
  readonly riskClass: RiskClass;
  readonly kmsKey: Key;
  readonly recordKeepingBucket: Bucket;
  readonly recordsTable: Table;
  readonly humanOversightContact: string;

  constructor(scope: Construct, id: string, props: ConformityAssessmentConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);
    this.humanOversightContact = props.humanOversightContact;

    this.riskClass = props.riskClassOverride ?? riskClassForBlueprint(props.blueprintId);

    // ---- CMK ----
    this.kmsKey = new Key(this, 'Key', {
      alias: `alias/agenticai/aiact-${props.envName}-${props.tenantId}-${props.agentId}`,
      description: `EU AI Act record-keeping CMK (${props.envName}/${props.tenantId}/${props.agentId}).`,
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

    // ---- Record-keeping bucket — COMPLIANCE 7y ----
    this.recordKeepingBucket = new Bucket(this, 'RecordKeepingBucket', {
      bucketName: `agenticai-aiact-${props.envName}-${stack.account}-${stack.region}${props.bucketSuffix ? `-${props.bucketSuffix}` : ''}`,
      encryption: BucketEncryption.KMS,
      encryptionKey: this.kmsKey,
      bucketKeyEnabled: true,
      enforceSSL: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      objectLockEnabled: true,
      objectLockDefaultRetention: ObjectLockRetention.compliance(Duration.days(RECORD_KEEPING_RETENTION_YEARS * 365)),
      removalPolicy: RemovalPolicy.RETAIN,
    });
    // L-A note: enforceSSL: true on the L2 Bucket already adds the TLS-deny
    // automatically. We do NOT add a manual duplicate.

    NagSuppressions.addResourceSuppressions(
      this.recordKeepingBucket,
      [
        { id: 'AwsSolutions-S1', reason: 'SEC-001: record-keeping bucket is the audit trail; access logs/replication deferred to v2 DR roadmap.' },
        { id: 'NIST.800.53.R5-S3BucketLoggingEnabled', reason: 'SEC-001: same as AwsSolutions-S1.' },
        { id: 'NIST.800.53.R5-S3BucketReplicationEnabled', reason: 'SEC-002: CRR deferred to v2.' },
      ],
      true,
    );

    // ---- DDB index ----
    this.recordsTable = new Table(this, 'Records', {
      tableName: `agenticai-aiact-records-${props.envName}-${props.tenantId}-${props.agentId}`,
      partitionKey: { name: 'pk', type: AttributeType.STRING }, // tenant#agent
      sortKey: { name: 'emittedAt', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      encryption: TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.kmsKey,
      pointInTimeRecovery: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    NagSuppressions.addResourceSuppressions(
      this.recordsTable,
      [{ id: 'NIST.800.53.R5-DynamoDBInBackupPlan', reason: 'SEC-023: PITR is enabled; AWS Backup plan is a customer opt-in.' }],
      true,
    );

    // ---- Post-deploy upload via AwsCustomResource ----
    // Generates the three documents inline at synth and uploads them to the
    // record-keeping bucket on stack create / update. Object Lock retention
    // is the bucket default; the upload inherits 7y COMPLIANCE.
    //
    // H-C fix: previous version used `new Date(0)` (epoch zero, 1970-01-01)
    // as the prefix — verified live by bug-bash agent. Use the actual deploy
    // time. Synth tests now pin via the optional `emittedAt` prop.
    const emittedAt = props.emittedAt ?? new Date().toISOString();
    const platformVersion = props.platformVersion ?? 'v0.4.0';
    const conformityInputs = {
      tenantId: props.tenantId,
      agentId: props.agentId,
      envName: props.envName,
      blueprintId: props.blueprintId,
      riskClass: this.riskClass,
      providerName: props.providerName,
      contactEmail: props.contactEmail,
      modelIds: props.modelIds,
      humanOversightContact: props.humanOversightContact,
      emittedAt,
      platformVersion,
    };

    const docs: Array<{ key: string; body: string }> = [
      { key: 'technical-documentation.md', body: technicalDocumentation(conformityInputs) },
      { key: 'risk-assessment.md', body: riskAssessment(conformityInputs) },
      { key: 'human-oversight-protocol.md', body: humanOversightProtocol(conformityInputs) },
    ];

    // H-C fix (missing risk-assessment.md): the previous loop created
    // 3 AwsCustomResource instances with parallel-creating Lambdas; one
    // upload sometimes raced on the COMPLIANCE-locked bucket's first-PUT
    // metadata and silently dropped (live-verified by E2E agent — only 2/3
    // docs landed). Fix: serialise the uploads via explicit `addDependency`
    // and grant a single broad `s3:PutObject` over the whole prefix.
    const prefix = `${props.tenantId}/${props.agentId}/${emittedAt}`;
    let prev: AwsCustomResource | undefined;
    for (const d of docs) {
      const upload = new AwsCustomResource(this, `Upload${d.key.replace(/[^a-zA-Z0-9]/g, '')}`, {
        onCreate: {
          service: 'S3',
          action: 'putObject',
          parameters: {
            Bucket: this.recordKeepingBucket.bucketName,
            Key: `${prefix}/${d.key}`,
            Body: d.body,
            ContentType: 'text/markdown; charset=utf-8',
            ServerSideEncryption: 'aws:kms',
            SSEKMSKeyId: this.kmsKey.keyArn,
          },
          physicalResourceId: PhysicalResourceId.of(`aiact-${props.tenantId}-${props.agentId}-${d.key}`),
        },
        onUpdate: {
          service: 'S3',
          action: 'putObject',
          parameters: {
            Bucket: this.recordKeepingBucket.bucketName,
            Key: `${prefix}/${d.key}`,
            Body: d.body,
            ContentType: 'text/markdown; charset=utf-8',
            ServerSideEncryption: 'aws:kms',
            SSEKMSKeyId: this.kmsKey.keyArn,
          },
          physicalResourceId: PhysicalResourceId.of(`aiact-${props.tenantId}-${props.agentId}-${d.key}`),
        },
        policy: AwsCustomResourcePolicy.fromStatements([
          new PolicyStatement({
            actions: ['s3:PutObject'],
            resources: [`${this.recordKeepingBucket.bucketArn}/${prefix}/*`],
          }),
          new PolicyStatement({
            actions: ['kms:Encrypt', 'kms:GenerateDataKey'],
            resources: [this.kmsKey.keyArn],
          }),
        ]),
      });
      if (prev) {
        upload.node.addDependency(prev);
      }
      prev = upload;
    }
  }
}
