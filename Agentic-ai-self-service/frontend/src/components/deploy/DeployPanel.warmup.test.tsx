/**
 * The post-deploy warmup, as the rendered panel actually sends it.
 *
 * A Memory agent stores every real invocation, so the ping DeployPanel sends right after
 * a deploy must identify itself: both generated Memory entrypoints return on
 * `warmup: true` before Memory and the model (backend
 * tests/test_the_warmup_is_not_a_memory_turn.py). This clicks Deploy on a Memory canvas
 * and reads the /api/test-runtime request the panel made.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { DeployPanel } from './DeployPanel';
import type { RuntimeConfiguration } from '../../types/components';
import { useWorkflowStore } from '../../store/workflowStore';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

const config: RuntimeConfiguration = {
  name: 'memory-runtime',
  entrypoint: 'agent.py',
  framework: 'strands_agents',
  model: { provider: 'bedrock', modelId: 'm', temperature: 0.7, topP: 0.9 },
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

describe('DeployPanel post-deploy warmup', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useWorkflowStore.getState().resetWorkflowDocument(null);
    mockAuthFetch.mockImplementation(async (url: string) => {
      if (url === '/api/deploy') {
        return {
          ok: true,
          json: async () => ({ success: true, runtimeId: 'memory_runtime_AbCdEf1234', endpoint: 'https://e' }),
        };
      }
      const empty = url === '/api/settings/tags' || url === '/api/settings/tag-profiles' ? [] : {};
      return { ok: true, json: async () => empty };
    });
  });

  it('warms a Memory runtime once, with the warmup marker', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="node-1"
        connectedTools={['memory']}
        memoryConfig={{ enabled: true }}
        isVisible
        onClose={() => {}}
      />,
    );

    const deployButton = screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0];
    await waitFor(() => expect(deployButton).toBeEnabled());
    fireEvent.click(deployButton);

    await waitFor(() => {
      expect(mockAuthFetch.mock.calls.some((c) => c[0] === '/api/test-runtime')).toBe(true);
    });
    const warmups = mockAuthFetch.mock.calls.filter((c) => c[0] === '/api/test-runtime');
    expect(warmups).toHaveLength(1);
    const body = JSON.parse((warmups[0][1] as { body: string }).body);
    expect(body).toEqual({ endpoint: 'https://e', input: 'ping', runtimeId: 'memory_runtime_AbCdEf1234', warmup: true });
  });
});
