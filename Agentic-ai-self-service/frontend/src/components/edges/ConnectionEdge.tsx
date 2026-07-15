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
import type { ValidationError } from '../../types/validation';
import { getEdgeColorWithValidation } from './edgeUtils';

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
      {/* Neon glow underlay — bloom around the wire; brighter when selected. */}
      <path
        d={edgePath}
        fill="none"
        stroke={edgeColor}
        strokeWidth={selected ? 9 : 6}
        strokeLinecap="round"
        style={{
          opacity: selected ? 0.55 : 0.32,
          filter: 'blur(4px)',
          transition: 'opacity 0.2s ease, stroke-width 0.2s ease',
        }}
      />

      {/* Main edge path */}
      <BaseEdge
        id={id}
        path={edgePath}
        markerEnd={markerEnd}
        style={{
          stroke: edgeColor,
          strokeWidth: selected ? 2.5 : 2,
          strokeDasharray: hasError ? '5,5' : undefined,
          transition: 'stroke-width 0.15s ease',
        }}
      />

      {/* Animated flow overlay when selected — dashes travel source→target to
          convey direction/activity (paused under prefers-reduced-motion via CSS). */}
      {selected && !hasError && (
        <path
          d={edgePath}
          fill="none"
          stroke="#ffffff"
          strokeWidth={2.5}
          strokeDasharray="1 10"
          strokeLinecap="round"
          style={{ opacity: 0.9, animation: 'edge-flow 0.9s linear infinite' }}
        />
      )}

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
            className="flex items-center justify-center text-xs bg-white px-2 py-0.5 rounded border"
            style={{ borderColor: edgeColor, boxShadow: 'var(--elevation-1)' }}
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
