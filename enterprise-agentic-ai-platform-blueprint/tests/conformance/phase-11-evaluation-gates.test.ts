/**
 * Phase 11 conformance — EvaluationGatesConstruct.
 *
 * Pins the CFN shape of the gap-closure evaluation-gates infrastructure:
 *   - S3 corpus bucket with KMS + Object Lock GOVERNANCE 90 days + versioned
 *     + TLS-only + BlockPublicAccess.
 *   - DDB run-history table with CMK + PITR + GSI by-status.
 *   - CMK with rotation enabled + alias.
 *   - Runner role: bedrock:InvokeModel scoped to allow-list resources only,
 *     no wildcard `bedrock:*`.
 *   - SNS failures topic + KMS encryption.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Partial-1 (CDK-side).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';

import { EvaluationGatesConstruct } from '@agenticai/evaluation-gates';

function synth(envName = 'nonprod') {
  const app = new App();
  const stack = new Stack(app, 'EvalGatesTest', {
    env: { account: '111111111111', region: 'us-west-2' },
  });
  new EvaluationGatesConstruct(stack, 'EvalGates', { envName });
  return Template.fromStack(stack);
}

describe('Phase 11 — EvaluationGatesConstruct CFN shape', () => {
  it('emits the corpus bucket with Object Lock GOVERNANCE 90 days', () => {
    const t = synth();
    t.hasResourceProperties('AWS::S3::Bucket', {
      ObjectLockEnabled: true,
      ObjectLockConfiguration: {
        ObjectLockEnabled: 'Enabled',
        Rule: {
          DefaultRetention: {
            Mode: 'GOVERNANCE',
            Days: 90,
          },
        },
      },
    });
  });

  it('encrypts corpus with KMS + bucket-key + blocks public access', () => {
    const t = synth();
    t.hasResourceProperties('AWS::S3::Bucket', {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: Match.arrayWith([
          Match.objectLike({
            BucketKeyEnabled: true,
            ServerSideEncryptionByDefault: {
              SSEAlgorithm: 'aws:kms',
            },
          }),
        ]),
      },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      VersioningConfiguration: { Status: 'Enabled' },
    });
  });

  it('denies non-TLS access via bucket policy', () => {
    const t = synth();
    const policies = t.findResources('AWS::S3::BucketPolicy');
    const denyTls = Object.values(policies).some((p: any) =>
      JSON.stringify(p.Properties.PolicyDocument).includes('"aws:SecureTransport":"false"'),
    );
    expect(denyTls).toBe(true);
  });

  it('emits the DDB run-history table CMK-encrypted with PITR + GSI', () => {
    const t = synth();
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agenticai-eval-runs-nonprod',
      BillingMode: 'PAY_PER_REQUEST',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      SSESpecification: { SSEEnabled: true, SSEType: 'KMS' },
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'by-status' }),
      ]),
    });
  });

  it('runner role policy lists bedrock:InvokeModel on allow-list resources, NOT a wildcard', () => {
    const t = synth();
    const policies = t.findResources('AWS::IAM::Policy');
    const runnerPolicies = Object.values(policies).filter((p: any) =>
      p.Properties.PolicyName?.includes('RunnerRole'),
    );
    expect(runnerPolicies.length).toBeGreaterThan(0);
    const allStatements = runnerPolicies.flatMap(
      (p: any) => p.Properties.PolicyDocument.Statement as Array<Record<string, unknown>>,
    );
    const invoke = allStatements.find(
      (s: any) =>
        Array.isArray(s.Action)
          ? s.Action.includes('bedrock:InvokeModel')
          : s.Action === 'bedrock:InvokeModel',
    );
    expect(invoke).toBeDefined();
    expect(JSON.stringify(invoke!.Resource)).not.toContain('"*"');
    // The SSOT helper allowedBedrockResources() emits foundation-model + inference-profile ARNs
    expect(JSON.stringify(invoke!.Resource)).toMatch(/foundation-model|inference-profile/);
  });

  it('emits a CMK with rotation + alias', () => {
    const t = synth();
    t.hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: true,
    });
    t.hasResourceProperties('AWS::KMS::Alias', {
      AliasName: 'alias/agenticai/eval-gates-nonprod',
    });
  });

  it('emits an SNS failures topic encrypted with the CMK', () => {
    const t = synth();
    t.hasResourceProperties('AWS::SNS::Topic', {
      TopicName: 'agenticai-eval-failures-nonprod',
      KmsMasterKeyId: Match.anyValue(),
    });
  });

  it('runner role assumed by codebuild.amazonaws.com', () => {
    const t = synth();
    t.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'AgenticAI-EvaluationGateRunner-nonprod',
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Principal: { Service: 'codebuild.amazonaws.com' },
            Action: 'sts:AssumeRole',
          }),
        ]),
      },
    });
  });
});
