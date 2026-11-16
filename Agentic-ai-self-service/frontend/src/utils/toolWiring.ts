/**
 * Direct tool-to-runtime wiring.
 *
 * AgentCore tools are served to an agent through an AgentCore Gateway (MCP), so
 * there is no direct runtime-tool edge in the deploy path. Customers asked to be
 * able to "add a tool directly to the runtime", so when a tool node is connected
 * straight to a runtime on the canvas we transparently route it through a
 * Gateway: reuse the runtime's existing Gateway if it has one, otherwise create a
 * Gateway node and wire runtime -> gateway -> tool. The resulting graph is the
 * exact shape deriveCanvasDeployInputs already compiles (runtime -> gateway ->
 * tool), so the deploy pipeline is unchanged.
 *
 * This module is pure (no store, no React, no import.meta): inputs are arguments,
 * output is a plan the store applies atomically, so it is unit-testable directly.
 */
import type { Edge } from '@xyflow/react';
import type { AgentCoreNode } from '../store/workflowStore';
import type { GatewayConfiguration, RuntimeConfiguration } from '../types/components';
import { createDefaultGatewayConfig } from './gatewayConfig';
import { determineConnectionType } from './edges';

export interface ToolRuntimeWiringPlan {
  /** A Gateway node to add, or null when the runtime already has one to reuse. */
  gatewayNode: AgentCoreNode | null;
  /** Edges to add (runtime->gateway when the gateway is new, plus tool->gateway). */
  edgesToAdd: Edge[];
}

const EMPTY_PLAN: ToolRuntimeWiringPlan = { gatewayNode: null, edgesToAdd: [] };

function edgeConnects(edge: Edge, a: string, b: string): boolean {
  return (
    (edge.source === a && edge.target === b) || (edge.source === b && edge.target === a)
  );
}

function hasEdgeBetween(edges: Edge[], a: string, b: string): boolean {
  return edges.some((edge) => edgeConnects(edge, a, b));
}

/** A hyphen-style Gateway name derived from the runtime, so an auto-created gateway
 *  does not reuse one fixed default name across runtimes. This is a friendly label
 *  only: it reduces (does not prove) name collisions, and the backend — not this
 *  name — owns resource identity, ownership and tenant isolation. Falls back to
 *  "agent-tools". */
export function deriveGatewayName(runtime: AgentCoreNode): string {
  const cfg = runtime.data.configuration as RuntimeConfiguration | undefined;
  const raw = (cfg?.name || runtime.data.label || 'agent').trim();
  const base = raw
    .replace(/[^a-zA-Z0-9-]/g, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
    .replace(/-+$/g, '');
  return `${base || 'agent'}-tools`;
}

function makeEdge(source: string, target: string, sourceType: string, targetType: string): Edge {
  return {
    id: `edge-${source}-${target}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    source,
    target,
    type: 'connection',
    data: { connectionType: determineConnectionType(sourceType as never, targetType as never) },
  };
}

/**
 * Plan the wiring needed to attach `toolNodeId` to `runtimeNodeId`.
 *
 * Returns a no-op plan when the tool is already served to the runtime's gateway.
 */
export function planToolToRuntimeWiring(
  nodes: AgentCoreNode[],
  edges: Edge[],
  toolNodeId: string,
  runtimeNodeId: string,
): ToolRuntimeWiringPlan {
  const runtime = nodes.find((n) => n.id === runtimeNodeId);
  const tool = nodes.find((n) => n.id === toolNodeId);
  if (!runtime || !tool) return EMPTY_PLAN;
  if (runtime.data.componentType !== 'runtime' || tool.data.componentType !== 'tool') {
    return EMPTY_PLAN;
  }

  // An existing gateway directly connected to this runtime is reused.
  const existingGateway = nodes.find(
    (n) =>
      n.data.componentType === 'gateway' &&
      edges.some((e) => edgeConnects(e, n.id, runtimeNodeId)),
  );

  if (existingGateway) {
    if (hasEdgeBetween(edges, toolNodeId, existingGateway.id)) {
      return EMPTY_PLAN; // tool already served to this gateway
    }
    return {
      gatewayNode: null,
      edgesToAdd: [makeEdge(toolNodeId, existingGateway.id, 'tool', 'gateway')],
    };
  }

  // No gateway yet: create one between the tool and the runtime.
  const gatewayId = `gateway-${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
  const midX = Math.round((tool.position.x + runtime.position.x) / 2);
  const midY = Math.round((tool.position.y + runtime.position.y) / 2);
  const gatewayNode: AgentCoreNode = {
    id: gatewayId,
    type: 'gateway',
    position: { x: midX, y: midY },
    selected: false,
    data: {
      label: 'AgentCore Gateway',
      componentType: 'gateway',
      configuration: {
        ...createDefaultGatewayConfig(),
        name: deriveGatewayName(runtime),
      } as GatewayConfiguration,
      validationStatus: 'valid',
    },
  };

  return {
    gatewayNode,
    edgesToAdd: [
      makeEdge(runtimeNodeId, gatewayId, 'runtime', 'gateway'),
      makeEdge(toolNodeId, gatewayId, 'tool', 'gateway'),
    ],
  };
}
