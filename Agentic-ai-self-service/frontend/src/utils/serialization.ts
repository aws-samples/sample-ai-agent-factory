/**
 * WorkflowSerializer service for workflow persistence.
 * Handles serialization to JSON and deserialization from JSON.
 * Requirements: 9.6, 14.1, 14.2
 */

import type { Viewport } from '@xyflow/react';
import type { AgentCoreNode } from '../store/workflowStore';
import type { Edge } from '@xyflow/react';
import type {
  AgentCoreComponentType,
  ValidationStatus,
  DeploymentStatus,
} from '../types/workflow';

// ============================================================================
// Serialized Types (JSON-safe versions)
// ============================================================================

export interface SerializedWorkflow {
  id: string;
  name: string;
  description: string;
  version: string;
  nodes: SerializedNode[];
  edges: SerializedEdge[];
  viewport: SerializedViewport;
  metadata: SerializedMetadata;
  createdAt: string;
  updatedAt: string;
}

export interface SerializedNode {
  id: string;
  type: AgentCoreComponentType;
  position: { x: number; y: number };
  data: Record<string, unknown>;
  selected: boolean;
  validationStatus: ValidationStatus;
}

export interface SerializedEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string;
  targetHandle?: string;
  type?: string;
  animated?: boolean;
  data?: Record<string, unknown>;
  selected?: boolean;
}

export interface SerializedViewport {
  x: number;
  y: number;
  zoom: number;
}

export interface SerializedMetadata {
  author: string;
  tags: string[];
  awsRegion: string;
  deploymentStatus: DeploymentStatus;
  lastDeployedAt?: string;
  endpointUrl?: string;
}

// ============================================================================
// Validation Errors
// ============================================================================

export interface SerializationError {
  field: string;
  message: string;
}

// ============================================================================
// WorkflowSerializer Class
// ============================================================================

export class WorkflowSerializer {
  /**
   * Serializes a workflow to a JSON string.
   * Requirement 9.6: THE Workflow_Canvas SHALL serialize workflow state to JSON format
   * Requirement 14.1: WHEN a user exports a workflow, THE Workflow_Canvas SHALL generate a JSON representation
   */
  static serialize(
    nodes: AgentCoreNode[],
    edges: Edge[],
    viewport: Viewport,
    metadata?: Partial<SerializedMetadata>,
    workflowInfo?: { id?: string; name?: string; description?: string; version?: string }
  ): string {
    const serializedWorkflow = this.toSerializedWorkflow(nodes, edges, viewport, metadata, workflowInfo);
    return JSON.stringify(serializedWorkflow, null, 2);
  }

  /**
   * Converts workflow data to a serializable object.
   */
  static toSerializedWorkflow(
    nodes: AgentCoreNode[],
    edges: Edge[],
    viewport: Viewport,
    metadata?: Partial<SerializedMetadata>,
    workflowInfo?: { id?: string; name?: string; description?: string; version?: string }
  ): SerializedWorkflow {
    const now = new Date().toISOString();

    return {
      id: workflowInfo?.id ?? generateId(),
      name: workflowInfo?.name ?? 'Untitled Workflow',
      description: workflowInfo?.description ?? '',
      version: workflowInfo?.version ?? '1.0.0',
      nodes: nodes.map(this.serializeNode),
      edges: edges.map(this.serializeEdge),
      viewport: this.serializeViewport(viewport),
      metadata: this.serializeMetadata(metadata),
      createdAt: now,
      updatedAt: now,
    };
  }

  /**
   * Deserializes a JSON string to workflow data.
   * Requirement 14.2: WHEN a user imports a workflow JSON, THE Workflow_Canvas SHALL reconstruct the workflow
   */
  static deserialize(json: string): {
    nodes: AgentCoreNode[];
    edges: Edge[];
    viewport: Viewport;
    metadata: SerializedMetadata;
    workflowInfo: { id: string; name: string; description: string; version: string };
  } {
    const parsed = JSON.parse(json) as SerializedWorkflow;
    return this.fromSerializedWorkflow(parsed);
  }

