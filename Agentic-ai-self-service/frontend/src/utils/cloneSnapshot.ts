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

import type { Edge, Viewport } from '@xyflow/react';
import type { AgentCoreNode } from '../store/workflowStore';
import {
  createEmptyDeploymentGovernance,
  normalizeDeploymentGovernance,
  type DeploymentGovernanceV1,
} from '../types/workflow';

export interface RawCanvasSnapshot {
  schemaVersion?: number;
  name?: string;
  nodes?: unknown[];
  edges?: unknown[];
  viewport?: unknown;
  governance?: unknown;
}

export interface ClonedCanvas {
  nodes: AgentCoreNode[];
  edges: Edge[];
  viewport: Viewport;
  governance: DeploymentGovernanceV1;
}

function cloneSnapshotValue<T>(value: T): T {
  if (typeof structuredClone === 'function') return structuredClone(value);
  // Registry snapshots are JSON payloads, so this fallback is equivalent on the
  // browser versions that predate structuredClone.
  return JSON.parse(JSON.stringify(value)) as T;
}

export function snapshotToCanvas(snapshot: RawCanvasSnapshot | null | undefined): ClonedCanvas {
  const rawNodes = Array.isArray(snapshot?.nodes) ? (snapshot!.nodes as AgentCoreNode[]) : [];
  const rawEdges = Array.isArray(snapshot?.edges) ? (snapshot!.edges as Edge[]) : [];
  const schemaVersion = snapshot?.schemaVersion ?? 1;
  const rawViewport = snapshot?.viewport;
  const hasValidViewport = (
    rawViewport !== null
    && typeof rawViewport === 'object'
    && !Array.isArray(rawViewport)
    && typeof (rawViewport as Record<string, unknown>).x === 'number'
    && Number.isFinite((rawViewport as Record<string, unknown>).x)
    && typeof (rawViewport as Record<string, unknown>).y === 'number'
    && Number.isFinite((rawViewport as Record<string, unknown>).y)
    && typeof (rawViewport as Record<string, unknown>).zoom === 'number'
    && Number.isFinite((rawViewport as Record<string, unknown>).zoom)
    && Number((rawViewport as Record<string, unknown>).zoom) >= 0.1
    && Number((rawViewport as Record<string, unknown>).zoom) <= 4
  );
  if (schemaVersion === 2 && !hasValidViewport) {
    throw new Error('Registry snapshot schemaVersion 2 requires a valid viewport');
  }
  const viewport: Viewport = hasValidViewport
    ? cloneSnapshotValue(rawViewport as Viewport)
    : { x: 0, y: 0, zoom: 1 };

  let governance: DeploymentGovernanceV1;
  if (schemaVersion === 1) {
    // V1 snapshots predate governance. Do not reinterpret an unversioned extra
    // property as deployment authority.
    governance = createEmptyDeploymentGovernance();
  } else if (schemaVersion === 2) {
    if (!snapshot || !Object.prototype.hasOwnProperty.call(snapshot, 'governance')) {
      throw new Error('Registry snapshot schemaVersion 2 requires governance metadata');
    }
    governance = normalizeDeploymentGovernance(snapshot.governance);
  } else {
    throw new Error(`Unsupported registry snapshot schemaVersion: ${schemaVersion}`);
  }

  // Snapshots are JSON values. Clone the complete nested configuration, not only
  // the first data object: a later edit to config.model/etc must never mutate the
  // cached registry entry.
  const nodes: AgentCoreNode[] = rawNodes.map((n) => ({
    ...cloneSnapshotValue(n),
    selected: false,
  }));

  // Edges reference node ids; clone REPLACES the canvas, so the snapshot's
  // internal ids are self-consistent and are preserved verbatim (no remap).
  const edges: Edge[] = rawEdges.map((e) => ({ ...cloneSnapshotValue(e), selected: false }));

  return { nodes, edges, viewport, governance };
}
