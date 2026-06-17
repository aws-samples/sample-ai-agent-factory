/**
 * Property-based tests for drag-drop operations.
 * Validates: Requirements 1.2, 12.3
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  calculateDropPosition,
  calculateGhostPosition,
  createNodeFromDrop,
} from './dragDrop';
import type { AgentCoreComponentType } from '../types/workflow';

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

const componentTypeArb: fc.Arbitrary<AgentCoreComponentType> = fc.constantFrom(
  'runtime',
  'gateway',
  'memory',
  'code_interpreter',
  'browser',
  'observability',
  'identity'
);

const positionArb = fc.record({
  x: fc.float({ min: Math.fround(0), max: Math.fround(2000), noNaN: true }),
  y: fc.float({ min: Math.fround(0), max: Math.fround(2000), noNaN: true }),
});

const viewportArb = fc.record({
  x: fc.float({ min: Math.fround(-1000), max: Math.fround(1000), noNaN: true }),
  y: fc.float({ min: Math.fround(-1000), max: Math.fround(1000), noNaN: true }),
  zoom: fc.float({ min: Math.fround(0.1), max: Math.fround(4), noNaN: true }),
});

const canvasRectArb = fc.record({
  left: fc.float({ min: Math.fround(0), max: Math.fround(500), noNaN: true }),
  top: fc.float({ min: Math.fround(0), max: Math.fround(500), noNaN: true }),
  width: fc.float({ min: Math.fround(500), max: Math.fround(2000), noNaN: true }),
  height: fc.float({ min: Math.fround(500), max: Math.fround(2000), noNaN: true }),
}).map((rect) => ({
  ...rect,
  right: rect.left + rect.width,
  bottom: rect.top + rect.height,
  x: rect.left,
  y: rect.top,
  toJSON: () => rect,
} as DOMRect));

// ============================================================================
// Property 1: Node Creation at Drop Position
// ============================================================================

describe('Property 1: Node Creation at Drop Position', () => {
  /**
   * **Validates: Requirements 1.2**
   *
   * For any component type dragged from the palette and for any valid drop position
   * on the canvas, the created node's position shall equal the drop coordinates.
   */
  it('created node position equals calculated drop coordinates', () => {
    fc.assert(
      fc.property(componentTypeArb, positionArb, (componentType, position) => {
        const node = createNodeFromDrop(componentType, position);

        // Node position should exactly match the drop position
        expect(node.position.x).toBe(position.x);
        expect(node.position.y).toBe(position.y);
      }),
      { numRuns: 100 }
    );
  });

  it('drop position calculation is deterministic for same inputs', () => {
    fc.assert(
      fc.property(
        positionArb,
        canvasRectArb,
        viewportArb,
        (clientPos, canvasRect, viewport) => {
          const pos1 = calculateDropPosition(clientPos.x, clientPos.y, canvasRect, viewport);
          const pos2 = calculateDropPosition(clientPos.x, clientPos.y, canvasRect, viewport);

          // Same inputs should produce same outputs
          expect(pos1.x).toBe(pos2.x);
          expect(pos1.y).toBe(pos2.y);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('drop position accounts for viewport pan', () => {
    fc.assert(
      fc.property(
        positionArb,
        canvasRectArb,
        fc.float({ min: Math.fround(-500), max: Math.fround(500), noNaN: true }),
        fc.float({ min: Math.fround(-500), max: Math.fround(500), noNaN: true }),
        (clientPos, canvasRect, panX, panY) => {
          const viewport1 = { x: 0, y: 0, zoom: 1 };
          const viewport2 = { x: panX, y: panY, zoom: 1 };

          const pos1 = calculateDropPosition(clientPos.x, clientPos.y, canvasRect, viewport1);
          const pos2 = calculateDropPosition(clientPos.x, clientPos.y, canvasRect, viewport2);

          // Panning the viewport should shift the drop position inversely
          expect(pos2.x).toBeCloseTo(pos1.x - panX, 3);
          expect(pos2.y).toBeCloseTo(pos1.y - panY, 3);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('drop position accounts for viewport zoom', () => {
    fc.assert(
      fc.property(
        positionArb,
        canvasRectArb,
        fc.float({ min: Math.fround(0.5), max: Math.fround(2), noNaN: true }),
        (clientPos, canvasRect, zoom) => {
          const viewportWithZoom = { x: 0, y: 0, zoom };

          const posWithZoom = calculateDropPosition(clientPos.x, clientPos.y, canvasRect, viewportWithZoom);

          // Zooming should scale the drop position
          const relativeX = clientPos.x - canvasRect.left;
          const relativeY = clientPos.y - canvasRect.top;

          expect(posWithZoom.x).toBeCloseTo(relativeX / zoom, 3);
          expect(posWithZoom.y).toBeCloseTo(relativeY / zoom, 3);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('created node has correct component type', () => {
    fc.assert(
      fc.property(componentTypeArb, positionArb, (componentType, position) => {
        const node = createNodeFromDrop(componentType, position);

        expect(node.type).toBe(componentType);
        expect(node.data.componentType).toBe(componentType);
      }),
      { numRuns: 100 }
    );
  });

  it('created node has unique ID', () => {
    fc.assert(
      fc.property(componentTypeArb, positionArb, (componentType, position) => {
        const node1 = createNodeFromDrop(componentType, position);
        const node2 = createNodeFromDrop(componentType, position);

        // Each node should have a unique ID
        expect(node1.id).not.toBe(node2.id);
      }),
      { numRuns: 100 }
    );
  });

  it('created node starts unselected with pending validation', () => {
    fc.assert(
      fc.property(componentTypeArb, positionArb, (componentType, position) => {
        const node = createNodeFromDrop(componentType, position);

        expect(node.selected).toBe(false);
        expect(node.data.validationStatus).toBe('pending');
      }),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Property 42: Drag Ghost Preview
// ============================================================================

describe('Property 42: Drag Ghost Preview', () => {
  /**
   * **Validates: Requirements 12.3**
   *
   * For any component drag from palette, a ghost preview of the component
   * shall follow the cursor position.
   */
  it('ghost position follows cursor relative to canvas', () => {
    fc.assert(
      fc.property(
        positionArb,
        canvasRectArb,
        (clientPos, canvasRect) => {
          const ghostPos = calculateGhostPosition(clientPos.x, clientPos.y, canvasRect);

          // Ghost position should be relative to canvas
          expect(ghostPos.x).toBe(clientPos.x - canvasRect.left);
          expect(ghostPos.y).toBe(clientPos.y - canvasRect.top);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('ghost position calculation is deterministic', () => {
    fc.assert(
      fc.property(
        positionArb,
        canvasRectArb,
        (clientPos, canvasRect) => {
          const pos1 = calculateGhostPosition(clientPos.x, clientPos.y, canvasRect);
          const pos2 = calculateGhostPosition(clientPos.x, clientPos.y, canvasRect);

          expect(pos1.x).toBe(pos2.x);
          expect(pos1.y).toBe(pos2.y);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('ghost position changes linearly with cursor movement', () => {
    fc.assert(
      fc.property(
        positionArb,
        canvasRectArb,
        fc.float({ min: Math.fround(-100), max: Math.fround(100), noNaN: true }),
        fc.float({ min: Math.fround(-100), max: Math.fround(100), noNaN: true }),
        (clientPos, canvasRect, deltaX, deltaY) => {
          const pos1 = calculateGhostPosition(clientPos.x, clientPos.y, canvasRect);
          const pos2 = calculateGhostPosition(clientPos.x + deltaX, clientPos.y + deltaY, canvasRect);

          // Ghost should move by the same delta as cursor
          expect(pos2.x - pos1.x).toBeCloseTo(deltaX, 5);
          expect(pos2.y - pos1.y).toBeCloseTo(deltaY, 5);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('ghost position is independent of viewport state', () => {
    fc.assert(
      fc.property(
        positionArb,
        canvasRectArb,
        (clientPos, canvasRect) => {
          // Ghost position should only depend on client position and canvas rect
          // It should NOT depend on viewport (pan/zoom)
          const ghostPos = calculateGhostPosition(clientPos.x, clientPos.y, canvasRect);

          // The ghost is rendered in screen coordinates, not canvas coordinates
          // So it should be a simple offset from canvas origin
          expect(ghostPos.x).toBeGreaterThanOrEqual(clientPos.x - canvasRect.right);
          expect(ghostPos.y).toBeGreaterThanOrEqual(clientPos.y - canvasRect.bottom);
        }
      ),
      { numRuns: 100 }
    );
  });
});
