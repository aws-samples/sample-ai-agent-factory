/**
 * Phase L conformance — D03PlatformCoreStack v0.5.0 AgentCore Registry seed.
 *
 * Pins the optional opt-in path (`enableAgentRegistry: true`) where the
 * platform stack provisions a single `PlatformRegistryConstruct` and seeds
 * it from `PLATFORM_TOOL_CATALOGUE`. The legacy v0.4.0 path
 * (`enableAgentRegistry` unset / false) is unchanged and exercised by the
 * integration suite — this file pins ONLY the opt-in mutation surface.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { PLATFORM_TOOL_CATALOGUE } from '@agenticai/platform-tool-catalogue';

import { D03PlatformCoreStack } from '../../apps/platform-account/lib/d03-platform-core-stack';

const PLATFORM_ACCOUNT_ID = '222222222222';
const WORKLOAD_ACCOUNT_ID = '333333333333';

interface SynthOpts {
  readonly enableAgentRegistry?: boolean;
  readonly registryName?: string;
  readonly registryAutoApproveOnSeed?: boolean;
}

function synth(opts: SynthOpts = {}): { template: Template; stack: D03PlatformCoreStack } {
  const app = new App();
  const stack = new D03PlatformCoreStack(app, 'AgenticAI-D03-PlatformCoreStack', {
    env: { account: PLATFORM_ACCOUNT_ID, region: 'us-east-1' },
    workloadAccountIds: [WORKLOAD_ACCOUNT_ID],
    externalId: 'phase-l-test-eid',
    tenantAllocations: [
      {
        tenantId: 'demo',
        agentId: 'primary',
        workloadAccountId: WORKLOAD_ACCOUNT_ID,
        costCentre: 'platform',
        envName: 'nonprod',
      },
    ],
    enableAgentRegistry: opts.enableAgentRegistry,
    registryName: opts.registryName,
    registryAutoApproveOnSeed: opts.registryAutoApproveOnSeed,
  });
  return { template: Template.fromStack(stack), stack };
}

describe('Phase L — Registry opt-in seed', () => {
  it('emits zero Custom::BedrockAgentCoreRegistry when enableAgentRegistry is unset (back-compat)', () => {
    const { template } = synth();
    const r = template.findResources('Custom::BedrockAgentCoreRegistry');
    expect(Object.keys(r)).toHaveLength(0);
  });

  it('emits exactly one Custom::BedrockAgentCoreRegistry when enabled', () => {
    const { template } = synth({
      enableAgentRegistry: true,
      registryName: 'agenticai-test-registry',
    });
    const r = template.findResources('Custom::BedrockAgentCoreRegistry');
    expect(Object.keys(r)).toHaveLength(1);
  });

  it('seeds one Custom::BedrockAgentCoreRegistryRecord per catalogue tool', () => {
    const { template } = synth({
      enableAgentRegistry: true,
      registryName: 'agenticai-test-registry',
    });
    const recs = template.findResources('Custom::BedrockAgentCoreRegistryRecord');
    const expectedCount = Object.keys(PLATFORM_TOOL_CATALOGUE).length;
    expect(Object.keys(recs)).toHaveLength(expectedCount);
  });

  it('does NOT emit Submit/Approve resources when registryAutoApproveOnSeed is false', () => {
    const { template } = synth({
      enableAgentRegistry: true,
      registryName: 'agenticai-test-registry',
      registryAutoApproveOnSeed: false,
    });
    const submits = template.findResources('Custom::BedrockAgentCoreRegistryRecordSubmit');
    const approves = template.findResources('Custom::BedrockAgentCoreRegistryRecordApprove');
    expect(Object.keys(submits)).toHaveLength(0);
    expect(Object.keys(approves)).toHaveLength(0);
  });

  it('emits Submit + Approve per record when registryAutoApproveOnSeed is true', () => {
    const { template } = synth({
      enableAgentRegistry: true,
      registryName: 'agenticai-test-registry',
      registryAutoApproveOnSeed: true,
    });
    const expectedCount = Object.keys(PLATFORM_TOOL_CATALOGUE).length;
    const submits = template.findResources('Custom::BedrockAgentCoreRegistryRecordSubmit');
    const approves = template.findResources('Custom::BedrockAgentCoreRegistryRecordApprove');
    expect(Object.keys(submits)).toHaveLength(expectedCount);
    expect(Object.keys(approves)).toHaveLength(expectedCount);
  });

  it('emits AgentRegistryId + AgentRegistryArn outputs when enabled', () => {
    const { template } = synth({
      enableAgentRegistry: true,
      registryName: 'agenticai-test-registry',
    });
    const outputs = template.findOutputs('*');
    expect(Object.keys(outputs)).toContain('AgentRegistryId');
    expect(Object.keys(outputs)).toContain('AgentRegistryArn');
  });

  it('throws when enableAgentRegistry=true but registryName is missing', () => {
    expect(() => synth({ enableAgentRegistry: true })).toThrow(/registryName/);
  });
});
