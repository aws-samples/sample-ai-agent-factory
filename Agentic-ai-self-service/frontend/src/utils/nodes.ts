/**
 * Node utility functions for selection and movement operations.
 * These pure functions enable property-based testing of node operations.
 */

import type { Edge } from '@xyflow/react';
import type { AgentCoreNode, AgentCoreNodeData } from '../store/workflowStore';

// ============================================================================
// Node Selection Operations
// ============================================================================

/**
 * Apply selection to nodes, ensuring only one node is selected at a time.
 * Property 4: Node Selection State Consistency
 * For any node click operation, exactly one node shall be in selected state,
 * and it shall be the clicked node.
 *
 * @param nodes - Current array of nodes
 * @param selectedNodeId - ID of the node to select (null to deselect all)
 * @returns New array of nodes with updated selection state
 */
export function applyNodeSelection(
  nodes: AgentCoreNode[],
  selectedNodeId: string | null
): AgentCoreNode[] {
  return nodes.map((node) => ({
    ...node,
    selected: node.id === selectedNodeId,
  }));
}

/**
 * Get the currently selected node from an array of nodes.
 * @param nodes - Array of nodes
 * @returns The selected node or null if none selected
 */
export function getSelectedNode(nodes: AgentCoreNode[]): AgentCoreNode | null {
  return nodes.find((node) => node.selected) || null;
}

/**
 * Count the number of selected nodes.
 * @param nodes - Array of nodes
 * @returns Number of selected nodes
 */
export function countSelectedNodes(nodes: AgentCoreNode[]): number {
  return nodes.filter((node) => node.selected).length;
}

// ============================================================================
// Node Movement Operations
// ============================================================================

/**
 * Update a node's position.
 * Property 5: Node Movement Updates Position and Edges
 * For any node drag operation with delta (dx, dy), the node position
 * shall change by (dx, dy).
 *
 * @param nodes - Current array of nodes
 * @param nodeId - ID of the node to move
 * @param newPosition - New position { x, y }
 * @returns New array of nodes with updated position
 */
export function updateNodePosition(
  nodes: AgentCoreNode[],
  nodeId: string,
  newPosition: { x: number; y: number }
): AgentCoreNode[] {
  return nodes.map((node) =>
    node.id === nodeId
      ? { ...node, position: newPosition }
      : node
  );
}

/**
 * Apply a position delta to a node.
 * @param nodes - Current array of nodes
 * @param nodeId - ID of the node to move
 * @param delta - Position delta { dx, dy }
 * @returns New array of nodes with updated position
 */
export function applyNodeMoveDelta(
  nodes: AgentCoreNode[],
  nodeId: string,
  delta: { dx: number; dy: number }
): AgentCoreNode[] {
  return nodes.map((node) =>
    node.id === nodeId
      ? {
          ...node,
          position: {
            x: node.position.x + delta.dx,
            y: node.position.y + delta.dy,
          },
        }
      : node
  );
}

// ============================================================================
// Node Deletion Operations
// ============================================================================

/**
 * Delete a node and all its connected edges.
 * Property 6: Node Deletion Removes Node and Connected Edges
 * For any node deletion operation, the workflow shall contain neither
 * the deleted node nor any edges that referenced the deleted node.
 *
 * @param nodes - Current array of nodes
 * @param edges - Current array of edges
 * @param nodeId - ID of the node to delete
 * @returns Object with updated nodes and edges arrays
 */
export function deleteNodeWithEdges(
  nodes: AgentCoreNode[],
  edges: Edge[],
  nodeId: string
): { nodes: AgentCoreNode[]; edges: Edge[] } {
  return {
    nodes: nodes.filter((node) => node.id !== nodeId),
    edges: edges.filter(
      (edge) => edge.source !== nodeId && edge.target !== nodeId
    ),
  };
}

/**
 * Get all edges connected to a specific node.
 * @param edges - Array of edges
 * @param nodeId - ID of the node
 * @returns Array of edges connected to the node
 */
export function getConnectedEdges(edges: Edge[], nodeId: string): Edge[] {
  return edges.filter(
    (edge) => edge.source === nodeId || edge.target === nodeId
  );
}

// ============================================================================
// Node Creation Utilities
// ============================================================================

/**
 * Create a new AgentCore node at a specific position.
 * @param id - Unique node ID
 * @param type - Component type
 * @param position - Position on canvas
 * @param label - Display label
 * @returns New node object
 */
export function createNode(
  id: string,
  type: AgentCoreNodeData['componentType'],
  position: { x: number; y: number },
  label: string
): AgentCoreNode {
  return {
    id,
    type,
    position,
    selected: false,
    data: {
      label,
      componentType: type,
      validationStatus: 'pending',
    },
  };
}

/**
 * Check if a node exists in the array.
 * @param nodes - Array of nodes
 * @param nodeId - ID to check
 * @returns True if node exists
 */
export function nodeExists(nodes: AgentCoreNode[], nodeId: string): boolean {
  return nodes.some((node) => node.id === nodeId);
}

/**
 * Check if an edge references a specific node.
 * @param edge - Edge to check
 * @param nodeId - Node ID to check for
 * @returns True if edge references the node
 */
export function edgeReferencesNode(edge: Edge, nodeId: string): boolean {
  return edge.source === nodeId || edge.target === nodeId;
}
