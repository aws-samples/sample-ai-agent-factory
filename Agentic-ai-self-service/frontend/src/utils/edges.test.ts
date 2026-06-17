/**
 * Property-based tests for edge operations.
 * Validates: Requirements 2.4, 2.5, 2.6, 2.7, 2.8
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import type { Edge } from '@xyflow/react';
import {
  calculateBezierControlPoints,
  generateBezierPath,
  isValidBezierPath,
  getEdgeColor,
  determineConnectionType,
  areComponentsCompatible,
  getCompatibleTargets,
  createEdgeIfCompatible,
  applyEdgeSelection,
  getSelectedEdge,
  countSelectedEdges,
  deleteEdge,
  edgeExists,
  findEdgeByNodes,
} from './edges';
import { CONNECTION_COLORS, CONNECTION_COMPATIBILITY } from '../types/validation';
import type { AgentCoreComponentType, ConnectionType } from '../types/workflow';
import type { AgentCoreNode } from '../store/workflowStore';

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

const connectionTypeArb: fc.Arbitrary<ConnectionType> = fc.constantFrom(
  'data',
  'tool',
  'identity'
);

const coordinateArb = fc.float({ min: Math.fround(-5000), max: Math.fround(5000), noNaN: true });

const edgeIdArb = fc.string({ minLength: 1, maxLength: 20 }).filter((s) => s.trim().length > 0);

const nodeIdArb = fc.string({ minLength: 1, maxLength: 20 }).filter((s) => s.trim().length > 0);

const edgeArb: fc.Arbitrary<Edge> = fc.record({
  id: edgeIdArb,
  source: nodeIdArb,
  target: nodeIdArb,
  sourceHandle: fc.constant(null),
  targetHandle: fc.constant(null),
  selected: fc.boolean(),
  type: fc.constant('connection'),
  data: fc.record({
    connectionType: connectionTypeArb,
  }),
});

// Generate array of edges with unique IDs
const edgesArrayArb = (minLength: number = 0, maxLength: number = 10): fc.Arbitrary<Edge[]> =>
  fc.array(edgeArb, { minLength, maxLength }).map((edges) => {
    const seen = new Set<string>();
    return edges.filter((edge) => {
      if (seen.has(edge.id)) return false;
      seen.add(edge.id);
      return true;
    });
  });

const nodeArb: fc.Arbitrary<AgentCoreNode> = fc.record({
  id: nodeIdArb,
  type: componentTypeArb,
  position: fc.record({
    x: coordinateArb,
    y: coordinateArb,
  }),
  selected: fc.boolean(),
  data: fc.record({
    label: fc.string({ minLength: 1, maxLength: 50 }),
    componentType: componentTypeArb,
    validationStatus: fc.constantFrom('valid', 'warning', 'error', 'pending'),
  }),
});

// ============================================================================
// Property 11: Bezier Curve Path Validity
// ============================================================================

describe('Property 11: Bezier Curve Path Validity', () => {
  /**
   * **Validates: Requirements 2.4**
   *
   * For any edge connecting two ports, the rendered path shall be a valid
   * cubic Bezier curve with control points calculated to create smooth curvature.
   */
  it('generates valid cubic Bezier path for any coordinates', () => {
    fc.assert(
      fc.property(
        coordinateArb,
        coordinateArb,
        coordinateArb,
        coordinateArb,
        (sourceX, sourceY, targetX, targetY) => {
          const path = generateBezierPath(sourceX, sourceY, targetX, targetY);

          // Path should be a valid Bezier curve
          expect(isValidBezierPath(path)).toBe(true);

          // Path should start with M (moveto) command
          expect(path.startsWith('M')).toBe(true);

          // Path should contain C (cubic Bezier) command
          expect(path.includes('C')).toBe(true);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('control points create smooth horizontal curvature', () => {
    fc.assert(
      fc.property(
        coordinateArb,
        coordinateArb,
        coordinateArb,
        coordinateArb,
        (sourceX, sourceY, targetX, targetY) => {
          const { sourceControlX, sourceControlY, targetControlX, targetControlY } =
            calculateBezierControlPoints(sourceX, sourceY, targetX, targetY);

          // Source control point should be to the right of source
          expect(sourceControlX).toBeGreaterThanOrEqual(sourceX);

          // Target control point should be to the left of target
          expect(targetControlX).toBeLessThanOrEqual(targetX);

          // Control points should maintain Y coordinates for horizontal flow
          expect(sourceControlY).toBe(sourceY);
          expect(targetControlY).toBe(targetY);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('control offset is at least 50px for smooth curves', () => {
    fc.assert(
      fc.property(
        coordinateArb,
        coordinateArb,
        coordinateArb,
        coordinateArb,
        (sourceX, sourceY, targetX, targetY) => {
          const { sourceControlX, targetControlX } =
            calculateBezierControlPoints(sourceX, sourceY, targetX, targetY);

          // Control offset should be at least 50px
          const sourceOffset = sourceControlX - sourceX;
          const targetOffset = targetX - targetControlX;

          expect(sourceOffset).toBeGreaterThanOrEqual(50);
          expect(targetOffset).toBeGreaterThanOrEqual(50);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('path contains source and target coordinates', () => {
    fc.assert(
      fc.property(
        coordinateArb,
        coordinateArb,
        coordinateArb,
        coordinateArb,
        (sourceX, sourceY, targetX, targetY) => {
          const path = generateBezierPath(sourceX, sourceY, targetX, targetY);

          // Path should contain source coordinates at the start
          expect(path).toContain(`M ${sourceX},${sourceY}`);

          // Path should end with target coordinates
          expect(path).toContain(`${targetX},${targetY}`);
        }
      ),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Property 12: Connection Color by Type
// ============================================================================

describe('Property 12: Connection Color by Type', () => {
  /**
   * **Validates: Requirements 2.5, 2.6, 2.7**
   *
   * For any edge with connection type T, the rendered color shall be:
   * - blue (#3B82F6) for data
   * - green (#22C55E) for authentication
   * - orange (#F97316) for policy
   */
  it('returns correct color for each connection type', () => {
    fc.assert(
      fc.property(connectionTypeArb, (connectionType) => {
        const color = getEdgeColor(connectionType);

        // Color should match the expected value from CONNECTION_COLORS
        expect(color).toBe(CONNECTION_COLORS[connectionType]);
      }),
      { numRuns: 100 }
    );
  });

  it('data connections are blue (#3B82F6)', () => {
    const color = getEdgeColor('data');
    expect(color).toBe('#3B82F6');
  });

  it('tool connections are green (#22C55E)', () => {
    const color = getEdgeColor('tool');
    expect(color).toBe('#22C55E');
  });

  it('identity connections are orange (#F97316)', () => {
    const color = getEdgeColor('identity');
    expect(color).toBe('#F97316');
  });

  it('determines correct connection type based on component types', () => {
    // Identity component should result in identity connection
    expect(determineConnectionType('identity', 'runtime')).toBe('identity');
    expect(determineConnectionType('runtime', 'identity')).toBe('identity');

    // Tool components should result in tool connection
    expect(determineConnectionType('memory', 'runtime')).toBe('tool');
    expect(determineConnectionType('code_interpreter', 'runtime')).toBe('tool');
    expect(determineConnectionType('browser', 'runtime')).toBe('tool');

    // Runtime to gateway should be data connection
    expect(determineConnectionType('runtime', 'gateway')).toBe('data');
    expect(determineConnectionType('gateway', 'runtime')).toBe('data');
  });

  it('color is always a valid hex color', () => {
    fc.assert(
      fc.property(connectionTypeArb, (connectionType) => {
        const color = getEdgeColor(connectionType);

        // Should be a valid hex color format
        expect(color).toMatch(/^#[0-9A-Fa-f]{6}$/);
      }),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Property 13: Edge Selection on Click
// ============================================================================

describe('Property 13: Edge Selection on Click', () => {
  /**
   * **Validates: Requirements 2.8**
   *
   * For any edge click operation, the clicked edge shall enter selected state
   * and display delete option.
   */
  it('selecting an edge results in exactly one selected edge', () => {
    fc.assert(
      fc.property(edgesArrayArb(1, 10), (edges) => {
        // Pick a random edge to select
        const edgeToSelect = edges[Math.floor(Math.random() * edges.length)];
        const result = applyEdgeSelection(edges, edgeToSelect.id);

        // Exactly one edge should be selected
        const selectedCount = countSelectedEdges(result);
        expect(selectedCount).toBe(1);

        // The selected edge should be the one we clicked
        const selectedEdge = getSelectedEdge(result);
        expect(selectedEdge?.id).toBe(edgeToSelect.id);
      }),
      { numRuns: 100 }
    );
  });

  it('selecting null deselects all edges', () => {
    fc.assert(
      fc.property(edgesArrayArb(0, 10), (edges) => {
        const result = applyEdgeSelection(edges, null);

        // No edges should be selected
        const selectedCount = countSelectedEdges(result);
        expect(selectedCount).toBe(0);
      }),
      { numRuns: 100 }
    );
  });

  it('selection preserves edge count', () => {
    fc.assert(
      fc.property(edgesArrayArb(0, 10), edgeIdArb, (edges, selectedId) => {
        const result = applyEdgeSelection(edges, selectedId);

        // Edge count should remain the same
        expect(result.length).toBe(edges.length);
      }),
      { numRuns: 100 }
    );
  });

  it('selection preserves edge source and target', () => {
    fc.assert(
      fc.property(edgesArrayArb(1, 10), (edges) => {
        const edgeToSelect = edges[0];
        const result = applyEdgeSelection(edges, edgeToSelect.id);

        // All edge sources and targets should remain unchanged
        for (let i = 0; i < edges.length; i++) {
          expect(result[i].source).toBe(edges[i].source);
          expect(result[i].target).toBe(edges[i].target);
        }
      }),
      { numRuns: 100 }
    );
  });

  it('selecting non-existent edge deselects all', () => {
    fc.assert(
      fc.property(edgesArrayArb(1, 10), (edges) => {
        const nonExistentId = 'non-existent-id-' + Date.now();
        const result = applyEdgeSelection(edges, nonExistentId);

        // No edges should be selected since the ID doesn't exist
        const selectedCount = countSelectedEdges(result);
        expect(selectedCount).toBe(0);
      }),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Property 9: Compatible Connection Creates Edge
// Property 10: Incompatible Connection Rejected
// ============================================================================

describe('Property 9 & 10: Connection Compatibility', () => {
  /**
   * **Property 9: Compatible Connection Creates Edge**
   * **Validates: Requirements 2.2**
   *
   * For any connection attempt from source port to target port where the source
   * and target component types are in the compatibility matrix, an edge shall
   * be created connecting the two ports.
   */
  it('Property 9: areComponentsCompatible returns true for compatible pairs', () => {
    // Test all compatible pairs from the matrix
    Object.entries(CONNECTION_COMPATIBILITY).forEach(([source, targets]) => {
      targets.forEach((target) => {
        expect(areComponentsCompatible(source as AgentCoreComponentType, target)).toBe(true);
      });
    });
  });

  /**
   * **Property 10: Incompatible Connection Rejected**
   * **Validates: Requirements 2.3**
   *
   * For any connection attempt from source port to target port where the source
   * and target component types are NOT in the compatibility matrix, no edge
   * shall be created.
   */
  it('Property 10: areComponentsCompatible returns false for incompatible pairs', () => {
    // Gateway cannot connect to gateway
    expect(areComponentsCompatible('gateway', 'gateway')).toBe(false);

    // Memory cannot connect to gateway
    expect(areComponentsCompatible('memory', 'gateway')).toBe(false);

    // Memory cannot connect to identity
    expect(areComponentsCompatible('memory', 'identity')).toBe(false);

    // Code interpreter cannot connect to gateway
    expect(areComponentsCompatible('code_interpreter', 'gateway')).toBe(false);

    // Browser cannot connect to browser
    expect(areComponentsCompatible('browser', 'browser')).toBe(false);

    // Identity cannot connect to identity
    expect(areComponentsCompatible('identity', 'identity')).toBe(false);
  });

  it('getCompatibleTargets returns correct targets for each type', () => {
    fc.assert(
      fc.property(componentTypeArb, (sourceType) => {
        const targets = getCompatibleTargets(sourceType);

        // Should match the compatibility matrix
        expect(targets).toEqual(CONNECTION_COMPATIBILITY[sourceType]);
      }),
      { numRuns: 100 }
    );
  });

  it('Property 9: createEdgeIfCompatible creates edge for compatible nodes', () => {
    fc.assert(
      fc.property(nodeArb, nodeArb, (sourceNode, targetNode) => {
        // Ensure unique IDs
        const source = { ...sourceNode, id: 'source-' + sourceNode.id };
        const target = { ...targetNode, id: 'target-' + targetNode.id };

        const sourceType = source.data.componentType;
        const targetType = target.data.componentType;

        const edge = createEdgeIfCompatible(source, target);

        if (areComponentsCompatible(sourceType, targetType)) {
          // Should create an edge
          expect(edge).not.toBeNull();
          expect(edge?.source).toBe(source.id);
          expect(edge?.target).toBe(target.id);
          expect(edge?.type).toBe('connection');
        }
      }),
      { numRuns: 100 }
    );
  });

  it('Property 10: createEdgeIfCompatible returns null for incompatible nodes', () => {
    fc.assert(
      fc.property(nodeArb, nodeArb, (sourceNode, targetNode) => {
        // Ensure unique IDs
        const source = { ...sourceNode, id: 'source-' + sourceNode.id };
        const target = { ...targetNode, id: 'target-' + targetNode.id };

        const sourceType = source.data.componentType;
        const targetType = target.data.componentType;

        const edge = createEdgeIfCompatible(source, target);

        if (!areComponentsCompatible(sourceType, targetType)) {
          // Should not create an edge
          expect(edge).toBeNull();
        }
      }),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Edge Deletion Tests
// ============================================================================

describe('Edge Deletion', () => {
  it('deleteEdge removes the specified edge', () => {
    fc.assert(
      fc.property(edgesArrayArb(1, 10), (edges) => {
        const edgeToDelete = edges[0];
        const result = deleteEdge(edges, edgeToDelete.id);

        // Edge should be removed
        expect(edgeExists(result, edgeToDelete.id)).toBe(false);

        // Other edges should remain
        expect(result.length).toBe(edges.length - 1);
      }),
      { numRuns: 100 }
    );
  });

  it('deleteEdge with non-existent ID returns unchanged array', () => {
    fc.assert(
      fc.property(edgesArrayArb(1, 10), (edges) => {
        const nonExistentId = 'non-existent-' + Date.now();
        const result = deleteEdge(edges, nonExistentId);

        // Array should be unchanged
        expect(result.length).toBe(edges.length);
      }),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Edge Finding Tests
// ============================================================================

describe('Edge Finding', () => {
  it('findEdgeByNodes finds existing edge', () => {
    fc.assert(
      fc.property(edgesArrayArb(1, 10), (edges) => {
        const edge = edges[0];
        const found = findEdgeByNodes(edges, edge.source, edge.target);

        expect(found).not.toBeNull();
        expect(found?.id).toBe(edge.id);
      }),
      { numRuns: 100 }
    );
  });

  it('findEdgeByNodes returns null for non-existent connection', () => {
    fc.assert(
      fc.property(edgesArrayArb(0, 10), (edges) => {
        const found = findEdgeByNodes(edges, 'non-existent-source', 'non-existent-target');

        expect(found).toBeNull();
      }),
      { numRuns: 100 }
    );
  });
});
