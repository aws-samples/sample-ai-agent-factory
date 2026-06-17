/**
 * Property-based tests for viewport transformations.
 * Validates: Requirements 1.3, 1.4
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  applyPanDelta,
  applyZoomAtPoint,
  screenToCanvas,
  canvasToScreen,
} from './viewport';

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

const viewportArb = fc.record({
  x: fc.float({ min: Math.fround(-10000), max: Math.fround(10000), noNaN: true }),
  y: fc.float({ min: Math.fround(-10000), max: Math.fround(10000), noNaN: true }),
  zoom: fc.float({ min: Math.fround(0.1), max: Math.fround(4), noNaN: true }),
});

const dragDeltaArb = fc.record({
  dx: fc.float({ min: Math.fround(-1000), max: Math.fround(1000), noNaN: true }),
  dy: fc.float({ min: Math.fround(-1000), max: Math.fround(1000), noNaN: true }),
});

const screenPointArb = fc.record({
  x: fc.float({ min: Math.fround(0), max: Math.fround(2000), noNaN: true }),
  y: fc.float({ min: Math.fround(0), max: Math.fround(2000), noNaN: true }),
});

const zoomFactorArb = fc.float({ min: Math.fround(0.5), max: Math.fround(2), noNaN: true });

// ============================================================================
// Property 2: Viewport Pan Follows Drag Delta
// ============================================================================

describe('Property 2: Viewport Pan Follows Drag Delta', () => {
  /**
   * **Validates: Requirements 1.3**
   *
   * For any canvas drag operation with delta (dx, dy), the viewport position
   * shall change by (-dx, -dy) to create the panning effect.
   */
  it('viewport position changes by negative drag delta', () => {
    fc.assert(
      fc.property(viewportArb, dragDeltaArb, (viewport, dragDelta) => {
        const newViewport = applyPanDelta(viewport, dragDelta);

        // The viewport should move in the opposite direction of the drag
        // to create the effect of "grabbing" the canvas
        const expectedX = viewport.x - dragDelta.dx;
        const expectedY = viewport.y - dragDelta.dy;

        expect(newViewport.x).toBeCloseTo(expectedX, 5);
        expect(newViewport.y).toBeCloseTo(expectedY, 5);
      }),
      { numRuns: 100 }
    );
  });

  it('pan preserves zoom level', () => {
    fc.assert(
      fc.property(viewportArb, dragDeltaArb, (viewport, dragDelta) => {
        const newViewport = applyPanDelta(viewport, dragDelta);

        // Zoom should remain unchanged during pan
        expect(newViewport.zoom).toBe(viewport.zoom);
      }),
      { numRuns: 100 }
    );
  });

  it('zero drag delta results in unchanged position', () => {
    fc.assert(
      fc.property(viewportArb, (viewport) => {
        const newViewport = applyPanDelta(viewport, { dx: 0, dy: 0 });

        expect(newViewport.x).toBe(viewport.x);
        expect(newViewport.y).toBe(viewport.y);
      }),
      { numRuns: 100 }
    );
  });

  it('consecutive pans are additive', () => {
    fc.assert(
      fc.property(viewportArb, dragDeltaArb, dragDeltaArb, (viewport, delta1, delta2) => {
        // Apply two separate pans
        const afterFirst = applyPanDelta(viewport, delta1);
        const afterBoth = applyPanDelta(afterFirst, delta2);

        // Apply combined pan
        const combinedDelta = { dx: delta1.dx + delta2.dx, dy: delta1.dy + delta2.dy };
        const afterCombined = applyPanDelta(viewport, combinedDelta);

        // Results should be equivalent
        expect(afterBoth.x).toBeCloseTo(afterCombined.x, 5);
        expect(afterBoth.y).toBeCloseTo(afterCombined.y, 5);
      }),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Property 3: Zoom Centered on Cursor
// ============================================================================

describe('Property 3: Zoom Centered on Cursor', () => {
  /**
   * **Validates: Requirements 1.4**
   *
   * For any zoom operation at cursor position (cx, cy) with zoom factor z,
   * the point under the cursor shall remain at the same screen position after zooming.
   */
  it('point under cursor remains at same screen position after zoom', () => {
    fc.assert(
      fc.property(viewportArb, screenPointArb, zoomFactorArb, (viewport, cursorScreen, zoomFactor) => {
        // Get canvas point under cursor before zoom
        const canvasPointBefore = screenToCanvas(cursorScreen, viewport);

        // Apply zoom
        const newViewport = applyZoomAtPoint(viewport, cursorScreen, zoomFactor);

        // If zoom was clamped to bounds and didn't change, skip this test case
        if (newViewport.zoom === viewport.zoom) {
          return true;
        }

        // Get screen position of the same canvas point after zoom
        const screenPointAfter = canvasToScreen(canvasPointBefore, newViewport);

        // The screen position should remain the same (within floating point tolerance)
        expect(screenPointAfter.x).toBeCloseTo(cursorScreen.x, 3);
        expect(screenPointAfter.y).toBeCloseTo(cursorScreen.y, 3);
      }),
      { numRuns: 100 }
    );
  });

  it('zoom respects minimum zoom bound', () => {
    fc.assert(
      fc.property(viewportArb, screenPointArb, (viewport, cursorScreen) => {
        // Try to zoom out significantly
        const newViewport = applyZoomAtPoint(viewport, cursorScreen, 0.01, 0.1, 4);

        expect(newViewport.zoom).toBeGreaterThanOrEqual(0.1);
      }),
      { numRuns: 100 }
    );
  });

  it('zoom respects maximum zoom bound', () => {
    fc.assert(
      fc.property(viewportArb, screenPointArb, (viewport, cursorScreen) => {
        // Try to zoom in significantly
        const newViewport = applyZoomAtPoint(viewport, cursorScreen, 100, 0.1, 4);

        expect(newViewport.zoom).toBeLessThanOrEqual(4);
      }),
      { numRuns: 100 }
    );
  });

  it('zoom factor of 1 results in unchanged viewport', () => {
    fc.assert(
      fc.property(viewportArb, screenPointArb, (viewport, cursorScreen) => {
        const newViewport = applyZoomAtPoint(viewport, cursorScreen, 1);

        expect(newViewport.x).toBeCloseTo(viewport.x, 5);
        expect(newViewport.y).toBeCloseTo(viewport.y, 5);
        expect(newViewport.zoom).toBeCloseTo(viewport.zoom, 5);
      }),
      { numRuns: 100 }
    );
  });

  it('zoom in followed by zoom out returns to original zoom level', () => {
    // Use a constrained viewport that won't hit zoom bounds
    const constrainedViewportArb = fc.record({
      x: fc.float({ min: Math.fround(-10000), max: Math.fround(10000), noNaN: true }),
      y: fc.float({ min: Math.fround(-10000), max: Math.fround(10000), noNaN: true }),
      // Constrain zoom to middle range to avoid hitting bounds after zoom in/out
      zoom: fc.float({ min: Math.fround(0.5), max: Math.fround(2), noNaN: true }),
    });

    // Use smaller zoom factors to avoid hitting bounds
    const smallZoomFactorArb = fc.float({ min: Math.fround(1.1), max: Math.fround(1.5), noNaN: true });

    fc.assert(
      fc.property(
        constrainedViewportArb,
        screenPointArb,
        smallZoomFactorArb,
        (viewport, cursorScreen, zoomInFactor) => {
          // Zoom in
          const zoomedIn = applyZoomAtPoint(viewport, cursorScreen, zoomInFactor);

          // If we hit bounds, skip (shouldn't happen with constrained inputs)
          if (zoomedIn.zoom === viewport.zoom) {
            return true;
          }

          // Zoom out by inverse factor
          const zoomOutFactor = 1 / zoomInFactor;
          const zoomedOut = applyZoomAtPoint(zoomedIn, cursorScreen, zoomOutFactor);

          // If the zoom out also hit bounds, skip
          if (zoomedOut.zoom === zoomedIn.zoom) {
            return true;
          }

          // Zoom level should return to original (within tolerance)
          // Using relative comparison for better floating-point handling
          const relativeError = Math.abs(zoomedOut.zoom - viewport.zoom) / viewport.zoom;
          expect(relativeError).toBeLessThan(0.01); // 1% tolerance
        }
      ),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Coordinate Conversion Properties
// ============================================================================

describe('Coordinate Conversion Round-Trip', () => {
  it('screen to canvas to screen is identity', () => {
    fc.assert(
      fc.property(viewportArb, screenPointArb, (viewport, screenPoint) => {
        const canvasPoint = screenToCanvas(screenPoint, viewport);
        const backToScreen = canvasToScreen(canvasPoint, viewport);

        expect(backToScreen.x).toBeCloseTo(screenPoint.x, 5);
        expect(backToScreen.y).toBeCloseTo(screenPoint.y, 5);
      }),
      { numRuns: 100 }
    );
  });

  it('canvas to screen to canvas is identity', () => {
    const canvasPointArb = fc.record({
      x: fc.float({ min: Math.fround(-10000), max: Math.fround(10000), noNaN: true }),
      y: fc.float({ min: Math.fround(-10000), max: Math.fround(10000), noNaN: true }),
    });

    fc.assert(
      fc.property(viewportArb, canvasPointArb, (viewport, canvasPoint) => {
        const screenPoint = canvasToScreen(canvasPoint, viewport);
        const backToCanvas = screenToCanvas(screenPoint, viewport);

        expect(backToCanvas.x).toBeCloseTo(canvasPoint.x, 5);
        expect(backToCanvas.y).toBeCloseTo(canvasPoint.y, 5);
      }),
      { numRuns: 100 }
    );
  });
});
