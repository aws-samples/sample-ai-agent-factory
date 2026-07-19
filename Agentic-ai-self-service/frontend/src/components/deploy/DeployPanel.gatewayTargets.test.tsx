/**
 * DeployPanel — mixed gateway targets end-to-end mapping test.
 *
 * Proves that when a gateway node carries a mixed `targets[]` (MCP servers +
 * Lambda + OpenAPI + Smithy), clicking Deploy POSTs:
 *   - `externalMcpServers` = every mcp_server entry, and
 *   - `gatewayConfig.targets` = only the non-MCP families (openapi/lambda/smithy),
 * so mcp_server entries are not double-deployed.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { DeployPanel } from './DeployPanel';
import type { RuntimeConfiguration, GatewayConfiguration } from '../../types/components';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

// workflowStore is used for node-execution state + registry publish snapshot.
vi.mock('../../store/workflowStore', () => ({
  useWorkflowStore: Object.assign(
    () => ({
      setNodeExecutionStateByType: vi.fn(),
      resetAllExecutionStates: vi.fn(),
    }),
    { getState: () => ({ nodes: [], edges: [] }) },
  ),
}));

const config: RuntimeConfiguration = {
  name: 'test-runtime',
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

const gatewayConfig: GatewayConfiguration = {
  name: 'multi-gw',
  targetType: 'lambda',
  targetConfig: { type: 'lambda', functionArn: 'arn:aws:lambda:us-west-2:123456789012:function:a' },
  targets: [
    { type: 'mcp_server', serverId: 'aws-knowledge' },
    { type: 'lambda', functionArn: 'arn:aws:lambda:us-west-2:123456789012:function:a' },
    { type: 'openapi', specUrl: 'https://api.example.com/openapi.json' },
    { type: 'smithy', modelName: 'dynamodb' },
  ],
  enableSemanticSearch: true,
};

describe('DeployPanel — mixed gateway targets mapping', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockAuthFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ success: true, runtimeId: 'r1', endpoint: 'https://e' }),
    });
  });

  it('POSTs externalMcpServers + non-MCP gatewayConfig.targets to /api/deploy', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="node-1"
        gatewayConfig={gatewayConfig}
        isVisible
        onClose={() => {}}
      />,
    );

    // Click the primary Deploy button (there are two — panel + footer).
    fireEvent.click(screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0]);

    await waitFor(() => {
      const deployCall = mockAuthFetch.mock.calls.find((c) => c[0] === '/api/deploy');
      expect(deployCall).toBeTruthy();
    });

    const deployCall = mockAuthFetch.mock.calls.find((c) => c[0] === '/api/deploy')!;
    const body = JSON.parse((deployCall[1] as { body: string }).body);

    // MCP entries flow through externalMcpServers.
    expect(body.externalMcpServers).toEqual([{ server_id: 'aws-knowledge' }]);

    // gatewayConfig.targets carries ONLY the non-MCP families.
    expect(body.gatewayConfig.targets.map((t: { type: string }) => t.type)).toEqual([
      'lambda',
      'openapi',
      'smithy',
    ]);
  });
});
