/**
 * Phase 7 conformance — CDK Pipelines (platform + workload) with evaluation
 * gate and manual approval.
 *
 * Spec: R-DEVX-002 mandatory stage sequence (§1.3.5 L210-212).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { PlatformPipelineStack } from '../../pipelines/platform-pipeline-stack';
import { WorkloadPipelineStack } from '../../pipelines/workload-pipeline-stack';

const GITHUB_CONNECTION =
  'arn:aws:codestar-connections:us-west-2:111111111111:connection/abc-123';

function synthPlatform() {
  const app = new App();
  const stack = new PlatformPipelineStack(app, 'PP', {
    env: { account: '111111111111', region: 'us-west-2' },
    githubRepo: 'aws-samples/sample-ai-agent-factory',
    githubConnectionArn: GITHUB_CONNECTION,
    organizationId: 'o-example123',
    logArchive: { env: { account: '333333333333', region: 'us-west-2' }, envName: 'nonprod' },
    audit: { env: { account: '666666666666', region: 'us-west-2' }, envName: 'nonprod' },
    platformNonprod: { env: { account: '111111111111', region: 'us-west-2' }, envName: 'nonprod' },
    platformProd: { env: { account: '222222222222', region: 'us-west-2' }, envName: 'prod' },
    workloadAccountIds: ['444444444444', '555555555555'],
    pipelineRoleArn: 'arn:aws:iam::111111111111:role/AgenticAI-PlatformPipelineRole',
  });
  return Template.fromStack(stack);
}

function synthWorkload() {
  const app = new App();
  const stack = new WorkloadPipelineStack(app, 'WP', {
    env: { account: '111111111111', region: 'us-west-2' },
    githubRepo: 'aws-samples/sample-ai-agent-factory',
    githubConnectionArn: GITHUB_CONNECTION,
    tenantId: 'demo',
    agentId: 'primary',
    costCentre: 'engineering',
    workloadNonprodEnv: { account: '444444444444', region: 'us-west-2' },
    workloadProdEnv: { account: '555555555555', region: 'us-west-2' },
  });
  return Template.fromStack(stack);
}

describe('Phase 7 — Platform pipeline', () => {
  it('emits a single CodePipeline', () => {
    const t = synthPlatform();
    t.resourceCountIs('AWS::CodePipeline::Pipeline', 1);
  });

  it('pipeline name is stable', () => {
    const t = synthPlatform();
    t.hasResourceProperties('AWS::CodePipeline::Pipeline', {
      Name: 'agenticai-platform-pipeline',
    });
  });

  it('configures cross-account keys (required for multi-account stages)', () => {
    const t = synthPlatform();
    // CodePipelines emits encrypted artifact store with KMS key ARN when crossAccountKeys=true.
    const pipelines = t.findResources('AWS::CodePipeline::Pipeline');
    const pipeline = Object.values(pipelines)[0] as any;
    const stores = pipeline.Properties.ArtifactStores ?? [
      pipeline.Properties.ArtifactStore,
    ];
    const usesKms = stores.some(
      (s: any) => s?.ArtifactStore?.EncryptionKey?.Type === 'KMS' || s?.EncryptionKey?.Type === 'KMS',
    );
    expect(usesKms).toBe(true);
  });
});

describe('Phase 7 — Workload pipeline has mandatory stages + eval gate', () => {
  it('emits an evaluation-gate CodeBuild with the 5 SLO threshold env vars', () => {
    const t = synthWorkload();
    const projects = t.findResources('AWS::CodeBuild::Project');
    const joined = JSON.stringify(projects);
    expect(joined).toContain('EVAL_REGRESSION_PASS_MIN_PCT');
    expect(joined).toContain('EVAL_GUARDRAIL_VIOLATION_MAX_PCT');
    expect(joined).toContain('EVAL_QUALITY_MIN_PCT');
    expect(joined).toContain('EVAL_TOOL_SUCCESS_MIN_PCT');
    expect(joined).toContain('EVAL_FIRST_TOKEN_P99_MAX_MS');
  });

  it('pipeline name embeds tenant + agent', () => {
    const t = synthWorkload();
    t.hasResourceProperties('AWS::CodePipeline::Pipeline', {
      Name: 'agenticai-workload-demo-primary',
    });
  });
});
