import { describe, expect, it } from 'vitest';

import type { DeploymentGovernanceV1 } from '../../types/workflow';
import { createRegistryCanvasSnapshot } from './registry';


describe('registry canvas snapshot governance', () => {
  it('publishes schema v2 with the exact captured governance', () => {
    const governance: DeploymentGovernanceV1 = {
      version: 1,
      namingProfile: { prefix: 'ecb' },
      tags: {
        explicitValues: { owner: 'alice' },
        effectiveValues: { owner: 'alice' },
        profile: { name: 'regulated', updatedAt: '2026-09-23T12:00:00Z' },
        policyRevision: 'sha256:v7',
      },
    };

    const snapshot = createRegistryCanvasSnapshot(
      'payments',
      [{ id: 'runtime-1' }],
      [],
      { x: 12, y: -4, zoom: 1.25 },
      governance,
    );

    expect(snapshot).toEqual({
      schemaVersion: 2,
      name: 'payments',
      nodes: [{ id: 'runtime-1' }],
      edges: [],
      viewport: { x: 12, y: -4, zoom: 1.25 },
      governance,
    });
    expect(snapshot.governance).not.toBe(governance);
  });

  it('refuses a stale captured tag set with no policy revision', () => {
    expect(() => createRegistryCanvasSnapshot(
      'invalid',
      [],
      [],
      { x: 0, y: 0, zoom: 1 },
      {
        version: 1,
        namingProfile: null,
        tags: {
          explicitValues: { owner: 'alice' },
          effectiveValues: { owner: 'alice' },
          profile: null,
          policyRevision: '',
        },
      },
    )).toThrow(/policyRevision/);
  });
});
