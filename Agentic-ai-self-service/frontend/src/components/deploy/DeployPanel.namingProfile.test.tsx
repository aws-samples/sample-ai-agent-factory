import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DeployPanel } from './DeployPanel';
import type { RuntimeConfiguration } from '../../types/components';
import { useWorkflowStore } from '../../store/workflowStore';
import { computeTagPolicyRevision } from './resourceTagState';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

const config: RuntimeConfiguration = {
  name: 'test-runtime',
  entrypoint: 'agent.py',
  framework: 'strands_agents',
  model: {
    provider: 'bedrock',
    modelId: 'us.anthropic.claude-sonnet-5',
    temperature: 0.7,
    topP: 0.9,
  },
  systemPrompt: 'hi',
  deploymentType: 'direct_code_deploy',
  pythonRuntime: 'PYTHON_3_12',
  protocol: 'HTTP',
  idleTimeout: 900,
  maxLifetime: 28800,
  enableOtel: false,
  modelProvider: 'bedrock',
  multiAgentPattern: 'none',
};

const POLICY_CREATED_AT = '2026-09-20T10:00:00Z';
const POLICY_UPDATED_AT = '2026-09-20T10:00:00Z';
const PROFILE_UPDATED_AT = '2026-09-20T11:00:00Z';
const TAG_POLICY = {
  key: 'Environment',
  default_value: null,
  required: true,
  show_on_card: true,
  created_at: POLICY_CREATED_AT,
  updated_at: POLICY_UPDATED_AT,
};

function requestBody(path: string): Record<string, unknown> {
  const call = mockAuthFetch.mock.calls.find(([url]) => url === path);
  if (!call) throw new Error(`No request made to ${path}`);
  return JSON.parse((call[1] as { body: string }).body);
}

function mockGovernanceApi() {
  mockAuthFetch.mockImplementation(async (url: string) => {
    if (url === '/api/settings/tags') {
      return {
        ok: true,
        json: async () => [{
          ...TAG_POLICY,
        }],
      };
    }
    if (url === '/api/settings/tag-profiles') {
      return {
        ok: true,
        json: async () => [{
          name: 'regulated',
          values: { Environment: 'production' },
          created_at: POLICY_CREATED_AT,
          updated_at: PROFILE_UPDATED_AT,
        }],
      };
    }
    if (url === '/api/generate-cfn-template') {
      return {
        ok: true,
        json: async () => ({
          download_url: 'https://downloads.example.invalid/artifact.zip',
        }),
      };
    }
    if (url === '/api/deploy') {
      return {
        ok: true,
        json: async () => ({
          success: true,
          runtimeId: 'runtime-1',
          endpoint: 'https://runtime.example.invalid',
        }),
      };
    }
    return { ok: true, json: async () => ({}) };
  });
}

async function waitForGovernanceReady() {
  await waitFor(() => {
    expect(screen.getByRole('button', { name: 'Export as Python' })).toBeEnabled();
    expect(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    ).toBeEnabled();
  }, { timeout: 10_000 });
}

