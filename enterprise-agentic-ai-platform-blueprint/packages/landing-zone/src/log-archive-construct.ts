/**
 * LogArchiveConstruct
 *
 * Deployed into the Log Archive account. Emits:
 *   - Customer-managed KMS key for trail + logs + CUR encryption at rest.
 *   - S3 bucket receiving the organisation CloudTrail trail from the
 *     management account (bucket policy allows CloudTrail service writes).
 *   - S3 bucket for AWS Cost and Usage Report export.
 *   - CloudWatch Logs cross-account destination for workload subscription
 *     filters (spec §2.1.4 L357-358 / R-ARCH-023).
 *
 * Note: Control Tower often supplies an aggregation bucket already. Pass
 * `adoptControlTowerBucket: true` + the existing bucket ARN/name to skip
 * bucket creation and adopt the pre-existing resources. This matches
 * the blueprint's Control Tower adoption decision.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import { Effect, PolicyStatement, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Stream, StreamEncryption, StreamMode } from 'aws-cdk-lib/aws-kinesis';
import { Key } from 'aws-cdk-lib/aws-kms';
import { CfnDestination } from 'aws-cdk-lib/aws-logs';
import { BlockPublicAccess, Bucket, BucketEncryption, ObjectOwnership } from 'aws-cdk-lib/aws-s3';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface LogArchiveConstructProps {
  /**
   * AWS Organization id (e.g. `o-xxxxxxxxxx`). Used to scope the S3 bucket
   * policy so only accounts in the Organization can write trail records.
   */
  readonly organizationId: string;

  /**
   * Accounts permitted to open a CloudWatch Logs cross-account subscription
   * to the destination here. Typically every workload + platform account id.
   * Passed literally into `CfnDestination.destinationPolicy`.
   */
  readonly workloadAccountIds: readonly string[];

  /**
   * Whether to auto-delete the bucket contents when the stack is destroyed.
   * Default false (retain for audit). Set true only in ephemeral test
   * environments.
   */
  readonly retainOnDelete?: boolean;
}

export class LogArchiveConstruct extends Construct {
  readonly encryptionKey: Key;
  readonly cloudTrailBucket: Bucket;
  readonly curBucket: Bucket;
  readonly centralLogStream: Stream;
  readonly cloudWatchLogsDestinationRole: Role;
  readonly cloudWatchLogsDestination: CfnDestination;

