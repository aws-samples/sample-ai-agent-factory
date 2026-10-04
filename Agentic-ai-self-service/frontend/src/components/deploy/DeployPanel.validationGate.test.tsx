/**
 * Live, 2026-09-28: a gateway with no target read "Ready to deploy", the deploy request went
 * out, and the deployer refused it minutes later while the canvas's "1 Error" badge sat behind
 * this panel. Deploy, the CloudFormation export and the Python export all send the canvas, so
 * all three must wait for a valid one, and the disabled control must say why.
 */
import { act, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RuntimeConfiguration } from '../../types/components';
import { useWorkflowStore } from '../../store/workflowStore';
import type { WorkflowValidationState } from '../../utils/validation';
import { DeployPanel } from './DeployPanel';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

const config: RuntimeConfiguration = {
  name: 'gated-runtime',
  entrypoint: 'agent.py',
  framework: 'strands_agents',
  model: { provider: 'bedrock', modelId: 'us.anthropic.claude-sonnet-5', temperature: 0.7, topP: 0.9 },
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

const GATEWAY_MESSAGE =
  'This gateway has nothing to serve. Add a Lambda, OpenAPI or MCP target in its configuration, '
  + 'or connect a tool or an MCP server runtime to it.';

function verdict(isValid: boolean): WorkflowValidationState {
  const errors = isValid
    ? []
    : [{ componentId: 'gw', field: 'targets', message: GATEWAY_MESSAGE, severity: 'error' as const }];
  return {
    isValid,
    isReadyToDeploy: isValid,
    nodeStates: new Map(),
    edgeStates: new Map(),
    errors,
    warnings: [],
  };
}

describe('DeployPanel honours the canvas validation verdict', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    useWorkflowStore.getState().resetWorkflowDocument(null);
    mockAuthFetch.mockImplementation(async (url: string) => {
      if (url === '/api/settings/tags' || url === '/api/settings/tag-profiles') {
        return { ok: true, status: 200, json: async () => [] };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });
  });

  // The first test in this file pays the panel's cold import and first render; under the full
  // suite's worker contention that alone exceeded the default 5 s budget (the same test passes
  // in 1 s alone, and the two tests after it in 0.2 s each).
  it('disables deploy and both exports on a red canvas, and names the first error', { timeout: 30_000 }, async () => {
    // Readiness is proven POSITIVELY first: with a valid verdict the CTA must become enabled
    // (governance loaded, config present). Only then is the verdict flipped, so a disabled
    // button afterwards can have exactly one cause. A mutant that dropped the gate survived a
    // version of this test that asserted "disabled" before governance had settled.
    useWorkflowStore.setState({ validationState: verdict(true), isReadyToDeploy: true });
    render(<DeployPanel config={config} nodeId="node-1" isVisible onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0]).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Download CloudFormation Template' })).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Export as Python' })).toBeEnabled();
    }, { timeout: 15_000 });

    act(() => {
      useWorkflowStore.setState({ validationState: verdict(false), isReadyToDeploy: false });
    });

    await waitFor(() => {
      const deploy = screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0];
      expect(deploy).toBeDisabled();
      expect(deploy).toHaveAttribute('title', `Fix the canvas first: ${GATEWAY_MESSAGE}`);
    }, { timeout: 15_000 });
    expect(screen.getByRole('button', { name: 'Download CloudFormation Template' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Download CloudFormation Template' })).toHaveAttribute(
      'title',
      `Fix the canvas first: ${GATEWAY_MESSAGE}`,
    );
    expect(screen.getByRole('button', { name: 'Export as Python' })).toBeDisabled();
    expect(mockAuthFetch.mock.calls.some(([url]) => url === '/api/deploy')).toBe(false);
  });

  it('enables them once the verdict is valid', async () => {
    useWorkflowStore.setState({ validationState: verdict(true), isReadyToDeploy: true });
    render(<DeployPanel config={config} nodeId="node-1" isVisible onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Download CloudFormation Template' })).toBeEnabled();
    }, { timeout: 5_000 });
    const deploy = screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0];
    expect(deploy).toBeEnabled();
    expect(deploy).not.toHaveAttribute('title');
    expect(screen.getByRole('button', { name: 'Export as Python' })).toBeEnabled();
  });

  it('does not block a canvas that has never been validated', async () => {
    // Every canvas mutation runs validation, so a null verdict is an empty store; the gate
    // must not invent a refusal the validator never issued.
    useWorkflowStore.setState({ validationState: null, isReadyToDeploy: false });
    render(<DeployPanel config={config} nodeId="node-1" isVisible onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Download CloudFormation Template' })).toBeEnabled();
    }, { timeout: 5_000 });
    expect(screen.getAllByRole('button', { name: /Deploy to AgentCore/i })[0]).toBeEnabled();
  });
});
