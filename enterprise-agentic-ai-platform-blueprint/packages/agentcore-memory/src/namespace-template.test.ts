/**
 * Unit tests for memory-namespace static-segment enforcement.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  buildMemoryNamespacePath,
  buildSharedMemoryNamespacePath,
} from './namespace-template';

describe('buildMemoryNamespacePath', () => {
  it('produces a deterministic path with static tenant/agent/env segments', () => {
    expect(
      buildMemoryNamespacePath({
        tenantId: 'acme',
        agentId: 'chat',
        envName: 'prod',
      }),
    ).toBe('agenticai/prod/acme/chat/{actorId}/{memoryStrategyId}/{sessionId}');
  });

  it('rejects segments that contain uppercase or special characters', () => {
    expect(() =>
      buildMemoryNamespacePath({ tenantId: 'Acme', agentId: 'chat', envName: 'prod' }),
    ).toThrow(/tenantId/);
    expect(() =>
      buildMemoryNamespacePath({ tenantId: 'acme', agentId: 'chat/bot', envName: 'prod' }),
    ).toThrow(/agentId/);
    expect(() =>
      buildMemoryNamespacePath({ tenantId: 'acme', agentId: 'chat', envName: 'PROD' }),
    ).toThrow(/envName/);
  });

  it('accepts the canonical three-variable tail', () => {
    const p = buildMemoryNamespacePath({
      tenantId: 't',
      agentId: 'a',
      envName: 'e',
      dynamicTail: '{actorId}/{sessionId}',
    });
    expect(p).toContain('{actorId}/{sessionId}');
  });

  it('rejects tails with disallowed template variables', () => {
    expect(() =>
      buildMemoryNamespacePath({
        tenantId: 't',
        agentId: 'a',
        envName: 'e',
        dynamicTail: '{tenantId}/{actorId}',
      }),
    ).toThrow(/only \{actorId\}/);
  });
});

// G-6: shared-memory namespace coverage.
describe('buildSharedMemoryNamespacePath', () => {
  it('produces shared/<env>/<domain>/<topic>/<tail>', () => {
    expect(
      buildSharedMemoryNamespacePath({
        envName: 'prod',
        domainId: 'legal',
        topicId: 'risk-flags',
      }),
    ).toBe('shared/prod/legal/risk-flags/{actorId}/{sessionId}');
  });

  it('honours custom dynamic tail when only allowed vars used', () => {
    expect(
      buildSharedMemoryNamespacePath({
        envName: 'prod',
        domainId: 'finance',
        topicId: 'pricing',
        dynamicTail: '{actorId}',
      }),
    ).toBe('shared/prod/finance/pricing/{actorId}');
  });

  it('rejects bad domain id', () => {
    expect(() =>
      buildSharedMemoryNamespacePath({ envName: 'p', domainId: 'A', topicId: 'risk-flags' }),
    ).toThrow(/domainId/);
  });

  it('rejects too-short topic id', () => {
    expect(() =>
      buildSharedMemoryNamespacePath({ envName: 'p', domainId: 'legal', topicId: 'x' }),
    ).toThrow(/topicId/);
  });

  it('rejects non-allowed template vars in dynamic tail', () => {
    expect(() =>
      buildSharedMemoryNamespacePath({
        envName: 'p',
        domainId: 'legal',
        topicId: 'risk-flags',
        dynamicTail: '{tenantId}/{actorId}',
      }),
    ).toThrow(/only/);
  });

  it('rejects bad envName', () => {
    expect(() =>
      buildSharedMemoryNamespacePath({ envName: 'P', domainId: 'legal', topicId: 'risk-flags' }),
    ).toThrow(/envName/);
  });
});