  /**
   * Converts a serialized workflow object back to workflow data.
   */
  static fromSerializedWorkflow(serialized: SerializedWorkflow): {
    nodes: AgentCoreNode[];
    edges: Edge[];
    viewport: Viewport;
    metadata: SerializedMetadata;
    workflowInfo: { id: string; name: string; description: string; version: string };
  } {
    return {
      nodes: serialized.nodes.map(this.deserializeNode),
      edges: serialized.edges.map(this.deserializeEdge),
      viewport: this.deserializeViewport(serialized.viewport),
      metadata: serialized.metadata,
      workflowInfo: {
        id: serialized.id,
        name: serialized.name,
        description: serialized.description,
        version: serialized.version,
      },
    };
  }

  /**
   * Validates a JSON string against the workflow schema.
   * Requirement 14.3: THE Workflow_Canvas SHALL validate imported workflow JSON against the schema
   */
  static validateSchema(json: string): SerializationError[] {
    const errors: SerializationError[] = [];

    let parsed: unknown;
    try {
      parsed = JSON.parse(json);
    } catch {
      errors.push({ field: 'root', message: 'Invalid JSON format' });
      return errors;
    }

    if (typeof parsed !== 'object' || parsed === null) {
      errors.push({ field: 'root', message: 'Workflow must be an object' });
      return errors;
    }

    const workflow = parsed as Record<string, unknown>;

    // Validate required fields
    if (typeof workflow.id !== 'string' || workflow.id.length === 0) {
      errors.push({ field: 'id', message: 'Workflow id is required and must be a non-empty string' });
    }

    if (typeof workflow.name !== 'string') {
      errors.push({ field: 'name', message: 'Workflow name must be a string' });
    }

    if (typeof workflow.version !== 'string') {
      errors.push({ field: 'version', message: 'Workflow version must be a string' });
    } else if (!/^\d+\.\d+\.\d+$/.test(workflow.version as string)) {
      errors.push({ field: 'version', message: 'Workflow version must be in semver format (e.g., 1.0.0)' });
    }

    // Validate nodes array
    if (!Array.isArray(workflow.nodes)) {
      errors.push({ field: 'nodes', message: 'Workflow nodes must be an array' });
    } else {
      (workflow.nodes as unknown[]).forEach((node, index) => {
        const nodeErrors = this.validateNode(node, index);
        errors.push(...nodeErrors);
      });
    }

    // Validate edges array
    if (!Array.isArray(workflow.edges)) {
      errors.push({ field: 'edges', message: 'Workflow edges must be an array' });
    } else {
      (workflow.edges as unknown[]).forEach((edge, index) => {
        const edgeErrors = this.validateEdge(edge, index);
        errors.push(...edgeErrors);
      });
    }

    // Validate viewport
    if (typeof workflow.viewport !== 'object' || workflow.viewport === null) {
      errors.push({ field: 'viewport', message: 'Workflow viewport must be an object' });
    } else {
      const viewportErrors = this.validateViewport(workflow.viewport as Record<string, unknown>);
      errors.push(...viewportErrors);
    }

    // Validate metadata
    if (typeof workflow.metadata !== 'object' || workflow.metadata === null) {
      errors.push({ field: 'metadata', message: 'Workflow metadata must be an object' });
    } else {
      const metadataErrors = this.validateMetadata(workflow.metadata as Record<string, unknown>);
      errors.push(...metadataErrors);
    }

    return errors;
  }

  // ============================================================================
  // Private Serialization Methods
  // ============================================================================

  private static serializeNode(node: AgentCoreNode): SerializedNode {
    return {
      id: node.id,
      type: node.data.componentType,
      position: { x: node.position.x, y: node.position.y },
      data: {
        label: node.data.label,
        componentType: node.data.componentType,
        configuration: node.data.configuration,
        validationStatus: node.data.validationStatus,
      },
      selected: node.selected ?? false,
      validationStatus: node.data.validationStatus,
    };
  }

