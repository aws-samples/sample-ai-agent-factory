/*
 * Shared artifact-store controls for CDK Pipelines.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Tags } from 'aws-cdk-lib';
import { Key } from 'aws-cdk-lib/aws-kms';
import {
  BlockPublicAccess,
  Bucket,
  BucketEncryption,
  ObjectOwnership,
} from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface PipelineResourceTags {
  readonly applicationId: string;
  readonly agentId: string;
  readonly tenantId: string;
  readonly costCentre: string;
  readonly environment: string;
}

/** Apply the mandatory five allocation tags to every taggable child resource. */
export function applyPipelineResourceTags(
  scope: Construct,
  tags: PipelineResourceTags,
): void {
  Tags.of(scope).add('application-id', tags.applicationId);
  Tags.of(scope).add('agent-id', tags.agentId);
  Tags.of(scope).add('tenant-id', tags.tenantId);
  Tags.of(scope).add('cost-centre', tags.costCentre);
  Tags.of(scope).add('environment', tags.environment);
}

/**
 * Create an encrypted artifact store that cannot survive a failed test
 * deployment as an untracked bucket. KMS deletion still observes AWS's
 * mandatory seven-day pending window.
 */
export function createPipelineArtifactBucket(
  scope: Construct,
  id: string,
  tags: PipelineResourceTags,
): Bucket {
  const encryptionKey = new Key(scope, `${id}Key`, {
    description: 'CMK for short-lived AgenticAI pipeline artifacts',
    enableKeyRotation: true,
    pendingWindow: Duration.days(7),
    removalPolicy: RemovalPolicy.DESTROY,
  });

  const bucket = new Bucket(scope, id, {
    autoDeleteObjects: true,
    blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
    encryption: BucketEncryption.KMS,
    encryptionKey,
    enforceSSL: true,
    lifecycleRules: [
      {
        abortIncompleteMultipartUploadAfter: Duration.days(7),
        expiration: Duration.days(30),
      },
    ],
    objectOwnership: ObjectOwnership.BUCKET_OWNER_ENFORCED,
    removalPolicy: RemovalPolicy.DESTROY,
  });

  applyPipelineResourceTags(encryptionKey, tags);
  applyPipelineResourceTags(bucket, tags);
  return bucket;
}
