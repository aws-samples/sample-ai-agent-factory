/**
 * Minimap component - Displays a scaled-down view of the workflow canvas.
 * Shows all nodes and a viewport indicator rectangle for navigation.
 * Requirements: 1.8, 1.9
 */

import { useMemo, useCallback, useRef } from 'react';
import type { Viewport } from '../../types/workflow';
import type { AgentCoreNodeData } from '../../store/workflowStore';
import {
  calculateNodeBounds,
  calculateMinimapScale,
  calculateViewportIndicator,
  calculateViewportFromMinimapClick,
  transformNodesToMinimap,
  DEFAULT_NODE_WIDTH,
  DEFAULT_NODE_HEIGHT,
  type NodePosition,
  type MinimapDimensions,
} from '../../utils/minimap';

// ============================================================================
// Constants
// ============================================================================

const MINIMAP_COLORS: Record<string, string> = {
  runtime: '#3B82F6',
  gateway: '#22C55E',
  identity: '#A855F7',
  policy: '#F97316',
  authentication: '#EAB308',
};

const DEFAULT_NODE_COLOR = '#6B7280';

// ============================================================================
// Props Interface
// ============================================================================

export interface MinimapNode {
  id: string;
  position: { x: number; y: number };
  data: AgentCoreNodeData;
  width?: number;
  height?: number;
}

export interface MinimapProps {
  nodes: MinimapNode[];
  viewport: Viewport;
  screenSize: { width: number; height: number };
  onViewportChange?: (viewport: Viewport) => void;
  width?: number;
  height?: number;
  className?: string;
}

// ============================================================================
// Minimap Component
// ============================================================================

export function Minimap({
  nodes,
  viewport,
  screenSize,
  onViewportChange,
  width = 200,
  height = 150,
  className = '',
}: MinimapProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  const minimapSize: MinimapDimensions = useMemo(() => ({ width, height }), [width, height]);

  // Convert nodes to NodePosition format
  const nodePositions: NodePosition[] = useMemo(() => {
    return nodes.map((node) => ({
      x: node.position.x,
      y: node.position.y,
      width: node.width ?? DEFAULT_NODE_WIDTH,
      height: node.height ?? DEFAULT_NODE_HEIGHT,
    }));
  }, [nodes]);

  // Calculate bounds and scale
  const bounds = useMemo(() => calculateNodeBounds(nodePositions), [nodePositions]);
  const scaleResult = useMemo(
    () => calculateMinimapScale(bounds, minimapSize),
    [bounds, minimapSize]
  );

  // Calculate viewport indicator
  const viewportIndicator = useMemo(
    () => calculateViewportIndicator(viewport, screenSize, bounds, scaleResult),
    [viewport, screenSize, bounds, scaleResult]
  );

  // Transform nodes to minimap coordinates
  const minimapNodes = useMemo(
    () => transformNodesToMinimap(nodePositions, bounds, scaleResult),
    [nodePositions, bounds, scaleResult]
  );

  // Handle minimap click for navigation
  // Requirement 1.9: WHEN a user clicks on the Minimap, THE Workflow_Canvas
  // SHALL pan to center on the clicked location
  const handleClick = useCallback(
    (event: React.MouseEvent<HTMLDivElement>) => {
      if (!containerRef.current || !onViewportChange) return;

      const rect = containerRef.current.getBoundingClientRect();
      const minimapClickPoint = {
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
      };

      const newViewport = calculateViewportFromMinimapClick(
        minimapClickPoint,
        viewport,
        screenSize,
        bounds,
        scaleResult
      );

      onViewportChange(newViewport);
    },
    [viewport, screenSize, bounds, scaleResult, onViewportChange]
  );

  return (
    <div
      ref={containerRef}
      className={`bg-gray-100 border border-gray-300 rounded-lg overflow-hidden cursor-pointer ${className}`}
      style={{ width, height }}
      onClick={handleClick}
      data-testid="minimap"
    >
      <svg width={width} height={height} className="block">
        {/* Background */}
        <rect width={width} height={height} fill="#f3f4f6" />

        {/* Render nodes */}
        {minimapNodes.map((node, index) => {
          const originalNode = nodes[index];
          const componentType = originalNode?.data?.componentType;
          const color = componentType ? MINIMAP_COLORS[componentType] || DEFAULT_NODE_COLOR : DEFAULT_NODE_COLOR;

          return (
            <rect
              key={originalNode?.id || index}
              x={node.x}
              y={node.y}
              width={Math.max(node.width, 2)}
              height={Math.max(node.height, 2)}
              fill={color}
              rx={1}
              ry={1}
              data-testid={`minimap-node-${originalNode?.id || index}`}
            />
          );
        })}

        {/* Viewport indicator rectangle */}
        <rect
          x={viewportIndicator.x}
          y={viewportIndicator.y}
          width={viewportIndicator.width}
          height={viewportIndicator.height}
          fill="rgba(59, 130, 246, 0.1)"
          stroke="#3B82F6"
          strokeWidth={2}
          rx={2}
          ry={2}
          data-testid="minimap-viewport-indicator"
        />
      </svg>
    </div>
  );
}

export default Minimap;
