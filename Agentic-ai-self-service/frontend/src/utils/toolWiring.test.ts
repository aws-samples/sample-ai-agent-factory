/**
 * Tests for direct tool-to-runtime wiring (auto-gateway).
 */
import { describe, it, expect } from 'vitest';
import { planToolToRuntimeWiring, deriveGatewayName } from './toolWiring';
import { deriveCanvasDeployInputs } from './deployRequestFromCanvas';
import { createDefaultGatewayConfig } from './gatewayConfig';
import type { AgentCoreNode } from '../store/workflowStore';
import type { Edge } from '@xyflow/react';

function runtimeNode(id = 'runtime-1', name = 'My Agent'): AgentCoreNode {
  return {
    id,
    type: 'runtime',
    position: { x: 0, y: 0 },
    selected: false,
    data: {
      label: 'Runtime',
      componentType: 'runtime',
      configuration: { name } as never,
      validationStatus: 'valid',
    },
  };
}

function toolNode(id = 'tool-1', toolId = 'web_search'): AgentCoreNode {
  return {
    id,
    type: 'tool',
    position: { x: 400, y: 0 },
    selected: false,
    data: {
      label: 'Amazon Web Search',
      componentType: 'tool',
      configuration: { name: toolId, toolId, description: '', enabled: true } as never,
      validationStatus: 'valid',
    },
  };
}

function gatewayNode(id = 'gateway-1'): AgentCoreNode {
  return {
    id,
    type: 'gateway',
    position: { x: 200, y: 0 },
    selected: false,
    data: {
      label: 'AgentCore Gateway',
      componentType: 'gateway',
      configuration: { name: 'my-agent-tools', gatewayProvider: 'agentcore', enableSemanticSearch: true } as never,
      validationStatus: 'valid',
    },
  };
}

describe('planToolToRuntimeWiring', () => {
  it('creates a gateway and wires runtime->gateway->tool when the runtime has none', () => {
    const rt = runtimeNode();
    const tool = toolNode();
    const plan = planToolToRuntimeWiring([rt, tool], [], tool.id, rt.id);

    expect(plan.gatewayNode).not.toBeNull();
    expect(plan.gatewayNode!.data.componentType).toBe('gateway');
    const gwName = (plan.gatewayNode!.data.configuration as { name?: string }).name;
    expect(gwName && gwName.length).toBeTruthy();
    expect(plan.edgesToAdd).toHaveLength(2);

    const gwId = plan.gatewayNode!.id;
    const connects = (a: string, b: string) =>
      plan.edgesToAdd.some((e) => (e.source === a && e.target === b) || (e.source === b && e.target === a));
    expect(connects(rt.id, gwId)).toBe(true);
    expect(connects(tool.id, gwId)).toBe(true);
  });

  it('reuses an existing gateway already connected to the runtime', () => {
    const rt = runtimeNode();
    const gw = gatewayNode();
    const tool = toolNode();
    const edges: Edge[] = [{ id: 'e1', source: rt.id, target: gw.id, type: 'connection' }];

    const plan = planToolToRuntimeWiring([rt, gw, tool], edges, tool.id, rt.id);
    expect(plan.gatewayNode).toBeNull();
    expect(plan.edgesToAdd).toHaveLength(1);
    expect(plan.edgesToAdd[0].source).toBe(tool.id);
    expect(plan.edgesToAdd[0].target).toBe(gw.id);
  });

  it('is a no-op when the tool is already served by the runtime gateway', () => {
    const rt = runtimeNode();
    const gw = gatewayNode();
    const tool = toolNode();
    const edges: Edge[] = [
      { id: 'e1', source: rt.id, target: gw.id, type: 'connection' },
      { id: 'e2', source: tool.id, target: gw.id, type: 'connection' },
    ];
    const plan = planToolToRuntimeWiring([rt, gw, tool], edges, tool.id, rt.id);
    expect(plan.gatewayNode).toBeNull();
    expect(plan.edgesToAdd).toHaveLength(0);
  });

  it('refuses when the ids are not a tool and a runtime', () => {
    const rt = runtimeNode();
    const rt2 = runtimeNode('runtime-2', 'Other');
    expect(planToolToRuntimeWiring([rt, rt2], [], rt2.id, rt.id)).toEqual({
      gatewayNode: null,
      edgesToAdd: [],
    });
  });

  it('derives a non-empty hyphen-safe gateway name from the runtime', () => {
    expect(deriveGatewayName(runtimeNode('r', 'My Agent Runtime'))).toBe('My-Agent-Runtime-tools');
    // Falls back to the node label when the config name is blank...
    expect(deriveGatewayName(runtimeNode('r', ''))).toBe('Runtime-tools');
    // ...and to "agent" when neither name nor label is usable.
    const bare = runtimeNode('r', '');
    bare.data.label = '';
    expect(deriveGatewayName(bare)).toBe('agent-tools');
  });

  it('the auto-created gateway introduces no privileged default beyond a hand-placed gateway', () => {
    const rt = runtimeNode();
    const tool = toolNode();
    const plan = planToolToRuntimeWiring([rt, tool], [], tool.id, rt.id);
    const autoCfg = plan.gatewayNode!.data.configuration as unknown as Record<string, unknown>;
    const handPlaced = createDefaultGatewayConfig() as unknown as Record<string, unknown>;

    // The auto gateway is exactly the hand-placed default with only its name set:
    // no extra credential, provider override, target, or wildcard is introduced.
    const autoRest = { ...autoCfg };
    const handRest = { ...handPlaced };
    delete autoRest.name;
    delete handRest.name;
    expect(autoRest).toEqual(handRest);
    expect(autoCfg.gatewayProvider).toBe('agentcore');
    expect(autoCfg.litellmApiKey).toBeUndefined();
    expect(autoCfg.litellmBaseUrl).toBeUndefined();
    // Name is a non-empty friendly label (backend owns real identity/ownership).
    expect(typeof autoCfg.name).toBe('string');
    expect((autoCfg.name as string).length).toBeGreaterThan(0);
  });

  it('produces a graph that compiles to a gateway-served deploy request', () => {
    const rt = runtimeNode();
    const tool = toolNode('tool-1', 'web_search');
    const plan = planToolToRuntimeWiring([rt, tool], [], tool.id, rt.id);
    const nodes = [rt, tool, plan.gatewayNode!];
    const inputs = deriveCanvasDeployInputs(rt.id, nodes, plan.edgesToAdd, {});

    expect(inputs.tools).toContain('gateway');
    expect(inputs.gatewayConfig).not.toBeNull();
    expect(inputs.gatewayTools).toContain('web_search');
  });
});
