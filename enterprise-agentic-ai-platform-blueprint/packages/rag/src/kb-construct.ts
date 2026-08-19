/**
 * RagKnowledgeBaseConstruct
 *
 * Spec §4.2. Provides a per-tenant knowledge-base source bucket:
 *   - CMK-encrypted at rest
 *   - Versioned
 *   - Public access blocked
 *   - Enforced SSL
 *   - Bucket policy restricts GetObject / ListBucket to the workload VPCE
 *     (VPCE-only access; R-RAG-* spec §4.2.x)
 *   - Scoped prefix: `kbs/<tenantId>/<kbId>/...` per R-RAG-*
 *
 * Titan V2 embeddings are pinned at the Bedrock Knowledge Base wrapper that
 * lands in a follow-on once the L1 CFN resource ships.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import {
  AnyPrincipal,
  Effect,
  PolicyStatement,
} from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import {
  BlockPublicAccess,
  Bucket,
  BucketEncryption,
  ObjectOwnership,
} from 'aws-cdk-lib/aws-s3';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface RagKnowledgeBaseConstructProps {
  readonly tenantId: string;
  readonly kbId: string;
  readonly envName: string;
  /**
   * VPC endpoint id that is allowed to access the bucket. All other requests
   * are denied by the bucket policy.
   */
  readonly approvedVpceId: string;
  readonly retainOnDelete?: boolean;
}

export class RagKnowledgeBaseConstruct extends Construct {
  readonly sourceBucket: Bucket;
  readonly kmsKey: Key;
  readonly scopedPrefix: string;

  constructor(scope: Construct, id: string, props: RagKnowledgeBaseConstructProps) {
    super(scope, id);

    const retain = props.retainOnDelete ?? true;
    const removalPolicy = retain ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY;

    this.scopedPrefix = `kbs/${props.tenantId}/${props.kbId}`;

    this.kmsKey = new Key(this, 'Key', {
      alias: `alias/agenticai/rag-${props.envName}-${props.tenantId}-${props.kbId}`,
      description: `CMK for RAG source bucket ${props.tenantId}/${props.kbId}.`,
      enableKeyRotation: true,
      pendingWindow: Duration.days(30),
      removalPolicy,
    });

    // S3 server-access log destination for the RAG bucket. Lives in the same
    // construct so scoped-prefix auditing is local to the tenant.
    const accessLogsBucket = new Bucket(this, 'AccessLogs', {
      bucketName: `agenticai-rag-${props.envName}-${props.tenantId}-${props.kbId}-access-${Stack.of(this).account}`,
      encryption: BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      objectOwnership: ObjectOwnership.OBJECT_WRITER,
      versioned: true,
      removalPolicy,
      autoDeleteObjects: !retain,
      lifecycleRules: [
        {
          id: 'expire-access-logs',
          expiration: Duration.days(365),
          noncurrentVersionExpiration: Duration.days(90),
        },
      ],
    });
    NagSuppressions.addResourceSuppressions(
      accessLogsBucket,
      [
        { id: 'AwsSolutions-S1', reason: 'SEC-001: self-logging loop; RAG access-log destination.' },
        { id: 'NIST.800.53.R5-S3BucketLoggingEnabled', reason: 'SEC-001: log destination cannot log to itself.' },
        { id: 'NIST.800.53.R5-S3BucketReplicationEnabled', reason: 'SEC-002: CRR deferred to v2 DR roadmap.' },
        { id: 'NIST.800.53.R5-S3DefaultEncryptionKMS', reason: 'SEC-003: ObjectWriter ownership incompatible with CMK+bucket-key.' },
      ],
      true,
    );
    NagSuppressions.addResourceSuppressions(
      this.kmsKey,
      [
        // Alias not flagged but we've seen false positives on key-reuse — no suppression needed.
      ],
      true,
    );

    this.sourceBucket = new Bucket(this, 'SourceBucket', {
      bucketName: `agenticai-rag-${props.envName}-${props.tenantId}-${props.kbId}-${Stack.of(this).account}`,
      encryption: BucketEncryption.KMS,
      encryptionKey: this.kmsKey,
      bucketKeyEnabled: true,
      enforceSSL: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      objectOwnership: ObjectOwnership.BUCKET_OWNER_ENFORCED,
      versioned: true,
      removalPolicy,
      autoDeleteObjects: !retain,
      serverAccessLogsBucket: accessLogsBucket,
      serverAccessLogsPrefix: `s3-access/${this.scopedPrefix}/`,
    });

    // Deny everything not arriving through the approved VPCE — R-RAG-* VPCE-only.
    this.sourceBucket.addToResourcePolicy(
      new PolicyStatement({
        sid: 'DenyNonVpceAccess',
        effect: Effect.DENY,
        principals: [new AnyPrincipal()],
        actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket', 's3:DeleteObject'],
        resources: [this.sourceBucket.bucketArn, `${this.sourceBucket.bucketArn}/*`],
        conditions: {
          StringNotEquals: { 'aws:SourceVpce': props.approvedVpceId },
        },
      }),
    );
    NagSuppressions.addResourceSuppressions(
      this.sourceBucket,
      [
        { id: 'NIST.800.53.R5-S3BucketReplicationEnabled', reason: 'SEC-002: CRR deferred to v2 DR.' },
      ],
      true,
    );
  }
}
