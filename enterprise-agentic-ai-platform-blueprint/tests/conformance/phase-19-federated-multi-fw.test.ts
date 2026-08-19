/**
 * Phase 19 conformance — federated mesh + multi-framework + shared memory.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-6 + Missing-7 + Missing-9
 * (synth-side; runtime tests live under tests/integration/).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  DEFAULT_DOMAIN_IDS,
  buildDomainScopedSk,
  buildFrameworkAdapterConfig,
  buildHomeDomainCondition,
  buildSharedMemoryNamespacePath,
  describeConflictPolicy,
  type FrameworkId,
} from '@agenticai/federation';

describe('Phase 19 — federated mesh + multi-fw + shared memory', () => {
  it('declares 4 default domains', () => {
    expect(DEFAULT_DOMAIN_IDS.length).toBeGreaterThanOrEqual(4);
  });

  it('domain-scoped SK shape pins to `domain#<id>#agent#<agent>`', () => {
    expect(buildDomainScopedSk('finance', 'invoice-bot')).toBe(
      'domain#finance#agent#invoice-bot',
    );
  });

  it('home-domain SCP condition uses aws:PrincipalTag/domainId', () => {
    const cond = buildHomeDomainCondition('legal');
    expect(cond.StringEquals['aws:PrincipalTag/domainId']).toBe('legal');
  });

  it('shared memory namespace lives under shared/<domain>/<topic>/', () => {
    expect(
      buildSharedMemoryNamespacePath({
        domainId: 'legal',
        topicId: 'risk-flags',
        conflictPolicy: 'last-write-wins',
      }),
    ).toMatch(/^shared\/legal\/risk-flags\//);
  });

  it('conflict policies enumerate the three locked options', () => {
    expect(describeConflictPolicy('last-write-wins').humanFallback).toBe(false);
    expect(describeConflictPolicy('merge-array').humanFallback).toBe(false);
    expect(describeConflictPolicy('human-arbitrate-via-hitl').humanFallback).toBe(true);
  });

  it('framework adapter qualifies tool names for all 3 frameworks', () => {
    for (const framework of ['strands', 'langgraph', 'crewai'] as const satisfies readonly FrameworkId[]) {
      const c = buildFrameworkAdapterConfig({
        framework,
        gatewayUrl: 'https://gw.example.com/a2a',
        targetName: 'demo',
        toolIds: ['tool-echo'],
        guardrailIdentifier: 'gd-1',
        inferenceProfileArn: 'arn:aws:bedrock:us-west-2:111111111111:application-inference-profile/demo',
      });
      expect(c.toolNames).toEqual(['target-demo___tool-echo']);
      expect(c.framework).toBe(framework);
    }
  });
});