describe('DeployPanel CloudFormation naming profile', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useWorkflowStore.getState().resetWorkflowDocument(null);
    sessionStorage.clear();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    mockAuthFetch.mockImplementation(async (url: string) => {
      if (url === '/api/settings/tags' || url === '/api/settings/tag-profiles') {
        return { ok: true, json: async () => [] };
      }
      if (url === '/api/generate-cfn-template' || url === '/api/export-python') {
        return {
          ok: true,
          json: async () => ({
            download_url: 'https://downloads.example.invalid/artifact.zip',
          }),
        };
      }
      if (url === '/api/deploy') {
        return {
          ok: true,
          json: async () => ({
            success: true,
            runtimeId: 'runtime-1',
            endpoint: 'https://runtime.example.invalid',
          }),
        };
      }
      return { ok: true, json: async () => ({}) };
    });
  });

  it('sends namingProfile only to the CloudFormation export route', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="node-1"
        isVisible
        onClose={() => {}}
      />,
    );
    await waitForGovernanceReady();

    fireEvent.change(screen.getByLabelText('Naming prefix'), {
      target: { value: 'ecb' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    );
    await waitFor(() => {
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/generate-cfn-template')).toBe(true);
    });

    fireEvent.click(screen.getByRole('button', { name: 'Export as Python' }));
    await waitFor(() => {
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/export-python')).toBe(true);
    });

    fireEvent.click(
      screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0],
    );
    await waitFor(() => {
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/deploy')).toBe(true);
    });

    expect(requestBody('/api/generate-cfn-template').namingProfile).toEqual({
      prefix: 'ecb',
    });
    expect(requestBody('/api/export-python')).not.toHaveProperty('namingProfile');
    expect(requestBody('/api/deploy')).not.toHaveProperty('namingProfile');
  });

  it('sends the selected governance tag profile with its resolved tags and naming profile', async () => {
    mockGovernanceApi();

    render(
      <DeployPanel
        config={config}
        nodeId="node-1"
        isVisible
        onClose={() => {}}
      />,
    );

    // The option must exist before the change, or the change is a silent no-op (a

    // profile list still loading under worker contention failed certification #31).

    await screen.findByRole('option', { name: 'regulated' }, { timeout: 10_000 });

    fireEvent.change(await screen.findByLabelText(
      'Tag profile',
      {},
      { timeout: 10_000 },
    ), {
      target: { value: 'regulated' },
    });
    await screen.findByDisplayValue('production', {}, { timeout: 10_000 });
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: 'Download CloudFormation Template' }),
      ).toBeEnabled();
    });
    fireEvent.change(screen.getByLabelText('Naming prefix'), {
      target: { value: 'ecb' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    );

    await waitFor(() => {
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/generate-cfn-template')).toBe(true);
    });
    const policyRevision = await computeTagPolicyRevision([TAG_POLICY]);
    expect(requestBody('/api/generate-cfn-template')).toMatchObject({
      tagProfile: 'regulated',
      resourceTags: { Environment: 'production' },
      policyRevision,
      tagProfileUpdatedAt: PROFILE_UPDATED_AT,
      namingProfile: { prefix: 'ecb' },
    });
  });

  it('keeps explicit governance tags through the Deploy to Chat to Deploy transition', async () => {
    mockGovernanceApi();
    render(
      <DeployPanel
        config={config}
        nodeId="node-1"
        isVisible
        onClose={() => {}}
      />,
    );

    // The option must exist before the change, or the change is a silent no-op (a

    // profile list still loading under worker contention failed certification #31).

    await screen.findByRole('option', { name: 'regulated' }, { timeout: 10_000 });

    fireEvent.change(await screen.findByLabelText(
      'Tag profile',
      {},
      { timeout: 10_000 },
    ), {
      target: { value: 'regulated' },
    });
    const environment = await screen.findByLabelText(/Environment/);
    fireEvent.change(environment, {
      target: { value: 'staging-override' },
    });
    const deployButton = screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0];
    await waitFor(() => expect(deployButton).toBeEnabled());
    fireEvent.click(deployButton);

    await waitFor(() => {
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/deploy')).toBe(true);
    });
    fireEvent.click(screen.getByRole('tab', { name: 'Deploy' }));

    await waitFor(() => {
      expect(screen.getByLabelText('Tag profile')).toHaveValue('regulated');
      expect(screen.getByLabelText(/Environment/)).toHaveValue('staging-override');
    }, { timeout: 10_000 });

    // Returning from Chat remounts the governance fields, which deliberately
    // fail closed while they re-fetch and verify the current policy/profile
    // revisions. Wait for that verification before exercising the export.
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: 'Download CloudFormation Template' }),
      ).toBeEnabled();
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    );
    await waitFor(() => {
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/generate-cfn-template')).toBe(true);
    });
    expect(requestBody('/api/generate-cfn-template')).toMatchObject({
      tagProfile: 'regulated',
      resourceTags: { Environment: 'staging-override' },
      tagProfileUpdatedAt: PROFILE_UPDATED_AT,
    });
  }, 15_000);

  it('shows governance tags on a restored deployment where CFN download is available', async () => {
    const policyRevision = await computeTagPolicyRevision([TAG_POLICY]);
    useWorkflowStore.getState().setGovernance({
      version: 1,
      tags: {
        explicitValues: {},
        effectiveValues: { Environment: 'production' },
        profile: {
          name: 'regulated',
          updatedAt: PROFILE_UPDATED_AT,
        },
        policyRevision,
      },
      namingProfile: null,
    });
    mockGovernanceApi();
    render(
      <DeployPanel
        config={config}
        nodeId="node-1"
        isVisible
        onClose={() => {}}
        restoredDeployment={{
          runtimeId: 'runtime-restored',
          endpoint: 'https://runtime.example.invalid',
        }}
      />,
    );

    await screen.findByText('Chat with your Agent', {}, { timeout: 10_000 });
    fireEvent.click(screen.getByRole('tab', { name: 'Deploy' }));
    expect(await screen.findByLabelText(
      'Tag profile',
      {},
      { timeout: 10_000 },
    )).toHaveValue('regulated');
    expect(await screen.findByDisplayValue(
      'production',
      {},
      { timeout: 10_000 },
    )).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    ).toBeEnabled();
  });

  it('invalid CFN naming disables only the CFN action', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="node-1"
        isVisible
        onClose={() => {}}
      />,
    );
    await waitForGovernanceReady();

    fireEvent.change(screen.getByLabelText('Naming prefix'), {
      target: { value: 'ECB' },
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(/lowercase letter/i);
    expect(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    ).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Export as Python' })).toBeEnabled();
    expect(
      screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0],
    ).toBeEnabled();
  });

  it('keeps the naming draft when live deployment unmounts and remounts the fields', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="node-1"
        isVisible
        onClose={() => {}}
      />,
    );
    await waitForGovernanceReady();

    fireEvent.change(screen.getByLabelText('Naming prefix'), {
      target: { value: 'ecb' },
    });
    const deployButton = screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0];
    await waitFor(() => expect(deployButton).toBeEnabled());
    fireEvent.click(deployButton);

    await waitFor(() => {
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/deploy')).toBe(true);
    });

    fireEvent.click(screen.getByRole('tab', { name: 'Deploy' }));
    await waitFor(() => {
      expect(screen.getByLabelText('Naming prefix')).toHaveValue('ecb');
    }, { timeout: 10_000 });
    await waitForGovernanceReady();

    fireEvent.click(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    );
    await waitFor(() => {
      expect(requestBody('/api/generate-cfn-template').namingProfile).toEqual({
        prefix: 'ecb',
      });
    });
  }, 15_000);
});
