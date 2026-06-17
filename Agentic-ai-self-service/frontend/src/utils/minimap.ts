/**
 * Minimap utility functions for scale calculations and coordinate transformations.
 * These pure functions enable property-based testing of minimap behavior.
 * Requirements: 1.8, 1.9
 */

import type { Viewport } from '../types/workflow';

// ============================================================================
// Types
// ============================================================================

export interface MinimapBounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
  width: number;
  height: number;
}

export interface NodePosition {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface MinimapDimensions {
  width: number;
  height: number;
}

export interface MinimapScaleResult {
  scale: number;
  offsetX: number;
  offsetY: number;
}

export interface ViewportIndicator {
  x: number;
  y: number;
  width: number;
  height: number;
}

// ============================================================================
// Constants
// ============================================================================

export const DEFAULT_NODE_WIDTH = 150;
export const DEFAULT_NODE_HEIGHT = 60;
export const MINIMAP_PADDING = 20;

// ============================================================================
// Bounds Calculation
// ============================================================================

/**
 * Calculate the bounding box that contains all nodes.
 * Returns bounds with padding for visual clarity.
 *
 * @param nodes - Array of node positions
 * @param padding - Padding around the bounds
 * @returns Bounding box containing all nodes
 */
export function calculateNodeBounds(
  nodes: NodePosition[],
  padding: number = MINIMAP_PADDING
): MinimapBounds {
  if (nodes.length === 0) {
    // Default bounds when no nodes exist
    return {
      minX: -500,
      minY: -500,
      maxX: 500,
      maxY: 500,
      width: 1000,
      height: 1000,
    };
  }

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  for (const node of nodes) {
    minX = Math.min(minX, node.x);
    minY = Math.min(minY, node.y);
    maxX = Math.max(maxX, node.x + node.width);
    maxY = Math.max(maxY, node.y + node.height);
  }

  // Add padding
  minX -= padding;
  minY -= padding;
  maxX += padding;
  maxY += padding;

  return {
    minX,
    minY,
    maxX,
    maxY,
    width: maxX - minX,
    height: maxY - minY,
  };
}

// ============================================================================
// Scale Calculation
// ============================================================================

/**
 * Calculate the scale factor to fit all nodes within the minimap.
 * Property 7: Minimap Scale Consistency
 * For any workflow with nodes, the minimap shall display all nodes at a
 * consistent scale factor relative to the full canvas bounds.
 *
 * @param bounds - The bounding box of all nodes
 * @param minimapSize - The size of the minimap container
 * @returns Scale factor and offsets for centering
 */
export function calculateMinimapScale(
  bounds: MinimapBounds,
  minimapSize: MinimapDimensions
): MinimapScaleResult {
  // Calculate scale to fit bounds within minimap
  const scaleX = minimapSize.width / bounds.width;
  const scaleY = minimapSize.height / bounds.height;

  // Use the smaller scale to ensure everything fits
  const scale = Math.min(scaleX, scaleY);

  // Calculate offsets to center the content
  const scaledWidth = bounds.width * scale;
  const scaledHeight = bounds.height * scale;
  const offsetX = (minimapSize.width - scaledWidth) / 2;
  const offsetY = (minimapSize.height - scaledHeight) / 2;

  return {
    scale,
    offsetX,
    offsetY,
  };
}

// ============================================================================
// Coordinate Transformations
// ============================================================================

/**
 * Transform a canvas position to minimap coordinates.
 *
 * @param canvasPoint - Point in canvas coordinates
 * @param bounds - The bounding box of all nodes
 * @param scaleResult - The scale calculation result
 * @returns Point in minimap coordinates
 */
export function canvasToMinimap(
  canvasPoint: { x: number; y: number },
  bounds: MinimapBounds,
  scaleResult: MinimapScaleResult
): { x: number; y: number } {
  return {
    x: (canvasPoint.x - bounds.minX) * scaleResult.scale + scaleResult.offsetX,
    y: (canvasPoint.y - bounds.minY) * scaleResult.scale + scaleResult.offsetY,
  };
}

/**
 * Transform a minimap position to canvas coordinates.
 * Property 8: Minimap Click Navigation
 * For any click at minimap position (mx, my), the viewport shall center on
 * the corresponding canvas position calculated by the inverse of the minimap
 * scale transformation.
 *
 * @param minimapPoint - Point in minimap coordinates
 * @param bounds - The bounding box of all nodes
 * @param scaleResult - The scale calculation result
 * @returns Point in canvas coordinates
 */
export function minimapToCanvas(
  minimapPoint: { x: number; y: number },
  bounds: MinimapBounds,
  scaleResult: MinimapScaleResult
): { x: number; y: number } {
  return {
    x: (minimapPoint.x - scaleResult.offsetX) / scaleResult.scale + bounds.minX,
    y: (minimapPoint.y - scaleResult.offsetY) / scaleResult.scale + bounds.minY,
  };
}

// ============================================================================
// Viewport Indicator Calculation
// ============================================================================

/**
 * Calculate the viewport indicator rectangle for the minimap.
 * This shows the currently visible area of the canvas.
 *
 * @param viewport - Current viewport state
 * @param screenSize - Size of the main canvas screen
 * @param bounds - The bounding box of all nodes
 * @param scaleResult - The scale calculation result
 * @returns Viewport indicator rectangle in minimap coordinates
 */
export function calculateViewportIndicator(
  viewport: Viewport,
  screenSize: { width: number; height: number },
  bounds: MinimapBounds,
  scaleResult: MinimapScaleResult
): ViewportIndicator {
  // Calculate the visible canvas area
  const visibleCanvasWidth = screenSize.width / viewport.zoom;
  const visibleCanvasHeight = screenSize.height / viewport.zoom;

  // The viewport.x and viewport.y represent the top-left corner of the visible area
  const topLeft = canvasToMinimap(
    { x: viewport.x, y: viewport.y },
    bounds,
    scaleResult
  );

  // Calculate dimensions in minimap coordinates
  const width = visibleCanvasWidth * scaleResult.scale;
  const height = visibleCanvasHeight * scaleResult.scale;

  return {
    x: topLeft.x,
    y: topLeft.y,
    width,
    height,
  };
}

// ============================================================================
// Navigation Calculation
// ============================================================================

/**
 * Calculate the new viewport position to center on a minimap click.
 * Property 8: Minimap Click Navigation
 *
 * @param minimapClickPoint - Click position in minimap coordinates
 * @param viewport - Current viewport state
 * @param screenSize - Size of the main canvas screen
 * @param bounds - The bounding box of all nodes
 * @param scaleResult - The scale calculation result
 * @returns New viewport position centered on the clicked canvas point
 */
export function calculateViewportFromMinimapClick(
  minimapClickPoint: { x: number; y: number },
  viewport: Viewport,
  screenSize: { width: number; height: number },
  bounds: MinimapBounds,
  scaleResult: MinimapScaleResult
): Viewport {
  // Convert minimap click to canvas coordinates
  const canvasPoint = minimapToCanvas(minimapClickPoint, bounds, scaleResult);

  // Calculate the visible canvas area
  const visibleCanvasWidth = screenSize.width / viewport.zoom;
  const visibleCanvasHeight = screenSize.height / viewport.zoom;

  // Center the viewport on the clicked point
  const newX = canvasPoint.x - visibleCanvasWidth / 2;
  const newY = canvasPoint.y - visibleCanvasHeight / 2;

  return {
    x: newX,
    y: newY,
    zoom: viewport.zoom,
  };
}

/**
 * Transform node positions to minimap coordinates for rendering.
 *
 * @param nodes - Array of node positions in canvas coordinates
 * @param bounds - The bounding box of all nodes
 * @param scaleResult - The scale calculation result
 * @returns Array of node positions in minimap coordinates
 */
export function transformNodesToMinimap(
  nodes: NodePosition[],
  bounds: MinimapBounds,
  scaleResult: MinimapScaleResult
): NodePosition[] {
  return nodes.map((node) => {
    const minimapPos = canvasToMinimap(
      { x: node.x, y: node.y },
      bounds,
      scaleResult
    );
    return {
      x: minimapPos.x,
      y: minimapPos.y,
      width: node.width * scaleResult.scale,
      height: node.height * scaleResult.scale,
    };
  });
}
