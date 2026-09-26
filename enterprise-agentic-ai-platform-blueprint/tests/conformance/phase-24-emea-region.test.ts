/**
 * EMEA Region onboarding conformance.
 *
 * Pins the Region and AgentCore VPC contracts required before a live
 * eu-west-1 campaign. The live service matrix and inference proof remain
 * separate release gates.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AgenticVpcConstruct } from '@agenticai/agentic-vpc';
import { D03WorkloadAgentStack } from '../../apps/workload-account/lib/d03-workload-agent-stack';

const ROOT = resolve(__dirname, '../..');

function subnetFilterTemplate(stack: Stack): string {
  const resources = Template.fromStack(stack).findResources(
    'Custom::AgentCoreSubnetFilter',
  );
  expect(Object.keys(resources)).toHaveLength(1);
  return JSON.stringify(resources);
}

function expectIrelandAzFilter(template: string): void {
  for (const azId of ['euw1-az1', 'euw1-az2', 'euw1-az3']) {
    expect(template).toContain(azId);
  }
  expect(template).not.toContain('use1-az');
  expect(template).not.toContain('usw2-az');
}

describe('EMEA Region resolution', () => {
  it('routes every bin stage through the fail-closed deployment Region resolver', () => {
    const source = readFileSync(
      resolve(ROOT, 'bin/agentic-ai-platform.ts'),
      'utf8',
    );
    expect(source).not.toMatch(/CDK_DEFAULT_REGION\s*\?\?/);
    expect(source.match(/deploymentRegion\(\)[,;]/g)).toHaveLength(8);
  });

  it('keeps repository context from silently pinning a deployment Region', () => {
    for (const path of [
      'cdk.json',
      'examples/reference-deployment-us-west-2/cdk.context.json',
    ]) {
      const context = JSON.parse(readFileSync(resolve(ROOT, path), 'utf8')) as {
        context?: Record<string, unknown>;
        [key: string]: unknown;
      };
      const values = context.context ?? context;
      expect(values).not.toHaveProperty('agenticai/defaultRegion');
      expect(values).not.toHaveProperty('agenticai/approvedRegions');
    }
  });
});

describe('EMEA legacy component boundary', () => {
  it.each([
    [
      'packages/evaluation-gates/src/evaluation-gates-construct.ts',
      'EvaluationGatesConstruct',
    ],
    [
      'packages/online-evaluation/src/online-evaluation-construct.ts',
      'OnlineEvaluationConstruct',
    ],
    [
      'packages/litellm-gateway/src/litellm-gateway-construct.ts',
      'LiteLLMGatewayConstruct',
    ],
    [
      'packages/agent-resilience/src/inference-circuit-breaker-construct.ts',
      'InferenceCircuitBreakerConstruct',
    ],
  ])('%s fails through the shared EMEA guard', (path, component) => {
    const source = readFileSync(resolve(ROOT, path), 'utf8');
    expect(source).toContain('assertEmeaProfilePathSupported');
    expect(source).toContain(`'${component}'`);
  });
});

describe('EMEA AgentCore VPC Availability Zones', () => {
  it('filters AgenticVpcConstruct subnets to Ireland-supported AZ IDs', () => {
    const app = new App();
    const stack = new Stack(app, 'IrelandVpc', {
      env: { account: '111111111111', region: 'eu-west-1' },
    });
    new AgenticVpcConstruct(stack, 'Vpc');
    expectIrelandAzFilter(subnetFilterTemplate(stack));
  });

  it('filters the legacy D03 workload stack to the same Ireland AZ IDs', () => {
    const app = new App();
    const stack = new D03WorkloadAgentStack(app, 'IrelandD03', {
      env: { account: '111111111111', region: 'eu-west-1' },
      platformAccountId: '222222222222',
      externalId: 'emea-test-external-id',
      tenantId: 'demo',
      agentId: 'primary',
      envName: 'nonprod',
      retainDataKeys: false,
    });
    expectIrelandAzFilter(subnetFilterTemplate(stack));
  });
});
