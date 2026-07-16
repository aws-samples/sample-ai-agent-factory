/**
 * Registry clone snapshot → canvas.
 *
 * A registry snapshot is a RAW React-Flow canvas ({name, nodes, edges} exactly
 * as the store holds it, captured verbatim at publish). Cloning must load those
 * nodes/edges DIRECTLY — NOT reinterpret them through the NL-generator's
 * {idSuffix, configuration, sourceIdSuffix} template-spec shape (which drops
 * every edge because those fields are undefined on real nodes, producing a
 * broken, unwired template).
 *
 * This pure helper deep-clones the snapshot (so canvas edits never mutate the
 * cached registry entry), clears transient UI flags, and returns instantiated
 * {nodes, edges} ready for the store's loadTemplate(). Pattern-agnostic: it
 * preserves whatever nodes/edges/config the publisher captured.
 */

import type { Edge } from '@xyflow/react';
import type { AgentCoreNode } from '../store/workflowStore';

export interface RawCanvasSnapshot {
  name?: string;
  nodes?: unknown[];
  edges?: unknown[];
}

export interface ClonedCanvas {
  nodes: AgentCoreNode[];
  edges: Edge[];
}

export function snapshotToCanvas(snapshot: RawCanvasSnapshot | null | undefined): ClonedCanvas {
  const rawNodes = Array.isArray(snapshot?.nodes) ? (snapshot!.nodes as AgentCoreNode[]) : [];
  const rawEdges = Array.isArray(snapshot?.edges) ? (snapshot!.edges as Edge[]) : [];

  // Deep-clone node + data so cloned-canvas edits never mutate the cached
  // snapshot; clear selection so the clone lands unselected.
  const nodes: AgentCoreNode[] = rawNodes.map((n) => ({
    ...n,
    data: { ...((n as AgentCoreNode).data ?? {}) },
    selected: false,
  }));

  // Edges reference node ids; clone REPLACES the canvas, so the snapshot's
  // internal ids are self-consistent and are preserved verbatim (no remap).
  const edges: Edge[] = rawEdges.map((e) => ({ ...(e as Edge), selected: false }));

  return { nodes, edges };
}
