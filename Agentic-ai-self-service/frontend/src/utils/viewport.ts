/**
 * Viewport utility functions for pan and zoom calculations.
 * These pure functions enable property-based testing of viewport transformations.
 */

import type { Viewport } from '../types/workflow';

// ============================================================================
// Viewport Pan Calculation
// ============================================================================

/**
 * Calculate new viewport position after a pan operation.
 * Property 2: Viewport Pan Follows Drag Delta
 * For any canvas drag operation with delta (dx, dy), the viewport position
 * shall change by (-dx, -dy) to create the panning effect.
 *
 * @param viewport - Current viewport state
 * @param dragDelta - The drag delta { dx, dy } in screen coordinates
 * @returns New viewport with updated position
 */
export function applyPanDelta(
  viewport: Viewport,
  dragDelta: { dx: number; dy: number }
): Viewport {
  return {
    ...viewport,
    x: viewport.x - dragDelta.dx,
    y: viewport.y - dragDelta.dy,
  };
}

// ============================================================================
// Viewport Zoom Calculation
// ============================================================================

/**
 * Calculate new viewport after a zoom operation centered on cursor.
 * Property 3: Zoom Centered on Cursor
 * For any zoom operation at cursor position (cx, cy) with zoom factor z,
 * the point under the cursor shall remain at the same screen position after zooming.
 *
 * The math:
 * - Before zoom: screenPoint = (canvasPoint - viewport.xy) * viewport.zoom
 * - After zoom: screenPoint = (canvasPoint - newViewport.xy) * newZoom
 * - Since screenPoint must stay the same, we solve for newViewport.xy
 *
 * @param viewport - Current viewport state
 * @param cursorScreen - Cursor position in screen coordinates { x, y }
 * @param zoomFactor - Multiplier for zoom (e.g., 1.1 for zoom in, 0.9 for zoom out)
 * @param minZoom - Minimum allowed zoom level
 * @param maxZoom - Maximum allowed zoom level
 * @returns New viewport with updated zoom and position
 */
export function applyZoomAtPoint(
  viewport: Viewport,
  cursorScreen: { x: number; y: number },
  zoomFactor: number,
  minZoom: number = 0.1,
  maxZoom: number = 4
): Viewport {
  // Calculate new zoom level with bounds
  const newZoom = Math.min(maxZoom, Math.max(minZoom, viewport.zoom * zoomFactor));

  // If zoom didn't change (hit bounds), return unchanged viewport
  if (newZoom === viewport.zoom) {
    return viewport;
  }

  // Convert cursor screen position to canvas position before zoom
  // canvasPoint = screenPoint / zoom + viewport.xy
  const cursorCanvasX = cursorScreen.x / viewport.zoom + viewport.x;
  const cursorCanvasY = cursorScreen.y / viewport.zoom + viewport.y;

  // Calculate new viewport position to keep cursor at same screen position
  // screenPoint = (canvasPoint - newViewport.xy) * newZoom
  // cursorScreen.x = (cursorCanvasX - newX) * newZoom
  // newX = cursorCanvasX - cursorScreen.x / newZoom
  const newX = cursorCanvasX - cursorScreen.x / newZoom;
  const newY = cursorCanvasY - cursorScreen.y / newZoom;

  return {
    x: newX,
    y: newY,
    zoom: newZoom,
  };
}

// ============================================================================
// Coordinate Conversion Utilities
// ============================================================================

/**
 * Convert screen coordinates to canvas coordinates.
 * @param screenPoint - Point in screen coordinates
 * @param viewport - Current viewport state
 * @returns Point in canvas coordinates
 */
export function screenToCanvas(
  screenPoint: { x: number; y: number },
  viewport: Viewport
): { x: number; y: number } {
  return {
    x: screenPoint.x / viewport.zoom + viewport.x,
    y: screenPoint.y / viewport.zoom + viewport.y,
  };
}

/**
 * Convert canvas coordinates to screen coordinates.
 * @param canvasPoint - Point in canvas coordinates
 * @param viewport - Current viewport state
 * @returns Point in screen coordinates
 */
export function canvasToScreen(
  canvasPoint: { x: number; y: number },
  viewport: Viewport
): { x: number; y: number } {
  return {
    x: (canvasPoint.x - viewport.x) * viewport.zoom,
    y: (canvasPoint.y - viewport.y) * viewport.zoom,
  };
}

/**
 * Check if a canvas point is visible in the current viewport.
 * @param canvasPoint - Point in canvas coordinates
 * @param viewport - Current viewport state
 * @param screenSize - Size of the screen/container
 * @returns True if the point is visible
 */
export function isPointVisible(
  canvasPoint: { x: number; y: number },
  viewport: Viewport,
  screenSize: { width: number; height: number }
): boolean {
  const screenPoint = canvasToScreen(canvasPoint, viewport);
  return (
    screenPoint.x >= 0 &&
    screenPoint.x <= screenSize.width &&
    screenPoint.y >= 0 &&
    screenPoint.y <= screenSize.height
  );
}
