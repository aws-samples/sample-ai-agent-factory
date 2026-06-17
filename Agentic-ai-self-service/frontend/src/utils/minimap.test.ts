/**
 * Property-based tests for minimap functionality.
 * Validates: Requirements 1.8, 1.9
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  calculateNodeBounds,
  calculateMinimapScale,
  canvasToMinimap,
  minimapToCanvas,
  calculateViewportIndicator,
  calculateViewportFromMinimapClick,
  transformNodesToMinimap,
  DEFAULT_NODE_WIDTH,
  DEFAULT_NODE_HEIGHT,
  MINIMAP_PADDING,
  type NodePosition,
  type MinimapDimensions,
} from './minimap';

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

const nodePositionArb = fc.record({
  x: fc.float({ min: Math.fround(-5000), max: Math.fround(5000), noNaN: true }),
  y: fc.float({ min: Math.fround(-5000), max: Math.fround(5000), noNaN: true }),
  width: fc.float({ min: Math.fround(50), max: Math.fround(300), noNaN: true }),
  height: fc.float({ min: Math.fround(30), max: Math.fround(150), noNaN: true }),
});

const nodesArrayArb = fc.array(nodePositionArb, { minLength: 1, maxLength: 20 });

const minimapSizeArb = fc.record({
  width: fc.float({ min: Math.fround(100), max: Math.fround(400), noNaN: true }),
  height: fc.float({ min: Math.fround(75), max: Math.fround(300), noNaN: true }),
});

const viewportArb = fc.record({
  x: fc.float({ min: Math.fround(-5000), max: Math.fround(5000), noNaN: true }),
  y: fc.float({ min: Math.fround(-5000), max: Math.fround(5000), noNaN: true }),
  zoom: fc.float({ min: Math.fround(0.1), max: Math.fround(4), noNaN: true }),
});

const screenSizeArb = fc.record({
  width: fc.float({ min: Math.fround(800), max: Math.fround(2000), noNaN: true }),
  height: fc.float({ min: Math.fround(600), max: Math.fround(1500), noNaN: true }),
});

// ============================================================================
// Property 7: Minimap Scale Consistency
// ============================================================================

describe('Property 7: Minimap Scale Consistency', () => {
  /**
   * **Validates: Requirements 1.8**
   *
   * For any workflow with nodes, the minimap shall display all nodes at a
   * consistent scale factor relative to the full canvas bounds.
   */
  it('all nodes are scaled by the same factor', () => {
    fc.assert(
      fc.property(nodesArrayArb, minimapSizeArb, (nodes, minimapSize) => {
        const bounds = calculateNodeBounds(nodes);
        const scaleResult = calculateMinimapScale(bounds, minimapSize);
        const minimapNodes = transformNodesToMinimap(nodes, bounds, scaleResult);

        // All nodes should be scaled by the same factor
        for (let i = 0; i < nodes.length; i++) {
          const originalWidth = nodes[i].width;
          const scaledWidth = minimapNodes[i].width;
          const widthRatio = scaledWidth / originalWidth;

          const originalHeight = nodes[i].height;
          const scaledHeight = minimapNodes[i].height;
          const heightRatio = scaledHeight / originalHeight;

          // Both width and height should be scaled by the same factor
          expect(widthRatio).toBeCloseTo(scaleResult.scale, 4);
          expect(heightRatio).toBeCloseTo(scaleResult.scale, 4);
        }
      }),
      { numRuns: 100 }
    );
  });

  it('all nodes fit within minimap bounds', () => {
    fc.assert(
      fc.property(nodesArrayArb, minimapSizeArb, (nodes, minimapSize) => {
        const bounds = calculateNodeBounds(nodes);
        const scaleResult = calculateMinimapScale(bounds, minimapSize);
        const minimapNodes = transformNodesToMinimap(nodes, bounds, scaleResult);

        // All nodes should fit within the minimap
        for (const node of minimapNodes) {
          expect(node.x).toBeGreaterThanOrEqual(-1); // Small tolerance for floating point
          expect(node.y).toBeGreaterThanOrEqual(-1);
          expect(node.x + node.width).toBeLessThanOrEqual(minimapSize.width + 1);
          expect(node.y + node.height).toBeLessThanOrEqual(minimapSize.height + 1);
        }
      }),
      { numRuns: 100 }
    );
  });

  it('scale factor is consistent regardless of node order', () => {
    fc.assert(
      fc.property(nodesArrayArb, minimapSizeArb, (nodes, minimapSize) => {
        // Calculate scale with original order
        const bounds1 = calculateNodeBounds(nodes);
        const scaleResult1 = calculateMinimapScale(bounds1, minimapSize);

        // Shuffle nodes and calculate again
        const shuffledNodes = [...nodes].reverse();
        const bounds2 = calculateNodeBounds(shuffledNodes);
        const scaleResult2 = calculateMinimapScale(bounds2, minimapSize);

        // Scale should be the same regardless of node order
        expect(scaleResult1.scale).toBeCloseTo(scaleResult2.scale, 5);
      }),
      { numRuns: 100 }
    );
  });

  it('bounds include all nodes with padding', () => {
    fc.assert(
      fc.property(nodesArrayArb, (nodes) => {
        const bounds = calculateNodeBounds(nodes, MINIMAP_PADDING);

        // All nodes should be within bounds (accounting for padding)
        for (const node of nodes) {
          expect(node.x).toBeGreaterThanOrEqual(bounds.minX);
          expect(node.y).toBeGreaterThanOrEqual(bounds.minY);
          expect(node.x + node.width).toBeLessThanOrEqual(bounds.maxX);
          expect(node.y + node.height).toBeLessThanOrEqual(bounds.maxY);
        }
      }),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Property 8: Minimap Click Navigation
// ============================================================================

describe('Property 8: Minimap Click Navigation', () => {
  /**
   * **Validates: Requirements 1.9**
   *
   * For any click at minimap position (mx, my), the viewport shall center on
   * the corresponding canvas position calculated by the inverse of the minimap
   * scale transformation.
   */
  it('minimap click centers viewport on corresponding canvas position', () => {
    fc.assert(
      fc.property(
        nodesArrayArb,
        minimapSizeArb,
        viewportArb,
        screenSizeArb,
        (nodes, minimapSize, viewport, screenSize) => {
          const bounds = calculateNodeBounds(nodes);
          const scaleResult = calculateMinimapScale(bounds, minimapSize);

          // Generate a click point within the minimap
          const minimapClickPoint = {
            x: minimapSize.width / 2,
            y: minimapSize.height / 2,
          };

          // Calculate the expected canvas position
          const expectedCanvasPoint = minimapToCanvas(minimapClickPoint, bounds, scaleResult);

          // Calculate new viewport from click
          const newViewport = calculateViewportFromMinimapClick(
            minimapClickPoint,
            viewport,
            screenSize,
            bounds,
            scaleResult
          );

          // The center of the new viewport should be at the clicked canvas position
          const visibleCanvasWidth = screenSize.width / viewport.zoom;
          const visibleCanvasHeight = screenSize.height / viewport.zoom;
          const viewportCenterX = newViewport.x + visibleCanvasWidth / 2;
          const viewportCenterY = newViewport.y + visibleCanvasHeight / 2;

          expect(viewportCenterX).toBeCloseTo(expectedCanvasPoint.x, 3);
          expect(viewportCenterY).toBeCloseTo(expectedCanvasPoint.y, 3);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('canvas to minimap to canvas is identity (round-trip)', () => {
    fc.assert(
      fc.property(nodesArrayArb, minimapSizeArb, (nodes, minimapSize) => {
        const bounds = calculateNodeBounds(nodes);
        const scaleResult = calculateMinimapScale(bounds, minimapSize);

        // Pick a canvas point within bounds
        const canvasPoint = {
          x: (bounds.minX + bounds.maxX) / 2,
          y: (bounds.minY + bounds.maxY) / 2,
        };

        // Convert to minimap and back
        const minimapPoint = canvasToMinimap(canvasPoint, bounds, scaleResult);
        const backToCanvas = minimapToCanvas(minimapPoint, bounds, scaleResult);

        // Should return to original position
        expect(backToCanvas.x).toBeCloseTo(canvasPoint.x, 4);
        expect(backToCanvas.y).toBeCloseTo(canvasPoint.y, 4);
      }),
      { numRuns: 100 }
    );
  });

  it('minimap to canvas to minimap is identity (round-trip)', () => {
    fc.assert(
      fc.property(nodesArrayArb, minimapSizeArb, (nodes, minimapSize) => {
        const bounds = calculateNodeBounds(nodes);
        const scaleResult = calculateMinimapScale(bounds, minimapSize);

        // Pick a minimap point within bounds
        const minimapPoint = {
          x: minimapSize.width / 2,
          y: minimapSize.height / 2,
        };

        // Convert to canvas and back
        const canvasPoint = minimapToCanvas(minimapPoint, bounds, scaleResult);
        const backToMinimap = canvasToMinimap(canvasPoint, bounds, scaleResult);

        // Should return to original position
        expect(backToMinimap.x).toBeCloseTo(minimapPoint.x, 4);
        expect(backToMinimap.y).toBeCloseTo(minimapPoint.y, 4);
      }),
      { numRuns: 100 }
    );
  });

  it('click preserves zoom level', () => {
    fc.assert(
      fc.property(
        nodesArrayArb,
        minimapSizeArb,
        viewportArb,
        screenSizeArb,
        (nodes, minimapSize, viewport, screenSize) => {
          const bounds = calculateNodeBounds(nodes);
          const scaleResult = calculateMinimapScale(bounds, minimapSize);

          const minimapClickPoint = {
            x: minimapSize.width / 3,
            y: minimapSize.height / 3,
          };

          const newViewport = calculateViewportFromMinimapClick(
            minimapClickPoint,
            viewport,
            screenSize,
            bounds,
            scaleResult
          );

          // Zoom should remain unchanged
          expect(newViewport.zoom).toBe(viewport.zoom);
        }
      ),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Viewport Indicator Properties
// ============================================================================

describe('Viewport Indicator Properties', () => {
  it('viewport indicator dimensions are proportional to visible area', () => {
    fc.assert(
      fc.property(
        nodesArrayArb,
        minimapSizeArb,
        viewportArb,
        screenSizeArb,
        (nodes, minimapSize, viewport, screenSize) => {
          const bounds = calculateNodeBounds(nodes);
          const scaleResult = calculateMinimapScale(bounds, minimapSize);
          const indicator = calculateViewportIndicator(viewport, screenSize, bounds, scaleResult);

          // The indicator dimensions should be proportional to the visible canvas area
          const visibleCanvasWidth = screenSize.width / viewport.zoom;
          const visibleCanvasHeight = screenSize.height / viewport.zoom;

          const expectedWidth = visibleCanvasWidth * scaleResult.scale;
          const expectedHeight = visibleCanvasHeight * scaleResult.scale;

          expect(indicator.width).toBeCloseTo(expectedWidth, 4);
          expect(indicator.height).toBeCloseTo(expectedHeight, 4);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('viewport indicator position corresponds to viewport position', () => {
    fc.assert(
      fc.property(
        nodesArrayArb,
        minimapSizeArb,
        viewportArb,
        screenSizeArb,
        (nodes, minimapSize, viewport, screenSize) => {
          const bounds = calculateNodeBounds(nodes);
          const scaleResult = calculateMinimapScale(bounds, minimapSize);
          const indicator = calculateViewportIndicator(viewport, screenSize, bounds, scaleResult);

          // The indicator position should correspond to the viewport position
          const expectedMinimapPos = canvasToMinimap(
            { x: viewport.x, y: viewport.y },
            bounds,
            scaleResult
          );

          expect(indicator.x).toBeCloseTo(expectedMinimapPos.x, 4);
          expect(indicator.y).toBeCloseTo(expectedMinimapPos.y, 4);
        }
      ),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Edge Cases
// ============================================================================

describe('Minimap Edge Cases', () => {
  it('handles empty nodes array with default bounds', () => {
    const bounds = calculateNodeBounds([]);

    expect(bounds.width).toBeGreaterThan(0);
    expect(bounds.height).toBeGreaterThan(0);
    expect(bounds.minX).toBeLessThan(bounds.maxX);
    expect(bounds.minY).toBeLessThan(bounds.maxY);
  });

  it('handles single node', () => {
    const singleNode: NodePosition = {
      x: 100,
      y: 200,
      width: DEFAULT_NODE_WIDTH,
      height: DEFAULT_NODE_HEIGHT,
    };

    const bounds = calculateNodeBounds([singleNode]);
    const minimapSize: MinimapDimensions = { width: 200, height: 150 };
    const scaleResult = calculateMinimapScale(bounds, minimapSize);
    const minimapNodes = transformNodesToMinimap([singleNode], bounds, scaleResult);

    expect(minimapNodes).toHaveLength(1);
    expect(minimapNodes[0].width).toBeGreaterThan(0);
    expect(minimapNodes[0].height).toBeGreaterThan(0);
  });

  it('handles nodes at same position', () => {
    const nodes: NodePosition[] = [
      { x: 100, y: 100, width: 150, height: 60 },
      { x: 100, y: 100, width: 150, height: 60 },
    ];

    const bounds = calculateNodeBounds(nodes);
    const minimapSize: MinimapDimensions = { width: 200, height: 150 };
    const scaleResult = calculateMinimapScale(bounds, minimapSize);

    expect(scaleResult.scale).toBeGreaterThan(0);
    expect(isFinite(scaleResult.scale)).toBe(true);
  });
});
