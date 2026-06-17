/**
 * Edge utility functions for connection operations.
 * These pure functions enable property-based testing of edge operations.
 * Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8
 */

import type { Edge } from '@xyflow/react';
import type { AgentCoreComponentType, ConnectionType } from '../types/workflow';
import { CONNECTION_COMPATIBILITY, CONNECTION_COLORS } from '../types/validation';
import type { AgentCoreNode } from '../store/workflowStore';

// ============================================================================
// Bezier Path Calculation
// ============================================================================

/**
 * Calculate cubic Bezier control points for smooth edge curves.
 * Property 11: Bezier Curve Path Validity
 * For any edge connecting two ports, the rendered path shall be a valid
 * cubic Bezier curve with control points calculated to create smooth curvature.
 *
 * @param sourceX - Source X coordinate
 * @param sourceY - Source Y coordinate
 * @param targetX - Target X coordinate
 * @param targetY - Target Y coordinate
 * @returns Control points for cubic Bezier curve
 */
export function calculateBezierControlPoints(
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number
): {
  sourceControlX: number;
  sourceControlY: number;
  targetControlX: number;
  targetControlY: number;
} {
  // Calculate horizontal distance for control point offset
  const dx = Math.abs(targetX - sourceX);
  const controlOffset = Math.max(dx * 0.5, 50); // Minimum offset of 50px

  return {
    sourceControlX: sourceX + controlOffset,
    sourceControlY: sourceY,
    targetControlX: targetX - controlOffset,
    targetControlY: targetY,
  };
}

/**
 * Generate SVG path string for cubic Bezier curve.
 * @param sourceX - Source X coordinate
 * @param sourceY - Source Y coordinate
 * @param targetX - Target X coordinate
 * @param targetY - Target Y coordinate
 * @returns SVG path string
 */
export function generateBezierPath(
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number
): string {
  const { sourceControlX, sourceControlY, targetControlX, targetControlY } =
    calculateBezierControlPoints(sourceX, sourceY, targetX, targetY);

  return `M ${sourceX},${sourceY} C ${sourceControlX},${sourceControlY} ${targetControlX},${targetControlY} ${targetX},${targetY}`;
}

/**
 * Validate that a path string represents a valid cubic Bezier curve.
 * @param path - SVG path string
 * @returns True if path is a valid cubic Bezier
 */
export function isValidBezierPath(path: string): boolean {
  // Cubic Bezier format: M x,y C cx1,cy1 cx2,cy2 x,y
  // Numbers can be in scientific notation (e.g., -1.4e-45)
  const numPattern = '-?\\d+\\.?\\d*(?:[eE][+-]?\\d+)?';
  const bezierRegex = new RegExp(
    `^M\\s*${numPattern},${numPattern}\\s*C\\s*${numPattern},${numPattern}\\s+${numPattern},${numPattern}\\s+${numPattern},${numPattern}$`
  );
  return bezierRegex.test(path.trim());
}

// ============================================================================
// Connection Color
// ============================================================================

/**
 * Get edge color based on connection type.
 * Property 12: Connection Color by Type
 * For any edge with connection type T, the rendered color shall be:
 * - blue (#3B82F6) for data
 * - green (#22C55E) for authentication
 * - orange (#F97316) for policy
 *
 * @param connectionType - The type of connection
 * @returns Hex color string
 */
export function getEdgeColor(connectionType: ConnectionType): string {
  return CONNECTION_COLORS[connectionType] || CONNECTION_COLORS.data;
}

/**
 * Determine connection type based on source and target component types.
 * @param sourceType - Source component type
 * @param targetType - Target component type
 * @returns The appropriate connection type
 */
export function determineConnectionType(
  sourceType: AgentCoreComponentType,
  targetType: AgentCoreComponentType
): ConnectionType {
  // Identity connections
  if (sourceType === 'identity' || targetType === 'identity') {
    return 'identity';
  }
  // Tool connections (code_interpreter, browser, memory, tool)
  if (['code_interpreter', 'browser', 'memory', 'tool'].includes(sourceType) ||
      ['code_interpreter', 'browser', 'memory', 'tool'].includes(targetType)) {
    return 'tool';
  }
  // Default to data flow
  return 'data';
}

// ============================================================================
// Connection Compatibility
// ============================================================================

