/**
 * Custom edge component for AgentCore workflow connections.
 * Renders Bezier curves with color coding by connection type.
 * Displays validation status indicators.
 * Requirements: 2.4, 2.5, 2.6, 2.7, 8.3
 */

import { memo } from 'react';
import {
  BaseEdge,
  getBezierPath,
  type EdgeProps,
  type Edge,
} from '@xyflow/react';
import type { ConnectionType, ValidationStatus } from '../../types/workflow';
import { CONNECTION_COLORS } from '../../types/validation';
import type { ValidationError } from '../../types/validation';

// ============================================================================
// Edge Data Interface
// ============================================================================

export interface ConnectionEdgeData extends Record<string, unknown> {
  connectionType: ConnectionType;
  label?: string;
  validationStatus?: ValidationStatus;
  validationErrors?: ValidationError[];
}

// ============================================================================
// Bezier Path Calculation
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

// ============================================================================
// ConnectionEdge Component
// ============================================================================

type ConnectionEdgeProps = EdgeProps<Edge<ConnectionEdgeData>>;

function ConnectionEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  selected,
  markerEnd,
}: ConnectionEdgeProps) {
  // Get connection type from data, default to 'data'
  const connectionType: ConnectionType = data?.connectionType || 'data';
  const validationStatus = data?.validationStatus;
  const validationErrors = data?.validationErrors || [];
  const hasError = validationStatus === 'error';

  const edgeColor = getEdgeColorWithValidation(connectionType, validationStatus);

  // Use React Flow's getBezierPath for consistent path calculation
  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });

  return (
    <>
      {/* Main edge path */}
      <BaseEdge
        id={id}
        path={edgePath}
        markerEnd={markerEnd}
        style={{
          stroke: edgeColor,
          strokeWidth: selected ? 3 : 2,
          strokeDasharray: hasError ? '5,5' : undefined,
          transition: 'stroke-width 0.15s ease',
        }}
      />

      {/* Selection highlight (wider invisible path for easier clicking) */}
      <path
        d={edgePath}
        fill="none"
        stroke="transparent"
        strokeWidth={20}
        className="react-flow__edge-interaction"
      />

      {/* Optional label */}
      {data?.label && (
        <foreignObject
          x={labelX - 50}
          y={labelY - 10}
          width={100}
          height={20}
          className="overflow-visible"
        >
          <div
            className="flex items-center justify-center text-xs bg-white px-2 py-0.5 rounded border shadow-sm"
            style={{ borderColor: edgeColor }}
          >
            {data.label}
          </div>
        </foreignObject>
      )}

      {/* Validation error indicator */}
      {hasError && validationErrors.length > 0 && (
        <foreignObject
          x={labelX - 75}
          y={labelY - 12}
          width={150}
          height={24}
          className="overflow-visible pointer-events-auto"
        >
          <div
            className="flex items-center justify-center text-xs bg-red-50 text-red-600 px-2 py-1 rounded border border-red-300 shadow-sm"
            title={validationErrors.map(e => e.message).join(', ')}
            data-testid="edge-validation-error"
          >
            <span className="mr-1">⚠</span>
            <span className="truncate">{validationErrors[0]?.message || 'Invalid connection'}</span>
          </div>
        </foreignObject>
      )}

      {/* Selection indicator */}
      {selected && !hasError && (
        <circle
          cx={labelX}
          cy={labelY}
          r={6}
          fill={edgeColor}
          className="cursor-pointer"
        />
      )}
    </>
  );
}

export default memo(ConnectionEdge);
