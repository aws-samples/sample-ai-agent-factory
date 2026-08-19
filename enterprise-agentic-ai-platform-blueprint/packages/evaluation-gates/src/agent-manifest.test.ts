/**
 * Tests for buildAgentManifest — multi-artefact deterministic versioning.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { buildAgentManifest } from './agent-manifest';

const FIXED_NOW = () => new Date('2026-05-15T00:00:00Z');

const baseInput = {
  agentId: 'chatbot-primary',
  tenantId: 'demo',
  gitSha: 'abcdef1234567',
  promptHashes: { 'system.md': 'aaaa', 'few-shot.md': 'bbbb' },
  toolPermissions: ['tool-echo', 'tool-ping'],
  configHash: 'cccc',
  thresholdsHash: 'dddd',
};

describe('buildAgentManifest', () => {
  it('produces a deterministic manifestSha for equivalent input', () => {
    const a = buildAgentManifest(baseInput, FIXED_NOW);
    const b = buildAgentManifest(baseInput, FIXED_NOW);
    expect(a.manifestSha).toEqual(b.manifestSha);
  });

  it('is order-independent across toolPermissions', () => {
    const a = buildAgentManifest(baseInput, FIXED_NOW);
    const b = buildAgentManifest(
      { ...baseInput, toolPermissions: ['tool-ping', 'tool-echo'] },
      FIXED_NOW,
    );
    expect(a.manifestSha).toEqual(b.manifestSha);
  });

  it('is order-independent across promptHashes keys', () => {
    const a = buildAgentManifest(baseInput, FIXED_NOW);
    const b = buildAgentManifest(
      { ...baseInput, promptHashes: { 'few-shot.md': 'bbbb', 'system.md': 'aaaa' } },
      FIXED_NOW,
    );
    expect(a.manifestSha).toEqual(b.manifestSha);
  });

  it('produces a different sha when any byte of any input changes', () => {
    const a = buildAgentManifest(baseInput, FIXED_NOW);
    const b = buildAgentManifest({ ...baseInput, gitSha: 'fedcba9876543' }, FIXED_NOW);
    const c = buildAgentManifest({ ...baseInput, configHash: 'ZZZZ' }, FIXED_NOW);
    const d = buildAgentManifest({ ...baseInput, thresholdsHash: 'ZZZZ' }, FIXED_NOW);
    expect(new Set([a.manifestSha, b.manifestSha, c.manifestSha, d.manifestSha]).size).toBe(4);
  });

  it('rejects malformed gitSha', () => {
    expect(() => buildAgentManifest({ ...baseInput, gitSha: 'not-hex' })).toThrow(/gitSha/);
    expect(() => buildAgentManifest({ ...baseInput, gitSha: 'abc' })).toThrow(/gitSha/);
  });

  it('rejects non-kebab-case toolPermission ids', () => {
    expect(() =>
      buildAgentManifest({ ...baseInput, toolPermissions: ['Tool-Echo'] }),
    ).toThrow(/kebab-case/);
    expect(() =>
      buildAgentManifest({ ...baseInput, toolPermissions: ['ab'] }),
    ).toThrow(/kebab-case/);
  });

  it('emits manifestVersion=1 + ISO timestamp', () => {
    const m = buildAgentManifest(baseInput, FIXED_NOW);
    expect(m.manifestVersion).toBe(1);
    expect(m.emittedAt).toBe('2026-05-15T00:00:00.000Z');
  });
});
