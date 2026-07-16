/**
 * Blueprint summary — turns a registry entry's raw canvas snapshot into a
 * component list + wiring summary for the detail view's Components tab (our
 * analogue of the reference registry's "Tools" tab).
 *
 * Pure + defensive: snapshots are raw React-Flow canvases captured at publish
 * ({name, nodes, edges}), so a node's kind may live on `node.type` or
 * `node.data.componentType`, its name on `node.data.label` or its config's
 * `name`. We read whichever is present and never throw on malformed input.
 */

import type { RegistryCanvasSnapshot } from '../../../services/api';

export interface ComponentRow {
  /** Node id (stable key). */
  id: string;
  /** Component kind: runtime, gateway, memory, tool, knowledge_base, … */
  type: string;
  /** Human label (config name or node label). */
  label: string;
  /** One-line config highlight (e.g. model id, tool id, gateway name). */
  detail: string;
}

export interface WiringRow {
  source: string;
  target: string;
  /** Resolved source/target component labels for display. */
  sourceLabel: string;
  targetLabel: string;
}

export interface BlueprintSummary {
  components: ComponentRow[];
  wiring: WiringRow[];
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' ? (v as Record<string, unknown>) : {};
}

function str(v: unknown): string | undefined {
  return typeof v === 'string' && v ? v : undefined;
}

function nodeType(node: Record<string, unknown>, data: Record<string, unknown>): string {
  return str(data.componentType) || str(node.type) || 'component';
}

function nodeLabel(
  node: Record<string, unknown>,
  data: Record<string, unknown>,
  config: Record<string, unknown>,
): string {
  return str(config.name) || str(data.label) || str(node.id) || 'unnamed';
}

/** Pull a compact, human-useful config highlight per component type. */
function nodeDetail(type: string, config: Record<string, unknown>): string {
  const pick = (...keys: string[]): string | undefined => {
    for (const k of keys) {
      const v = config[k];
      if (typeof v === 'string' && v) return v;
      if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    }
    return undefined;
  };
  switch (type) {
    case 'runtime':
      return pick('modelId', 'model', 'protocol') || 'agent runtime';
    case 'tool':
      return pick('toolId', 'toolName', 'name') || 'tool';
    case 'gateway':
      return pick('name', 'gatewayName') || 'MCP gateway';
    case 'memory':
      return pick('strategy', 'memoryMode') || 'memory enabled';
    case 'knowledge_base':
    case 'knowledgeBase':
      return pick('name', 'vectorStore', 'dataSourceType') || 'knowledge base';
    case 'guardrail':
      return pick('name', 'mode') || 'guardrail';
    case 'policy':
      return pick('mode', 'engine') || 'policy engine';
    default: {
      const name = pick('name');
      return name || type;
    }
  }
}

export function summarizeBlueprint(
  snapshot: RegistryCanvasSnapshot | null | undefined,
): BlueprintSummary {
  const rawNodes = Array.isArray(snapshot?.nodes) ? snapshot!.nodes : [];
  const rawEdges = Array.isArray(snapshot?.edges) ? snapshot!.edges : [];

  const labelById = new Map<string, string>();
  const components: ComponentRow[] = rawNodes.map((n) => {
    const node = asRecord(n);
    const data = asRecord(node.data);
    const config = asRecord(data.configuration ?? data.config);
    const id = str(node.id) || `node-${labelById.size}`;
    const type = nodeType(node, data);
    const label = nodeLabel(node, data, config);
    labelById.set(id, label);
    return { id, type, label, detail: nodeDetail(type, config) };
  });

  const wiring: WiringRow[] = rawEdges.map((e) => {
    const edge = asRecord(e);
    const source = str(edge.source) || '';
    const target = str(edge.target) || '';
    return {
      source,
      target,
      sourceLabel: labelById.get(source) || source || '—',
      targetLabel: labelById.get(target) || target || '—',
    };
  });

  return { components, wiring };
}
