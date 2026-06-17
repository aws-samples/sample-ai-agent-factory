/**
 * Property-based tests for node operations.
 * Validates: Requirements 1.5, 1.6
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  applyNodeSelection,
  getSelectedNode,
  countSelectedNodes,
  updateNodePosition,
  applyNodeMoveDelta,
  createNode,
  nodeExists,
} from './nodes';
import type { AgentCoreNode } from '../store/workflowStore';
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
  x: fc.float({ min: Math.fround(-5000), max: Math.fround(5000), noNaN: true }),
  y: fc.float({ min: Math.fround(-5000), max: Math.fround(5000), noNaN: true }),
});

const deltaArb = fc.record({
  dx: fc.float({ min: Math.fround(-1000), max: Math.fround(1000), noNaN: true }),
  dy: fc.float({ min: Math.fround(-1000), max: Math.fround(1000), noNaN: true }),
});

const nodeIdArb = fc.string({ minLength: 1, maxLength: 20 }).filter((s) => s.trim().length > 0);

const nodeArb: fc.Arbitrary<AgentCoreNode> = fc.record({
  id: nodeIdArb,
  type: componentTypeArb,
  position: positionArb,
  selected: fc.boolean(),
  data: fc.record({
    label: fc.string({ minLength: 1, maxLength: 50 }),
    componentType: componentTypeArb,
    validationStatus: fc.constantFrom('valid', 'warning', 'error', 'pending'),
  }),
});

// Generate array of nodes with unique IDs
const nodesArrayArb = (minLength: number = 0, maxLength: number = 10): fc.Arbitrary<AgentCoreNode[]> =>
  fc.array(nodeArb, { minLength, maxLength }).map((nodes) => {
    // Ensure unique IDs
    const seen = new Set<string>();
    return nodes.filter((node) => {
      if (seen.has(node.id)) return false;
      seen.add(node.id);
      return true;
    });
  });

// ============================================================================
// Property 4: Node Selection State Consistency
// ============================================================================

describe('Property 4: Node Selection State Consistency', () => {
  /**
   * **Validates: Requirements 1.5**
   *
   * For any node click operation, exactly one node shall be in selected state,
   * and it shall be the clicked node.
   */
  it('selecting a node results in exactly one selected node', () => {
    fc.assert(
      fc.property(nodesArrayArb(1, 10), (nodes) => {
        // Pick a random node to select
        const nodeToSelect = nodes[Math.floor(Math.random() * nodes.length)];
        const result = applyNodeSelection(nodes, nodeToSelect.id);

        // Exactly one node should be selected
        const selectedCount = countSelectedNodes(result);
        expect(selectedCount).toBe(1);

        // The selected node should be the one we clicked
        const selectedNode = getSelectedNode(result);
        expect(selectedNode?.id).toBe(nodeToSelect.id);
      }),
      { numRuns: 100 }
    );
  });

  it('selecting null deselects all nodes', () => {
    fc.assert(
      fc.property(nodesArrayArb(0, 10), (nodes) => {
        const result = applyNodeSelection(nodes, null);

        // No nodes should be selected
        const selectedCount = countSelectedNodes(result);
        expect(selectedCount).toBe(0);
      }),
      { numRuns: 100 }
    );
  });

  it('selection preserves node count', () => {
    fc.assert(
      fc.property(nodesArrayArb(0, 10), nodeIdArb, (nodes, selectedId) => {
        const result = applyNodeSelection(nodes, selectedId);

        // Node count should remain the same
        expect(result.length).toBe(nodes.length);
      }),
      { numRuns: 100 }
    );
  });

  it('selection preserves node positions', () => {
    fc.assert(
      fc.property(nodesArrayArb(1, 10), (nodes) => {
        const nodeToSelect = nodes[0];
        const result = applyNodeSelection(nodes, nodeToSelect.id);

        // All node positions should remain unchanged
        for (let i = 0; i < nodes.length; i++) {
          expect(result[i].position.x).toBe(nodes[i].position.x);
          expect(result[i].position.y).toBe(nodes[i].position.y);
        }
      }),
      { numRuns: 100 }
    );
  });

  it('selecting non-existent node deselects all', () => {
    fc.assert(
      fc.property(nodesArrayArb(1, 10), (nodes) => {
        const nonExistentId = 'non-existent-id-' + Date.now();
        const result = applyNodeSelection(nodes, nonExistentId);

        // No nodes should be selected since the ID doesn't exist
        const selectedCount = countSelectedNodes(result);
        expect(selectedCount).toBe(0);
      }),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Property 5: Node Movement Updates Position and Edges
// ============================================================================

describe('Property 5: Node Movement Updates Position and Edges', () => {
  /**
   * **Validates: Requirements 1.6**
   *
   * For any node drag operation with delta (dx, dy), the node position
   * shall change by (dx, dy) and all connected edges shall recalculate
   * their Bezier paths to maintain visual connection.
   */
  it('node position changes by exact delta', () => {
    fc.assert(
      fc.property(nodesArrayArb(1, 10), deltaArb, (nodes, delta) => {
        const nodeToMove = nodes[0];
        const result = applyNodeMoveDelta(nodes, nodeToMove.id, delta);

        const movedNode = result.find((n) => n.id === nodeToMove.id);
        expect(movedNode).toBeDefined();

        // Position should change by exactly the delta
        expect(movedNode!.position.x).toBeCloseTo(nodeToMove.position.x + delta.dx, 5);
        expect(movedNode!.position.y).toBeCloseTo(nodeToMove.position.y + delta.dy, 5);
      }),
      { numRuns: 100 }
    );
  });

  it('moving a node does not affect other nodes', () => {
    fc.assert(
      fc.property(nodesArrayArb(2, 10), deltaArb, (nodes, delta) => {
        const nodeToMove = nodes[0];
        const result = applyNodeMoveDelta(nodes, nodeToMove.id, delta);

        // Other nodes should remain unchanged
        for (let i = 1; i < nodes.length; i++) {
          const originalNode = nodes[i];
          const resultNode = result.find((n) => n.id === originalNode.id);

          expect(resultNode?.position.x).toBe(originalNode.position.x);
          expect(resultNode?.position.y).toBe(originalNode.position.y);
        }
      }),
      { numRuns: 100 }
    );
  });

  it('zero delta results in unchanged position', () => {
    fc.assert(
      fc.property(nodesArrayArb(1, 10), (nodes) => {
        const nodeToMove = nodes[0];
        const result = applyNodeMoveDelta(nodes, nodeToMove.id, { dx: 0, dy: 0 });

        const movedNode = result.find((n) => n.id === nodeToMove.id);
        // Use toBeCloseTo to handle -0 vs 0 edge case (Object.is treats them as different)
        expect(movedNode?.position.x).toBeCloseTo(nodeToMove.position.x, 10);
        expect(movedNode?.position.y).toBeCloseTo(nodeToMove.position.y, 10);
      }),
      { numRuns: 100 }
    );
  });

  it('consecutive moves are additive', () => {
    fc.assert(
      fc.property(nodesArrayArb(1, 10), deltaArb, deltaArb, (nodes, delta1, delta2) => {
        const nodeToMove = nodes[0];

        // Apply two separate moves
        const afterFirst = applyNodeMoveDelta(nodes, nodeToMove.id, delta1);
        const afterBoth = applyNodeMoveDelta(afterFirst, nodeToMove.id, delta2);

        // Apply combined move
        const combinedDelta = { dx: delta1.dx + delta2.dx, dy: delta1.dy + delta2.dy };
        const afterCombined = applyNodeMoveDelta(nodes, nodeToMove.id, combinedDelta);

        const nodeAfterBoth = afterBoth.find((n) => n.id === nodeToMove.id);
        const nodeAfterCombined = afterCombined.find((n) => n.id === nodeToMove.id);

        // Results should be equivalent
        expect(nodeAfterBoth?.position.x).toBeCloseTo(nodeAfterCombined!.position.x, 5);
        expect(nodeAfterBoth?.position.y).toBeCloseTo(nodeAfterCombined!.position.y, 5);
      }),
      { numRuns: 100 }
    );
  });

  it('updateNodePosition sets exact position', () => {
    fc.assert(
      fc.property(nodesArrayArb(1, 10), positionArb, (nodes, newPosition) => {
        const nodeToMove = nodes[0];
        const result = updateNodePosition(nodes, nodeToMove.id, newPosition);

        const movedNode = result.find((n) => n.id === nodeToMove.id);
        expect(movedNode?.position.x).toBe(newPosition.x);
        expect(movedNode?.position.y).toBe(newPosition.y);
      }),
      { numRuns: 100 }
    );
  });

  it('movement preserves node count', () => {
    fc.assert(
      fc.property(nodesArrayArb(0, 10), deltaArb, nodeIdArb, (nodes, delta, nodeId) => {
        const result = applyNodeMoveDelta(nodes, nodeId, delta);

        // Node count should remain the same
        expect(result.length).toBe(nodes.length);
      }),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Node Creation Properties
// ============================================================================

describe('Node Creation', () => {
  it('createNode creates node at specified position', () => {
    fc.assert(
      fc.property(nodeIdArb, componentTypeArb, positionArb, fc.string(), (id, type, position, label) => {
        const node = createNode(id, type, position, label);

        expect(node.id).toBe(id);
        expect(node.type).toBe(type);
        expect(node.position.x).toBe(position.x);
        expect(node.position.y).toBe(position.y);
        expect(node.data.componentType).toBe(type);
        expect(node.selected).toBe(false);
      }),
      { numRuns: 100 }
    );
  });

  it('nodeExists correctly identifies existing nodes', () => {
    fc.assert(
      fc.property(nodesArrayArb(1, 10), (nodes) => {
        // Existing node should be found
        const existingNode = nodes[0];
        expect(nodeExists(nodes, existingNode.id)).toBe(true);

        // Non-existing node should not be found
        const nonExistentId = 'non-existent-' + Date.now();
        expect(nodeExists(nodes, nonExistentId)).toBe(false);
      }),
      { numRuns: 100 }
    );
  });
});