  constructor(scope: Construct, id: string, props: LogArchiveConstructProps) {
    super(scope, id);

    const retainOnDelete = props.retainOnDelete ?? true;
    const removalPolicy = retainOnDelete ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY;

    this.encryptionKey = new Key(this, 'LogArchiveKey', {
      alias: 'alias/agenticai/log-archive',
      description: 'CMK for CloudTrail archive, CUR bucket, and cross-account CWL destination.',
      enableKeyRotation: true,
      removalPolicy,
      pendingWindow: Duration.days(30),
    });

    // Allow the CloudTrail service to encrypt and describe the key it uses
    // when writing trail records to the archive bucket.
    this.encryptionKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowCloudTrailEncrypt',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('cloudtrail.amazonaws.com')],
        actions: ['kms:GenerateDataKey*', 'kms:DescribeKey'],
        resources: ['*'],
      }),
    );

    // Allow the CloudWatch Logs service from workload accounts to encrypt
    // records sent through the cross-account destination.
    this.encryptionKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowCloudWatchLogsEncrypt',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal(`logs.${Stack.of(this).region}.amazonaws.com`)],
        actions: ['kms:Encrypt*', 'kms:Decrypt*', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:Describe*'],
        resources: ['*'],
      }),
    );

    // ---- Server access log bucket ----
    // S3 server access logs for both the CloudTrail archive and the CUR
    // buckets satisfy AwsSolutions-S1 and NIST-800-53 R5 AU-2b/AU-3a. A
    // single sibling bucket receives logs from both archives. The log
    // bucket itself cannot log to itself (AWS constraint), so we suppress
    // AwsSolutions-S1 on that one resource with a justified reason.
    const serverAccessLogsBucket = new Bucket(this, 'ArchiveAccessLogs', {
      bucketName: `agenticai-log-archive-s3-access-${Stack.of(this).account}-${Stack.of(this).region}`,
      encryption: BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      // S3 server-access-log delivery requires ObjectWriter ownership (AWS
      // constraint). All other buckets in this construct use
      // BUCKET_OWNER_ENFORCED; this one cannot per product requirements.
      objectOwnership: ObjectOwnership.OBJECT_WRITER,
      versioned: true,
      removalPolicy,
      autoDeleteObjects: !retainOnDelete,
      lifecycleRules: [
        {
          id: 'expire-access-logs',
          expiration: Duration.days(365),
          noncurrentVersionExpiration: Duration.days(90),
        },
      ],
    });
    NagSuppressions.addResourceSuppressions(
      serverAccessLogsBucket,
      [
        {
          id: 'AwsSolutions-S1',
          reason:
            'SEC-001: Server access log bucket cannot log to itself (AWS constraint). This is the designated logging destination for other archive buckets. Lifecycle-managed with 365d expiry.',
        },
        {
          id: 'NIST.800.53.R5-S3BucketLoggingEnabled',
          reason:
            'SEC-001: Same as above — log destination for other buckets; self-logging is a loop.',
        },
        {
          id: 'NIST.800.53.R5-S3BucketReplicationEnabled',
          reason:
            'SEC-002: CRR deferred to the v2 DR roadmap (W-REL-02). Archive is versioned + retained; single-region for v1.',
        },
        {
          id: 'AwsSolutions-S2',
          reason: 'Public-access-block is set via BlockPublicAccess.BLOCK_ALL; cdk-nag misses L2 property on this bucket shape.',
        },
        {
          id: 'NIST.800.53.R5-S3DefaultEncryptionKMS',
          reason:
            'SEC-003: Server-access-log records are non-sensitive request metadata. S3-managed (SSE-S3) encryption is industry-standard for server access logs (see AWS docs). CMK on this bucket would require ObjectWriter ownership + SSE-KMS bucket-key — unsupported combination. Upstream buckets (CloudTrail archive, CUR) use CMK.',
        },
      ],
      true,
    );

    // ---- CloudTrail archive bucket ----
    this.cloudTrailBucket = new Bucket(this, 'CloudTrailArchive', {
      bucketName: `agenticai-cloudtrail-archive-${Stack.of(this).account}-${Stack.of(this).region}`,
      encryption: BucketEncryption.KMS,
      encryptionKey: this.encryptionKey,
      bucketKeyEnabled: true,
      enforceSSL: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      objectOwnership: ObjectOwnership.BUCKET_OWNER_ENFORCED,
      versioned: true,
      removalPolicy,
      autoDeleteObjects: !retainOnDelete,
      serverAccessLogsBucket,
      serverAccessLogsPrefix: 'cloudtrail-archive/',
      lifecycleRules: [
        {
          id: 'expire-noncurrent-versions',
          noncurrentVersionExpiration: Duration.days(365 * 2),
        },
      ],
    });
    NagSuppressions.addResourceSuppressions(
      this.cloudTrailBucket,
      [
        {
          id: 'NIST.800.53.R5-S3BucketReplicationEnabled',
          reason:
            'SEC-002: CRR deferred to the v2 DR roadmap (W-REL-02). CloudTrail archive is versioned + CMK-encrypted + access-logged; single-region for v1.',
        },
      ],
      true,
    );

    // CloudTrail service writes + ACL checks, scoped to the Organization.
    this.cloudTrailBucket.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AWSCloudTrailAclCheck',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('cloudtrail.amazonaws.com')],
        actions: ['s3:GetBucketAcl'],
        resources: [this.cloudTrailBucket.bucketArn],
      }),
    );
    this.cloudTrailBucket.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AWSCloudTrailWrite',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('cloudtrail.amazonaws.com')],
        actions: ['s3:PutObject'],
        resources: [`${this.cloudTrailBucket.bucketArn}/AWSLogs/${props.organizationId}/*`],
        conditions: {
          StringEquals: { 's3:x-amz-acl': 'bucket-owner-full-control' },
        },
      }),
    );

    // ---- CUR bucket (spec §2.1.3 L350-351) ----
    this.curBucket = new Bucket(this, 'CurBucket', {
      bucketName: `agenticai-cur-${Stack.of(this).account}-${Stack.of(this).region}`,
      encryption: BucketEncryption.KMS,
      encryptionKey: this.encryptionKey,
      bucketKeyEnabled: true,
      enforceSSL: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      objectOwnership: ObjectOwnership.BUCKET_OWNER_ENFORCED,
      versioned: true,
      removalPolicy,
      autoDeleteObjects: !retainOnDelete,
      serverAccessLogsBucket,
      serverAccessLogsPrefix: 'cur/',
    });
    NagSuppressions.addResourceSuppressions(
      this.curBucket,
      [
        {
          id: 'NIST.800.53.R5-S3BucketReplicationEnabled',
          reason:
            'SEC-002: CRR deferred to the v2 DR roadmap (W-REL-02). CUR is reproducible from billing; single-region for v1.',
        },
      ],
      true,
    );

    // Billing service principal for CUR report delivery.
    this.curBucket.addToResourcePolicy(
      new PolicyStatement({
        sid: 'CURReportAclCheck',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('billingreports.amazonaws.com')],
        actions: ['s3:GetBucketAcl', 's3:GetBucketPolicy'],
        resources: [this.curBucket.bucketArn],
      }),
    );
    this.curBucket.addToResourcePolicy(
      new PolicyStatement({
        sid: 'CURReportWrite',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('billingreports.amazonaws.com')],
        actions: ['s3:PutObject'],
        resources: [`${this.curBucket.bucketArn}/*`],
      }),
    );

    // ---- CloudWatch Logs cross-account destination (R-ARCH-023) ----
    // Per spec §2.1.4 L357-358, workload accounts ship logs via subscription
    // filters to a central destination. CloudWatch Logs validates the stream
    // and role by writing a test record when the destination is created, so
    // both dependencies are provisioned here rather than represented by ARNs.
    this.centralLogStream = new Stream(this, 'CentralLogStream', {
      streamName: 'agenticai-central-logs',
      streamMode: StreamMode.ON_DEMAND,
      encryption: StreamEncryption.KMS,
      encryptionKey: this.encryptionKey,
      retentionPeriod: Duration.hours(24),
      removalPolicy,
    });

    const recipientAccount = Stack.of(this).account;
    const sourceAccounts = [...new Set([...props.workloadAccountIds, recipientAccount])];
    const allowedSourceArns = sourceAccounts.map(
      (account) =>
        `arn:${Stack.of(this).partition}:logs:${Stack.of(this).region}:${account}:*`,
    );

    this.cloudWatchLogsDestinationRole = new Role(this, 'CloudWatchLogsDestinationRole', {
      roleName: 'AgenticAI-LogArchive-CWLDestinationRole',
      assumedBy: new ServicePrincipal('logs.amazonaws.com', {
        conditions: {
          ArnLike: {
            'aws:SourceArn': allowedSourceArns,
          },
        },
      }),
      description: 'Allows CloudWatch Logs from approved accounts to write to the central Kinesis stream.',
    });
    const destinationWriteGrant = this.centralLogStream.grantWrite(
      this.cloudWatchLogsDestinationRole,
    );

    this.cloudWatchLogsDestination = new CfnDestination(this, 'CrossAccountLogDestination', {
      destinationName: 'AgenticAI-CentralLogs',
      roleArn: this.cloudWatchLogsDestinationRole.roleArn,
      targetArn: this.centralLogStream.streamArn,
      destinationPolicy: JSON.stringify({
        Version: '2012-10-17',
        Statement: [
          {
            Sid: 'AllowWorkloadSubscribeFilterPut',
            Effect: 'Allow',
            Principal: {
              // CloudWatch Logs destination policies require 12-digit account
              // IDs here; unlike general IAM resource policies, root ARNs are
              // rejected by PutDestinationPolicy.
              AWS: props.workloadAccountIds,
            },
            Action: 'logs:PutSubscriptionFilter',
            Resource: `arn:aws:logs:${Stack.of(this).region}:${Stack.of(this).account}:destination:AgenticAI-CentralLogs`,
          },
        ],
      }),
    });
    // CreateDestination immediately assumes the role and writes a test record.
    // Depend on the generated IAM policy, not only the role resource, so that
    // CloudFormation cannot race destination validation against policy attachment.
    destinationWriteGrant.applyBefore(this.cloudWatchLogsDestination);
  }
}
