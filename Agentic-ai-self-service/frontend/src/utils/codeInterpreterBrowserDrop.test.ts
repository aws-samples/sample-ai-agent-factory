/**
 * Regression: a Code Interpreter or Browser node dropped from the palette used
 * to arrive with no configuration, so validateWorkflow immediately marked it
 * "Component configuration is required" and blocked the whole deploy, with no
 * dialog to fix it. These tests pin that the node now drops with a valid default
 * configuration, validates clean, and is deployable when wired to a runtime.
 */
import { describe, it, expect } from 'vitest';
import { createNodeFromDrop } from './dragDrop';
import { validateWorkflow, type WorkflowNode, type WorkflowEdge } from './validation';
import type { CodeInterpreterConfiguration, BrowserConfiguration } from '../types/components';

describe('Code Interpreter / Browser node drop', () => {
  it.each(['code_interpreter', 'browser'] as const)(
    'seeds a valid default configuration on drop for %s',
    (componentType) => {
      const node = createNodeFromDrop(componentType, { x: 0, y: 0 });
      expect(node.data.configuration).toBeDefined();
      const cfg = node.data.configuration as CodeInterpreterConfiguration | BrowserConfiguration;
      expect(typeof cfg.name).toBe('string');
      expect(cfg.name.length).toBeGreaterThan(0);
      expect(cfg.enabled).toBe(true);
      expect(node.data.validationStatus).toBe('valid');
    },
  );

  it('validates a runtime with connected Code Interpreter and Browser as ready to deploy', () => {
    const runtime = createNodeFromDrop('runtime', { x: 0, y: 0 });
    runtime.data.configuration = {
      name: 'MyAgent',
      framework: 'strands_agents',
      model: { provider: 'bedrock', modelId: 'us.anthropic.claude-sonnet-5', temperature: 0.7, topP: 0.9 },
      systemPrompt: 'You are a helpful agent.',
      entrypoint: 'agent.py',
      deploymentType: 'direct_code_deploy',
      pythonRuntime: 'PYTHON_3_13',
      protocol: 'HTTP',
    } as never;
    const ci = createNodeFromDrop('code_interpreter', { x: 200, y: 0 });
    const br = createNodeFromDrop('browser', { x: 200, y: 120 });

    const nodes: WorkflowNode[] = [runtime, ci, br].map((n) => ({
      id: n.id,
      type: n.data.componentType,
      data: n.data,
    }));
    const edges: WorkflowEdge[] = [
      { id: `e-ci`, source: runtime.id, target: ci.id },
      { id: `e-br`, source: runtime.id, target: br.id },
    ];

    const result = validateWorkflow(nodes, edges);
    expect(result.nodeStates.get(ci.id)?.status).toBe('valid');
    expect(result.nodeStates.get(br.id)?.status).toBe('valid');
    expect(result.isValid).toBe(true);
    expect(result.isReadyToDeploy).toBe(true);
  });
});
