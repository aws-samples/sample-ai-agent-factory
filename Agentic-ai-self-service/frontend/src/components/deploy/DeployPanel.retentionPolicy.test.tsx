import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RuntimeConfiguration } from '../../types/components';
import { useWorkflowStore } from '../../store/workflowStore';
import { DeployPanel } from './DeployPanel';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

const config: RuntimeConfiguration = {
  name: 'retention-runtime',
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

async function waitForReady() {
  await waitFor(() => {
    expect(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    ).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Export as Python' })).toBeEnabled();
  });
}

describe('DeployPanel CloudFormation data retention', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    useWorkflowStore.getState().resetWorkflowDocument(null);
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    mockAuthFetch.mockImplementation(async (url: string) => {
      if (url === '/api/settings/tags' || url === '/api/settings/tag-profiles') {
        return { ok: true, status: 200, json: async () => [] };
      }
      if (url === '/api/generate-cfn-template' || url === '/api/export-python') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            download_url: 'https://downloads.example.invalid/artifact.zip',
          }),
        };
      }
      if (url === '/api/deploy') {
        return {
          ok: true,
          status: 202,
          json: async () => ({
            success: true,
            runtimeId: 'runtime-1',
            endpoint: 'https://runtime.example.invalid',
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });
  });

  it('defaults to Retain and sends the policy only to CloudFormation export', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="node-1"
        isVisible
        onClose={() => {}}
      />,
    );
    await waitForReady();

    const retain = screen.getByRole('radio', { name: /Retain \(recommended\)/ });
    const remove = screen.getByRole('radio', { name: /Delete with stack/ });
    expect(retain).toBeChecked();
    expect(remove).not.toBeChecked();
    expect(retain).toHaveAccessibleDescription(
      /Applies only to the downloaded CloudFormation bundle/i,
    );

    fireEvent.click(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Export as Python' }));
    fireEvent.click(
      screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0],
    );

    await waitFor(() => {
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/generate-cfn-template')).toBe(true);
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/export-python')).toBe(true);
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/deploy')).toBe(true);
    });

    expect(requestBody('/api/generate-cfn-template').dataRetentionPolicy).toBe('Retain');
    expect(requestBody('/api/export-python')).not.toHaveProperty('dataRetentionPolicy');
    expect(requestBody('/api/deploy')).not.toHaveProperty('dataRetentionPolicy');
  });

  it('warns before exporting Delete and sends its exact backend enum', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="node-1"
        isVisible
        onClose={() => {}}
      />,
    );
    await waitForReady();

    const remove = screen.getByRole('radio', { name: /Delete with stack/ });
    fireEvent.click(remove);

    expect(remove).toBeChecked();
    const warning = screen.getByRole('status');
    expect(warning).toHaveTextContent(/permanently remove stack-owned data/i);
    expect(remove).toHaveAccessibleDescription(/Use Delete only for ephemeral or test environments/i);

    fireEvent.click(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    );
    await waitFor(() => {
      expect(requestBody('/api/generate-cfn-template').dataRetentionPolicy).toBe('Delete');
    });
  });

  it('preserves Delete through the Deploy to Chat to Deploy transition', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="node-1"
        isVisible
        onClose={() => {}}
      />,
    );
    await waitForReady();

    fireEvent.click(screen.getByRole('radio', { name: /Delete with stack/ }));
    fireEvent.click(
      screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0],
    );

    await waitFor(() => {
      expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/deploy')).toBe(true);
    });
    fireEvent.click(screen.getByRole('tab', { name: 'Deploy' }));
    await waitForReady();

    expect(screen.getByRole('radio', { name: /Delete with stack/ })).toBeChecked();
    fireEvent.click(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    );
    await waitFor(() => {
      expect(requestBody('/api/generate-cfn-template').dataRetentionPolicy).toBe('Delete');
    });
  });
});
