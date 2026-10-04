import { beforeEach, describe, expect, it, vi } from 'vitest';

const { updateWorkflow } = vi.hoisted(() => ({
  updateWorkflow: vi.fn(),
}));

vi.mock('../services/api', () => ({
  getApiClient: () => ({ updateWorkflow }),
  isApiError: () => false,
}));

import type { DeploymentGovernanceV1 } from '../types/workflow';
import { createBackendSaveFunction } from './autoSave';


const GOVERNANCE: DeploymentGovernanceV1 = {
  version: 1,
  namingProfile: { prefix: 'ecb' },
  tags: {
    explicitValues: { owner: 'alice' },
    effectiveValues: { owner: 'alice' },
    profile: { name: 'regulated', updatedAt: '2026-09-23T12:00:00Z' },
    policyRevision: 'sha256:v7',
  },
};

describe('createBackendSaveFunction governance', () => {
  beforeEach(() => {
    updateWorkflow.mockReset();
    updateWorkflow.mockResolvedValue({ workflow: {} });
    localStorage.clear();
  });

  it('sends the exact canonical V1 object to the workflow API', async () => {
    const save = createBackendSaveFunction('workflow-1');
    const result = await save(JSON.stringify({
      id: 'workflow-1',
      name: 'Governed',
      description: '',
      version: '1.0.0',
      nodes: [],
      edges: [],
      viewport: { x: 0, y: 0, zoom: 1 },
      metadata: {
        author: 'alice',
        tags: [],
        awsRegion: 'eu-west-1',
        deploymentStatus: 'not_deployed',
      },
      governance: GOVERNANCE,
    }));

    expect(result.success).toBe(true);
    expect(updateWorkflow).toHaveBeenCalledWith(
      'workflow-1',
      expect.objectContaining({ governance: GOVERNANCE }),
    );
  });

  it('materializes empty V1 governance for a legacy local document', async () => {
    const save = createBackendSaveFunction('workflow-legacy');
    await save(JSON.stringify({
      id: 'workflow-legacy',
      name: 'Legacy',
      nodes: [],
      edges: [],
      viewport: { x: 0, y: 0, zoom: 1 },
    }));

    expect(updateWorkflow).toHaveBeenCalledWith(
      'workflow-legacy',
      expect.objectContaining({
        governance: {
          version: 1,
          namingProfile: null,
          tags: {
            explicitValues: {},
            effectiveValues: {},
            profile: null,
            policyRevision: '',
          },
        },
      }),
    );
  });
});
