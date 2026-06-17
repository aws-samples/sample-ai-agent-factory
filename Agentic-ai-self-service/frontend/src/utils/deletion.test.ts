/**
 * Property-based tests for node deletion.
 * Property 6: Node Deletion Removes Node and Connected Edges
 * Validates: Requirements 1.7
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import type { Edge } from '@xyflow/react';
import {
  deleteNodeWithEdges,
  getConnectedEdges,
  nodeExists,
  edgeReferencesNode,
  createNode,
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
    const seen = new Set<string>();
    return nodes.filter((node) => {
      if (seen.has(node.id)) return false;
      seen.add(node.id);
      return true;
    });
  });

// Generate edges that reference existing nodes
const edgesForNodesArb = (nodes: AgentCoreNode[]): fc.Arbitrary<Edge[]> => {
  if (nodes.length < 2) {
    return fc.constant([]);
  }

  const nodeIds = nodes.map((n) => n.id);

  return fc.array(
    fc.record({
      id: fc.string({ minLength: 5, maxLength: 20 }),
      source: fc.constantFrom(...nodeIds),
      target: fc.constantFrom(...nodeIds),
      sourceHandle: fc.constant(null),
      targetHandle: fc.constant(null),
    }),
    { minLength: 0, maxLength: Math.min(10, nodes.length * 2) }
  ).map((edges) => {
    // Filter out self-loops and ensure unique IDs
    const seen = new Set<string>();
    return edges.filter((edge) => {
      if (edge.source === edge.target) return false;
      if (seen.has(edge.id)) return false;
      seen.add(edge.id);
      return true;
    });
  });
};

// ============================================================================
// Property 6: Node Deletion Removes Node and Connected Edges
// ============================================================================

describe('Property 6: Node Deletion Removes Node and Connected Edges', () => {
  /**
   * **Validates: Requirements 1.7**
   *
   * For any node deletion operation, the workflow shall contain neither
   * the deleted node nor any edges that referenced the deleted node
   * as source or target.
   */
  it('deleted node is removed from nodes array', () => {
    fc.assert(
      fc.property(nodesArrayArb(1, 10), (nodes) => {
        const nodeToDelete = nodes[0];
        const { nodes: resultNodes } = deleteNodeWithEdges(nodes, [], nodeToDelete.id);

        // The deleted node should not exist in the result
        expect(nodeExists(resultNodes, nodeToDelete.id)).toBe(false);
      }),
      { numRuns: 100 }
    );
  });

  it('all edges connected to deleted node are removed', () => {
    fc.assert(
      fc.property(
        nodesArrayArb(2, 10).chain((nodes) =>
          edgesForNodesArb(nodes).map((edges) => ({ nodes, edges }))
        ),
        ({ nodes, edges }) => {
          const nodeToDelete = nodes[0];
          const { edges: resultEdges } = deleteNodeWithEdges(nodes, edges, nodeToDelete.id);

          // No edge should reference the deleted node
          for (const edge of resultEdges) {
            expect(edgeReferencesNode(edge, nodeToDelete.id)).toBe(false);
          }
        }
      ),
      { numRuns: 100 }
    );
  });

  it('other nodes are preserved after deletion', () => {
    fc.assert(
      fc.property(nodesArrayArb(2, 10), (nodes) => {
        const nodeToDelete = nodes[0];
        const otherNodes = nodes.slice(1);
        const { nodes: resultNodes } = deleteNodeWithEdges(nodes, [], nodeToDelete.id);

        // All other nodes should still exist
        for (const node of otherNodes) {
          expect(nodeExists(resultNodes, node.id)).toBe(true);
        }
      }),
      { numRuns: 100 }
    );
  });

  it('unconnected edges are preserved after deletion', () => {
    fc.assert(
      fc.property(
        nodesArrayArb(3, 10).chain((nodes) =>
          edgesForNodesArb(nodes).map((edges) => ({ nodes, edges }))
        ),
        ({ nodes, edges }) => {
          const nodeToDelete = nodes[0];
          const unconnectedEdges = edges.filter(
            (edge) => !edgeReferencesNode(edge, nodeToDelete.id)
          );

          const { edges: resultEdges } = deleteNodeWithEdges(nodes, edges, nodeToDelete.id);

          // All unconnected edges should still exist
          for (const edge of unconnectedEdges) {
            const stillExists = resultEdges.some((e) => e.id === edge.id);
            expect(stillExists).toBe(true);
          }
        }
      ),
      { numRuns: 100 }
    );
  });

  it('node count decreases by exactly one', () => {
    fc.assert(
      fc.property(nodesArrayArb(1, 10), (nodes) => {
        const nodeToDelete = nodes[0];
        const { nodes: resultNodes } = deleteNodeWithEdges(nodes, [], nodeToDelete.id);

        expect(resultNodes.length).toBe(nodes.length - 1);
      }),
      { numRuns: 100 }
    );
  });

  it('deleting non-existent node does not change nodes array', () => {
    fc.assert(
      fc.property(nodesArrayArb(1, 10), (nodes) => {
        const nonExistentId = 'non-existent-' + Date.now();
        const { nodes: resultNodes } = deleteNodeWithEdges(nodes, [], nonExistentId);

        expect(resultNodes.length).toBe(nodes.length);
      }),
      { numRuns: 100 }
    );
  });

  it('getConnectedEdges returns all edges referencing a node', () => {
    fc.assert(
      fc.property(
        nodesArrayArb(2, 10).chain((nodes) =>
          edgesForNodesArb(nodes).map((edges) => ({ nodes, edges }))
        ),
        ({ nodes, edges }) => {
          const targetNode = nodes[0];
          const connectedEdges = getConnectedEdges(edges, targetNode.id);

          // All returned edges should reference the target node
          for (const edge of connectedEdges) {
            expect(edgeReferencesNode(edge, targetNode.id)).toBe(true);
          }

          // All edges referencing the target node should be in the result
          const expectedCount = edges.filter((e) => edgeReferencesNode(e, targetNode.id)).length;
          expect(connectedEdges.length).toBe(expectedCount);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('edgeReferencesNode correctly identifies source and target references', () => {
    fc.assert(
      fc.property(nodeIdArb, nodeIdArb, nodeIdArb, (sourceId, targetId, otherId) => {
        const edge: Edge = {
          id: 'test-edge',
          source: sourceId,
          target: targetId,
          sourceHandle: null,
          targetHandle: null,
        };

        // Should return true for source
        expect(edgeReferencesNode(edge, sourceId)).toBe(true);

        // Should return true for target
        expect(edgeReferencesNode(edge, targetId)).toBe(true);

        // Should return false for unrelated node (unless it happens to match)
        if (otherId !== sourceId && otherId !== targetId) {
          expect(edgeReferencesNode(edge, otherId)).toBe(false);
        }
      }),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Integration Test: Full Deletion Workflow
// ============================================================================

describe('Node Deletion Integration', () => {
  it('complete deletion workflow removes node and all connected edges', () => {
    // Create a specific test scenario
    const nodes: AgentCoreNode[] = [
      createNode('node-1', 'runtime', { x: 0, y: 0 }, 'Runtime 1'),
      createNode('node-2', 'gateway', { x: 100, y: 0 }, 'Gateway 1'),
      createNode('node-3', 'identity', { x: 200, y: 0 }, 'Identity 1'),
    ];

    const edges: Edge[] = [
      { id: 'edge-1-2', source: 'node-1', target: 'node-2', sourceHandle: null, targetHandle: null },
      { id: 'edge-2-3', source: 'node-2', target: 'node-3', sourceHandle: null, targetHandle: null },
      { id: 'edge-1-3', source: 'node-1', target: 'node-3', sourceHandle: null, targetHandle: null },
    ];

    // Delete node-1
    const result = deleteNodeWithEdges(nodes, edges, 'node-1');

    // Node-1 should be removed
    expect(nodeExists(result.nodes, 'node-1')).toBe(false);
    expect(result.nodes.length).toBe(2);

    // Edges connected to node-1 should be removed
    expect(result.edges.some((e) => e.id === 'edge-1-2')).toBe(false);
    expect(result.edges.some((e) => e.id === 'edge-1-3')).toBe(false);

    // Edge between node-2 and node-3 should remain
    expect(result.edges.some((e) => e.id === 'edge-2-3')).toBe(true);
    expect(result.edges.length).toBe(1);
  });
});