/**
 * Check if two component types can be connected.
 * Property 9: Compatible Connection Creates Edge
 * For any connection attempt from source port to target port where the source
 * and target component types are in the compatibility matrix, an edge shall
 * be created connecting the two ports.
 *
 * Property 10: Incompatible Connection Rejected
 * For any connection attempt from source port to target port where the source
 * and target component types are NOT in the compatibility matrix, no edge
 * shall be created.
 *
 * @param sourceType - Source component type
 * @param targetType - Target component type
 * @returns True if connection is allowed
 */
export function areComponentsCompatible(
  sourceType: AgentCoreComponentType,
  targetType: AgentCoreComponentType
): boolean {
  const compatibleTargets = CONNECTION_COMPATIBILITY[sourceType];
  return compatibleTargets?.includes(targetType) ?? false;
}

/**
 * Get all compatible target types for a source component type.
 * @param sourceType - Source component type
 * @returns Array of compatible target types
 */
export function getCompatibleTargets(
  sourceType: AgentCoreComponentType
): AgentCoreComponentType[] {
  return CONNECTION_COMPATIBILITY[sourceType] || [];
}

// ============================================================================
// Edge Creation
// ============================================================================

/**
 * Create a new edge between two nodes if compatible.
 * @param sourceNode - Source node
 * @param targetNode - Target node
 * @param sourceHandle - Source handle ID (optional)
 * @param targetHandle - Target handle ID (optional)
 * @returns New edge or null if incompatible
 */
export function createEdgeIfCompatible(
  sourceNode: AgentCoreNode,
  targetNode: AgentCoreNode,
  sourceHandle: string | null = null,
  targetHandle: string | null = null
): Edge | null {
  const sourceType = sourceNode.data.componentType;
  const targetType = targetNode.data.componentType;

  if (!areComponentsCompatible(sourceType, targetType)) {
    return null;
  }

  const connectionType = determineConnectionType(sourceType, targetType);

  return {
    id: `edge-${sourceNode.id}-${targetNode.id}-${Date.now()}`,
    source: sourceNode.id,
    target: targetNode.id,
    sourceHandle,
    targetHandle,
    type: 'connection',
    data: {
      connectionType,
    },
  };
}

// ============================================================================
// Edge Selection
// ============================================================================

/**
 * Apply selection to edges, ensuring only one edge is selected at a time.
 * Property 13: Edge Selection on Click
 * For any edge click operation, the clicked edge shall enter selected state
 * and display delete option.
 *
 * @param edges - Current array of edges
 * @param selectedEdgeId - ID of the edge to select (null to deselect all)
 * @returns New array of edges with updated selection state
 */
export function applyEdgeSelection(
  edges: Edge[],
  selectedEdgeId: string | null
): Edge[] {
  return edges.map((edge) => ({
    ...edge,
    selected: edge.id === selectedEdgeId,
  }));
}

/**
 * Get the currently selected edge from an array of edges.
 * @param edges - Array of edges
 * @returns The selected edge or null if none selected
 */
export function getSelectedEdge(edges: Edge[]): Edge | null {
  return edges.find((edge) => edge.selected) || null;
}

/**
 * Count the number of selected edges.
 * @param edges - Array of edges
 * @returns Number of selected edges
 */
export function countSelectedEdges(edges: Edge[]): number {
  return edges.filter((edge) => edge.selected).length;
}

// ============================================================================
// Edge Deletion
// ============================================================================

/**
 * Delete an edge by ID.
 * @param edges - Current array of edges
 * @param edgeId - ID of the edge to delete
 * @returns New array of edges without the deleted edge
 */
export function deleteEdge(edges: Edge[], edgeId: string): Edge[] {
  return edges.filter((edge) => edge.id !== edgeId);
}

/**
 * Check if an edge exists in the array.
 * @param edges - Array of edges
 * @param edgeId - ID to check
 * @returns True if edge exists
 */
export function edgeExists(edges: Edge[], edgeId: string): boolean {
  return edges.some((edge) => edge.id === edgeId);
}

/**
 * Find an edge by source and target node IDs.
 * @param edges - Array of edges
 * @param sourceId - Source node ID
 * @param targetId - Target node ID
 * @returns The edge or null if not found
 */
export function findEdgeByNodes(
  edges: Edge[],
  sourceId: string,
  targetId: string
): Edge | null {
  return edges.find(
    (edge) => edge.source === sourceId && edge.target === targetId
  ) || null;
}
