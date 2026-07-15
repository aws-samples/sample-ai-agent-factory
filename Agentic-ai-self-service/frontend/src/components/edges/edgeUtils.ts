/**
 * Utility functions for edge rendering and styling.
 * Extracted to support React Fast Refresh requirements.
 */

import type { ConnectionType, ValidationStatus } from '../../types/workflow';

// ============================================================================
// Constants
// ============================================================================

// Neon edge colors — bright, saturated so the wires glow on the dark canvas.
export const CONNECTION_COLORS: Record<ConnectionType, string> = {
  data: '#38bdf8',     // neon sky
  tool: '#34d399',     // neon emerald
  identity: '#fbbf24', // neon amber
};

// ============================================================================
// Bezier Curve Calculations
// ============================================================================

/**
 * Calculate cubic Bezier control points for smooth edge curves.
 * Property 11: Bezier Curve Path Validity
 * For any edge connecting two ports, the rendered path shall be a valid
 * cubic Bezier curve with control points calculated to create smooth curvature.
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

// ============================================================================
// Color Determination
// ============================================================================

/**
 * Get edge color based on connection type.
 * Property 12: Connection Color by Type
 * For any edge with connection type T, the rendered color shall be:
 * - blue (#3B82F6) for data
 * - green (#22C55E) for authentication
 * - orange (#F97316) for policy
 */
export function getEdgeColor(connectionType: ConnectionType): string {
  return CONNECTION_COLORS[connectionType] || CONNECTION_COLORS.data;
}

/**
 * Get edge color based on validation status (overrides connection type color if error).
 */
export function getEdgeColorWithValidation(
  connectionType: ConnectionType,
  validationStatus?: ValidationStatus
): string {
  if (validationStatus === 'error') {
    return '#EF4444'; // red-500
  }
  if (validationStatus === 'warning') {
    return '#F59E0B'; // amber-500
  }
  return getEdgeColor(connectionType);
}