  private static serializeEdge(edge: Edge): SerializedEdge {
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.sourceHandle ?? undefined,
      targetHandle: edge.targetHandle ?? undefined,
      type: edge.type,
      animated: edge.animated,
      data: edge.data as Record<string, unknown> | undefined,
      selected: edge.selected,
    };
  }

  private static serializeViewport(viewport: Viewport): SerializedViewport {
    return {
      x: viewport.x,
      y: viewport.y,
      zoom: viewport.zoom,
    };
  }

  private static serializeMetadata(metadata?: Partial<SerializedMetadata>): SerializedMetadata {
    return {
      author: metadata?.author ?? '',
      tags: metadata?.tags ?? [],
      awsRegion: metadata?.awsRegion ?? 'us-east-1',
      deploymentStatus: metadata?.deploymentStatus ?? 'not_deployed',
      lastDeployedAt: metadata?.lastDeployedAt,
      endpointUrl: metadata?.endpointUrl,
    };
  }

  // ============================================================================
  // Private Deserialization Methods
  // ============================================================================

  private static deserializeNode(node: SerializedNode): AgentCoreNode {
    return {
      id: node.id,
      type: node.type,
      position: { x: node.position.x, y: node.position.y },
      data: {
        label: (node.data.label as string) ?? node.type,
        componentType: node.type,
        configuration: node.data.configuration as AgentCoreNode['data']['configuration'],
        validationStatus: node.validationStatus ?? 'pending',
      },
      selected: node.selected ?? false,
    };
  }

  private static deserializeEdge(edge: SerializedEdge): Edge {
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.sourceHandle ?? null,
      targetHandle: edge.targetHandle ?? null,
      type: edge.type,
      animated: edge.animated ?? false,
      data: edge.data,
      selected: edge.selected ?? false,
    };
  }

  private static deserializeViewport(viewport: SerializedViewport): Viewport {
    return {
      x: viewport.x,
      y: viewport.y,
      zoom: Math.max(0.1, Math.min(4, viewport.zoom)),
    };
  }

  // ============================================================================
  // Private Validation Methods
  // ============================================================================

  private static validateNode(node: unknown, index: number): SerializationError[] {
    const errors: SerializationError[] = [];
    const prefix = `nodes[${index}]`;

    if (typeof node !== 'object' || node === null) {
      errors.push({ field: prefix, message: 'Node must be an object' });
      return errors;
    }

    const n = node as Record<string, unknown>;

    if (typeof n.id !== 'string' || n.id.length === 0) {
      errors.push({ field: `${prefix}.id`, message: 'Node id is required' });
    }

    const validTypes = [
      'runtime',
      'gateway',
      'memory',
      'code_interpreter',
      'browser',
      'observability',
      'identity',
      'evaluation',
      'policy',
      'a2a',
    ];
    if (typeof n.type !== 'string' || !validTypes.includes(n.type)) {
      errors.push({ field: `${prefix}.type`, message: `Node type must be one of: ${validTypes.join(', ')}` });
    }

    if (typeof n.position !== 'object' || n.position === null) {
      errors.push({ field: `${prefix}.position`, message: 'Node position is required' });
    } else {
      const pos = n.position as Record<string, unknown>;
      if (typeof pos.x !== 'number') {
        errors.push({ field: `${prefix}.position.x`, message: 'Node position.x must be a number' });
      }
      if (typeof pos.y !== 'number') {
        errors.push({ field: `${prefix}.position.y`, message: 'Node position.y must be a number' });
      }
    }

    return errors;
  }

  private static validateEdge(edge: unknown, index: number): SerializationError[] {
    const errors: SerializationError[] = [];
    const prefix = `edges[${index}]`;

    if (typeof edge !== 'object' || edge === null) {
      errors.push({ field: prefix, message: 'Edge must be an object' });
      return errors;
    }

    const e = edge as Record<string, unknown>;

    if (typeof e.id !== 'string' || e.id.length === 0) {
      errors.push({ field: `${prefix}.id`, message: 'Edge id is required' });
    }

    if (typeof e.source !== 'string' || e.source.length === 0) {
      errors.push({ field: `${prefix}.source`, message: 'Edge source is required' });
    }

    if (typeof e.target !== 'string' || e.target.length === 0) {
      errors.push({ field: `${prefix}.target`, message: 'Edge target is required' });
    }

    return errors;
  }

  private static validateViewport(viewport: Record<string, unknown>): SerializationError[] {
    const errors: SerializationError[] = [];

    if (typeof viewport.x !== 'number') {
      errors.push({ field: 'viewport.x', message: 'Viewport x must be a number' });
    }

    if (typeof viewport.y !== 'number') {
      errors.push({ field: 'viewport.y', message: 'Viewport y must be a number' });
    }

    if (typeof viewport.zoom !== 'number') {
      errors.push({ field: 'viewport.zoom', message: 'Viewport zoom must be a number' });
    } else if (viewport.zoom < 0.1 || viewport.zoom > 4) {
      errors.push({ field: 'viewport.zoom', message: 'Viewport zoom must be between 0.1 and 4' });
    }

    return errors;
  }

  private static validateMetadata(metadata: Record<string, unknown>): SerializationError[] {
    const errors: SerializationError[] = [];

    if (typeof metadata.author !== 'string') {
      errors.push({ field: 'metadata.author', message: 'Metadata author must be a string' });
    }

    if (!Array.isArray(metadata.tags)) {
      errors.push({ field: 'metadata.tags', message: 'Metadata tags must be an array' });
    }

    if (typeof metadata.awsRegion !== 'string') {
      errors.push({ field: 'metadata.awsRegion', message: 'Metadata awsRegion must be a string' });
    }

    const validStatuses = ['not_deployed', 'deploying', 'deployed', 'failed'];
    if (typeof metadata.deploymentStatus !== 'string' || !validStatuses.includes(metadata.deploymentStatus)) {
      errors.push({
        field: 'metadata.deploymentStatus',
        message: `Metadata deploymentStatus must be one of: ${validStatuses.join(', ')}`,
      });
    }

    return errors;
  }
}

