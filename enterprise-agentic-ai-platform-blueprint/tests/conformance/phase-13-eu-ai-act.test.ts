/**
 * Phase 13 conformance — EU AI Act ConformityAssessmentConstruct.
 *
 * Pins:
 *   - Object Lock **COMPLIANCE 7y** on the record-keeping bucket (Article 12).
 *   - KMS + bucket-key + TLS-only + BlockPublicAccess.
 *   - DDB index CMK + PITR.
 *   - Post-deploy custom resource uploads three Markdown documents.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-2 (CDK-side).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';

import { ConformityAssessmentConstruct } from '@agenticai/eu-ai-act-compliance';

function synth(blueprintId = 'multi-agent') {
  const app = new App();
  const stack = new Stack(app, 'AiActTest', {
    env: { account: '111111111111', region: 'us-west-2' },
  });
  new ConformityAssessmentConstruct(stack, 'AiAct', {
    envName: 'prod',
    tenantId: 'demo',
    agentId: 'primary',
    blueprintId,
    providerName: 'AWS Solutions',
    contactEmail: 'compliance@example.com',
    modelIds: ['anthropic.claude-sonnet-4-5-20250929-v1:0'],
    humanOversightContact: 'oversight@example.com',
  });
  return Template.fromStack(stack);
}

describe('Phase 13 — ConformityAssessmentConstruct CFN shape', () => {
  it('emits the record-keeping bucket with Object Lock COMPLIANCE 7 years', () => {
    const t = synth();
    t.hasResourceProperties('AWS::S3::Bucket', {
      ObjectLockEnabled: true,
      ObjectLockConfiguration: {
        ObjectLockEnabled: 'Enabled',
        Rule: {
          DefaultRetention: {
            Mode: 'COMPLIANCE',
            Days: 7 * 365,
          },
        },
      },
    });
  });

  it('encrypts the record-keeping bucket with KMS + bucket-key + blocks public access', () => {
    const t = synth();
    t.hasResourceProperties('AWS::S3::Bucket', {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: Match.arrayWith([
          Match.objectLike({
            BucketKeyEnabled: true,
            ServerSideEncryptionByDefault: { SSEAlgorithm: 'aws:kms' },
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

  it('emits the records DDB CMK-encrypted with PITR', () => {
    const t = synth();
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agenticai-aiact-records-prod-demo-primary',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      SSESpecification: { SSEEnabled: true, SSEType: 'KMS' },
    });
  });

  it('emits 3 custom-resource uploads (one per conformity document)', () => {
    const t = synth();
    // AwsCustomResource synthesises as Custom::AWS resources.
    const customs = t.findResources('Custom::AWS');
    expect(Object.keys(customs).length).toBeGreaterThanOrEqual(3);
  });

  it('the multi-agent default classifies as high risk', () => {
    const t = synth('multi-agent');
    // The CMK alias name embeds tenant/agent, not risk; risk shows in the
    // uploaded Markdown body. Assertion is on the in-process construct
    // property to keep the test cheap.
    expect(t).toBeDefined();
    // Direct construct check:
    const app = new App();
    const stack = new Stack(app, 'TS', { env: { account: '111111111111', region: 'us-west-2' } });
    const c = new ConformityAssessmentConstruct(stack, 'C', {
      envName: 'prod',
      tenantId: 'demo',
      agentId: 'primary',
      blueprintId: 'multi-agent',
      providerName: 'AWS',
      contactEmail: 'a@b.com',
      modelIds: ['m'],
      humanOversightContact: 'h@b.com',
    });
    expect(c.riskClass).toBe('high');
  });
});
