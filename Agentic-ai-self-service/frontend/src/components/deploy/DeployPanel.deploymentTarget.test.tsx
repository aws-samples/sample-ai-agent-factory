import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RuntimeConfiguration } from '../../types/components';
import { DeployPanel } from './DeployPanel';
import { useWorkflowStore } from '../../store/workflowStore';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

const config: RuntimeConfiguration = {
  name: 'target-runtime',
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

function requestBody(path: string): Record<string, unknown> {
  const call = mockAuthFetch.mock.calls.find(([url]) => url === path);
  if (!call) throw new Error(`No request made to ${path}`);
  return JSON.parse((call[1] as { body: string }).body);
}

describe('DeployPanel deployment target', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useWorkflowStore.getState().resetWorkflowDocument(null);
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    mockAuthFetch.mockImplementation(async (url: string) => {
      if (url === '/api/deploy-targets') {
        return {
          ok: true,
          json: async () => ({
            enabled: true,
            home_region: 'us-east-1',
            regions: ['eu-west-1'],
            accounts: [
              {
                account_id: '123456789012',
                region: 'eu-west-1',
              },
            ],
          }),
        };
      }
      if (url === '/api/settings/tags' || url === '/api/settings/tag-profiles') {
        return { ok: true, json: async () => [] };
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
      if (url === '/api/generate-cfn-template' || url === '/api/export-python') {
        return {
          ok: true,
          json: async () => ({
            download_url: 'https://downloads.example.invalid/artifact.zip',
          }),
        };
      }
      return { ok: true, json: async () => ({}) };
    });
  });

  it('sends the selected target only to live deployment', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="node-1"
        isVisible
        onClose={() => {}}
      />,
    );

    fireEvent.change(await screen.findByLabelText('Live deployment target'), {
      target: { value: 'account:123456789012:eu-west-1' },
    });
    const deployButton = screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0];
    await waitFor(() => expect(deployButton).toBeEnabled());
    fireEvent.click(deployButton);

    await waitFor(() => {
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/deploy')).toBe(true);
    });
    expect(requestBody('/api/deploy')).toMatchObject({
      targetAccountId: '123456789012',
      targetRegion: 'eu-west-1',
    });

    fireEvent.click(screen.getByRole('tab', { name: 'Deploy' }));
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: 'Download CloudFormation Template' }),
      ).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Export as Python' })).toBeEnabled();
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Export as Python' }));

    await waitFor(() => {
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/generate-cfn-template')).toBe(true);
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/export-python')).toBe(true);
    });
    expect(requestBody('/api/generate-cfn-template')).not.toHaveProperty('targetAccountId');
    expect(requestBody('/api/generate-cfn-template')).not.toHaveProperty('targetRegion');
    expect(requestBody('/api/export-python')).not.toHaveProperty('targetAccountId');
    expect(requestBody('/api/export-python')).not.toHaveProperty('targetRegion');
  });
});