// ============================================================================
// Helper Functions
// ============================================================================

/**
 * Generates a unique ID for workflows.
 */
function generateId(): string {
  return `workflow-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
}

/**
 * Checks if two serialized workflows are equivalent.
 * Used for testing round-trip serialization.
 */
export function areWorkflowsEquivalent(
  a: SerializedWorkflow,
  b: SerializedWorkflow
): boolean {
  // Compare nodes
  if (a.nodes.length !== b.nodes.length) return false;
  for (let i = 0; i < a.nodes.length; i++) {
    if (!areNodesEquivalent(a.nodes[i], b.nodes[i])) return false;
  }

  // Compare edges
  if (a.edges.length !== b.edges.length) return false;
  for (let i = 0; i < a.edges.length; i++) {
    if (!areEdgesEquivalent(a.edges[i], b.edges[i])) return false;
  }

  // Compare viewport
  if (!areViewportsEquivalent(a.viewport, b.viewport)) return false;

  return true;
}

function areNodesEquivalent(a: SerializedNode, b: SerializedNode): boolean {
  return (
    a.id === b.id &&
    a.type === b.type &&
    Math.abs(a.position.x - b.position.x) < 0.001 &&
    Math.abs(a.position.y - b.position.y) < 0.001
  );
}

function areEdgesEquivalent(a: SerializedEdge, b: SerializedEdge): boolean {
  return (
    a.id === b.id &&
    a.source === b.source &&
    a.target === b.target
  );
}

function areViewportsEquivalent(a: SerializedViewport, b: SerializedViewport): boolean {
  return (
    Math.abs(a.x - b.x) < 0.001 &&
    Math.abs(a.y - b.y) < 0.001 &&
    Math.abs(a.zoom - b.zoom) < 0.001
  );
}
