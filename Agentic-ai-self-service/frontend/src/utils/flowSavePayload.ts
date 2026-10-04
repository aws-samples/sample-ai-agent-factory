/**
 * Builds the backend WorkflowDefinition payload for an autosave of the canvas.
 *
 * Extracted from useAutoSave so the flow store can re-submit the current canvas
 * after a version conflict the user chose to overwrite (F-15) through the same
 * serialiser the debounced save uses, rather than a second one that could drift.
 */

import type { Edge } from '@xyflow/react';
import { stripWriteOnlyCredentials } from './credentialScrub';
import type { AgentCoreNode, WorkflowState } from '../store/workflowStore';
import { getDeploymentRegion } from './awsRegion';
import { normalizeDeploymentGovernance } from '../types/workflow';

/**
 * Converts a camelCase key to snake_case.
 */
function toSnakeCase(str: string): string {
  return str.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`);
}

/**
 * Recursively converts all object keys from camelCase to snake_case.
 */
function keysToSnakeCase(obj: unknown): unknown {
  if (Array.isArray(obj)) {
    return obj.map(keysToSnakeCase);
  }
  if (obj !== null && typeof obj === 'object') {
    const result: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
      result[toSnakeCase(key)] = keysToSnakeCase(value);
    }
    return result;
  }
  return obj;
}

/**
 * Converts React Flow nodes to the backend ComponentNode format.
 * Only includes nodes that have a configuration set.
 * Converts all keys to snake_case to match backend Pydantic models.
 */
export function toBackendNodes(nodes: AgentCoreNode[]): unknown[] {
  return nodes
    .filter((node) => node.data?.configuration)
    .map((node) => {
      // Write-only credentials never reach the flow store: strip before snake-casing (both spellings are covered).
      const config = stripWriteOnlyCredentials(keysToSnakeCase(node.data.configuration)) as Record<string, unknown>;
      // Ensure component_type is set (discriminator field)
      if (!config.component_type) {
        config.component_type = node.data.componentType;
      }
      return {
        id: node.id,
        type: node.data.componentType,
        position: { x: node.position?.x ?? 0, y: node.position?.y ?? 0 },
        data: config,
        selected: node.selected ?? false,
        validation_status: node.data?.validationStatus ?? 'pending',
      };
    });
}

/**
 * Maps frontend connection types to backend ConnectionType enum values.
 */
const CONNECTION_TYPE_MAP: Record<string, string> = {
  data: 'data',
  identity: 'authentication',
  tool: 'data',
  authentication: 'authentication',
  policy: 'policy',
};

/**
 * Converts React Flow edges to the backend ConnectionEdge format.
 * Backend requires: source_handle (non-empty string), target_handle (non-empty string), type (ConnectionType)
 */
export function toBackendEdges(edges: Edge[]): unknown[] {
  return edges.map((edge) => {
    const edgeData = edge.data as Record<string, unknown> | undefined;
    const frontendType = (edgeData?.connectionType as string) || 'data';
    const backendType = CONNECTION_TYPE_MAP[frontendType] || 'data';

    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      source_handle: edge.sourceHandle || 'output',
      target_handle: edge.targetHandle || 'input',
      type: backendType,
      animated: false,
    };
  });
}

export type FlowSavePayloadSource = Pick<WorkflowState, 'nodes' | 'edges' | 'viewport' | 'governance'>;

/** The backend workflow document for the canvas `state` of flow `flowId`. */
export function buildFlowSavePayload(flowId: string, state: FlowSavePayloadSource) {
  const now = new Date().toISOString();
  return {
    id: flowId,
    name: 'auto-save',
    description: '',
    version: '1.0.0',
    nodes: toBackendNodes(state.nodes),
    edges: toBackendEdges(state.edges),
    viewport: {
      x: state.viewport.x,
      y: state.viewport.y,
      zoom: state.viewport.zoom,
    },
    metadata: {
      author: 'system',
      tags: [],
      aws_region: getDeploymentRegion(),
      deployment_status: 'not_deployed',
    },
    governance: normalizeDeploymentGovernance(state.governance),
    created_at: now,
    updated_at: now,
  };
}
