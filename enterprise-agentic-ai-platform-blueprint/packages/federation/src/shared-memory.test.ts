/**
 * Tests for shared-memory namespace + conflict resolution.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  buildSharedMemoryNamespacePath,
  describeConflictPolicy,
  validateSharedMemoryOptions,
} from './shared-memory';

describe('shared memory', () => {
  const base = { domainId: 'legal', topicId: 'risk-flags', conflictPolicy: 'last-write-wins' as const };

  it('builds the shared/<domain>/<topic> namespace shape', () => {
    expect(buildSharedMemoryNamespacePath(base)).toBe('shared/legal/risk-flags/{actorId}/{sessionId}');
  });

  it('rejects bad topic ids', () => {
    expect(() => validateSharedMemoryOptions({ ...base, topicId: 'X' })).toThrow();
  });

  it('rejects unknown conflict policies', () => {
    expect(() =>
      validateSharedMemoryOptions({ ...base, conflictPolicy: 'first-write-wins' as any }),
    ).toThrow();
  });

  it('declares the three policies with descriptions', () => {
    expect(describeConflictPolicy('last-write-wins').humanFallback).toBe(false);
    expect(describeConflictPolicy('merge-array').humanFallback).toBe(false);
    expect(describeConflictPolicy('human-arbitrate-via-hitl').humanFallback).toBe(true);
  });

  it('rejects dynamicTail using non-allowed template vars', () => {
    expect(() =>
      validateSharedMemoryOptions({ ...base, dynamicTail: '{tenantId}' }),
    ).toThrow();
  });
});
