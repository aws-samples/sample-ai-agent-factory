/**
 * Property-based tests for workflow serialization.
 * **Property 27: Workflow Serialization Round-Trip**
 * **Validates: Requirements 9.5, 9.6, 14.1, 14.2, 14.5**
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  WorkflowSerializer,
  areWorkflowsEquivalent,
  type SerializedWorkflow,
  type SerializedMetadata,
} from './serialization';
import type { AgentCoreNode } from '../store/workflowStore';
import type { AgentCoreComponentType } from '../types/workflow';
import type { Edge, Viewport } from '@xyflow/react';

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

const componentTypeArb = fc.constantFrom(
  'runtime',
  'gateway',
  'memory',
  'code_interpreter',
  'browser',
  'observability',
  'identity',
  'evaluation',
  'policy',
  'a2a'
) as fc.Arbitrary<AgentCoreComponentType>;

const validationStatusArb = fc.constantFrom('valid', 'warning', 'error', 'pending') as fc.Arbitrary<
  'valid' | 'warning' | 'error' | 'pending'
>;

const deploymentStatusArb = fc.constantFrom(
  'not_deployed',
  'deploying',
  'deployed',
  'failed'
) as fc.Arbitrary<'not_deployed' | 'deploying' | 'deployed' | 'failed'>;

const positionArb = fc.record({
  x: fc.float({ min: Math.fround(0), max: Math.fround(2000), noNaN: true }),
  y: fc.float({ min: Math.fround(0), max: Math.fround(2000), noNaN: true }),
});

const viewportArb: fc.Arbitrary<Viewport> = fc.record({
  x: fc.float({ min: Math.fround(-1000), max: Math.fround(1000), noNaN: true }),
  y: fc.float({ min: Math.fround(-1000), max: Math.fround(1000), noNaN: true }),
  zoom: fc.float({ min: Math.fround(0.1), max: Math.fround(4), noNaN: true }),
});

// Generate node data that ensures type and componentType match
const agentCoreNodeArb: fc.Arbitrary<AgentCoreNode> = componentTypeArb.chain((componentType) =>
  fc.record({
    id: fc.uuid(),
    type: fc.constant(componentType),
    position: positionArb,
    data: fc.record({
      label: fc.string({ minLength: 1, maxLength: 50 }),
      componentType: fc.constant(componentType),
      validationStatus: validationStatusArb,
    }),
    selected: fc.boolean(),
  })
);

const edgeArb: fc.Arbitrary<Edge> = fc.record({
  id: fc.uuid(),
  source: fc.uuid(),
  target: fc.uuid(),
  sourceHandle: fc.option(fc.string({ minLength: 1, maxLength: 20 }), { nil: null }),
  targetHandle: fc.option(fc.string({ minLength: 1, maxLength: 20 }), { nil: null }),
  type: fc.option(fc.constantFrom('data', 'tool', 'identity'), { nil: undefined }),
  animated: fc.boolean(),
  data: fc.option(fc.record({ label: fc.string() }), { nil: undefined }),
  selected: fc.boolean(),
});

const metadataArb: fc.Arbitrary<Partial<SerializedMetadata>> = fc.record({
  author: fc.string({ minLength: 0, maxLength: 50 }),
  tags: fc.array(fc.string({ minLength: 1, maxLength: 20 }), { minLength: 0, maxLength: 5 }),
  awsRegion: fc.constantFrom('us-east-1', 'us-west-2', 'eu-west-1', 'ap-southeast-1'),
  deploymentStatus: deploymentStatusArb,
});

const workflowInfoArb = fc.record({
  id: fc.uuid(),
  name: fc.string({ minLength: 1, maxLength: 100 }),
  description: fc.string({ minLength: 0, maxLength: 500 }),
  version: fc.tuple(
    fc.integer({ min: 0, max: 10 }),
    fc.integer({ min: 0, max: 10 }),
    fc.integer({ min: 0, max: 10 })
  ).map(([major, minor, patch]) => `${major}.${minor}.${patch}`),
});

// ============================================================================
// Property 27: Workflow Serialization Round-Trip
// ============================================================================

describe('Property 27: Workflow Serialization Round-Trip', () => {
  /**
   * **Validates: Requirements 9.5, 9.6, 14.1, 14.2, 14.5**
   *
   * For any valid workflow definition, serializing to JSON and then deserializing
   * shall produce an equivalent workflow with identical nodes (positions and configurations),
   * edges, and viewport state.
   */
  it('serialization round-trip preserves workflow data', () => {
    fc.assert(
      fc.property(
        fc.array(agentCoreNodeArb, { minLength: 0, maxLength: 10 }),
        fc.array(edgeArb, { minLength: 0, maxLength: 10 }),
        viewportArb,
        metadataArb,
        workflowInfoArb,
        (nodes, edges, viewport, metadata, workflowInfo) => {
          // Serialize to JSON
          const json = WorkflowSerializer.serialize(nodes, edges, viewport, metadata, workflowInfo);

          // Deserialize back
          const deserialized = WorkflowSerializer.deserialize(json);

          // Verify node count matches
          expect(deserialized.nodes.length).toBe(nodes.length);

          // Verify each node's essential properties
          for (let i = 0; i < nodes.length; i++) {
            const original = nodes[i];
            const restored = deserialized.nodes[i];

            expect(restored.id).toBe(original.id);
            expect(restored.data.componentType).toBe(original.data.componentType);
            expect(restored.position.x).toBeCloseTo(original.position.x, 3);
            expect(restored.position.y).toBeCloseTo(original.position.y, 3);
          }

          // Verify edge count matches
          expect(deserialized.edges.length).toBe(edges.length);

          // Verify each edge's essential properties
          for (let i = 0; i < edges.length; i++) {
            const original = edges[i];
            const restored = deserialized.edges[i];

            expect(restored.id).toBe(original.id);
            expect(restored.source).toBe(original.source);
            expect(restored.target).toBe(original.target);
          }

          // Verify viewport
          expect(deserialized.viewport.x).toBeCloseTo(viewport.x, 3);
          expect(deserialized.viewport.y).toBeCloseTo(viewport.y, 3);
          expect(deserialized.viewport.zoom).toBeCloseTo(viewport.zoom, 3);

          // Verify workflow info
          expect(deserialized.workflowInfo.id).toBe(workflowInfo.id);
          expect(deserialized.workflowInfo.name).toBe(workflowInfo.name);
          expect(deserialized.workflowInfo.version).toBe(workflowInfo.version);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('double serialization produces equivalent results', () => {
    fc.assert(
      fc.property(
        fc.array(agentCoreNodeArb, { minLength: 0, maxLength: 5 }),
        fc.array(edgeArb, { minLength: 0, maxLength: 5 }),
        viewportArb,
        metadataArb,
        workflowInfoArb,
        (nodes, edges, viewport, metadata, workflowInfo) => {
          // First round-trip
          const json1 = WorkflowSerializer.serialize(nodes, edges, viewport, metadata, workflowInfo);
          const deserialized1 = WorkflowSerializer.deserialize(json1);

          // Second round-trip
          const json2 = WorkflowSerializer.serialize(
            deserialized1.nodes,
            deserialized1.edges,
            deserialized1.viewport,
            deserialized1.metadata,
            deserialized1.workflowInfo
          );
          const deserialized2 = WorkflowSerializer.deserialize(json2);

          // Both deserializations should produce equivalent results
          expect(deserialized2.nodes.length).toBe(deserialized1.nodes.length);
          expect(deserialized2.edges.length).toBe(deserialized1.edges.length);
          expect(deserialized2.viewport.x).toBeCloseTo(deserialized1.viewport.x, 3);
          expect(deserialized2.viewport.y).toBeCloseTo(deserialized1.viewport.y, 3);
          expect(deserialized2.viewport.zoom).toBeCloseTo(deserialized1.viewport.zoom, 3);
        }
      ),
      { numRuns: 50 }
    );
  });

  it('serialized JSON is valid and parseable', () => {
    fc.assert(
      fc.property(
        fc.array(agentCoreNodeArb, { minLength: 0, maxLength: 5 }),
        fc.array(edgeArb, { minLength: 0, maxLength: 5 }),
        viewportArb,
        (nodes, edges, viewport) => {
          const json = WorkflowSerializer.serialize(nodes, edges, viewport);

          // Should be valid JSON
          expect(() => JSON.parse(json)).not.toThrow();

          // Should pass schema validation
          const errors = WorkflowSerializer.validateSchema(json);
          expect(errors).toHaveLength(0);
        }
      ),
      { numRuns: 50 }
    );
  });
});

// ============================================================================
// Schema Validation Tests
// ============================================================================

describe('Schema Validation', () => {
  it('rejects invalid JSON', () => {
    const errors = WorkflowSerializer.validateSchema('not valid json');
    expect(errors.length).toBeGreaterThan(0);
    expect(errors[0].message).toContain('Invalid JSON');
  });

  it('rejects non-object JSON', () => {
    const errors = WorkflowSerializer.validateSchema('"just a string"');
    expect(errors.length).toBeGreaterThan(0);
    expect(errors[0].message).toContain('must be an object');
  });

  it('rejects missing required fields', () => {
    const errors = WorkflowSerializer.validateSchema('{}');
    expect(errors.length).toBeGreaterThan(0);
    expect(errors.some((e) => e.field === 'id')).toBe(true);
    expect(errors.some((e) => e.field === 'nodes')).toBe(true);
    expect(errors.some((e) => e.field === 'edges')).toBe(true);
    expect(errors.some((e) => e.field === 'viewport')).toBe(true);
    expect(errors.some((e) => e.field === 'metadata')).toBe(true);
  });

  it('rejects invalid version format', () => {
    const workflow = {
      id: 'test-id',
      name: 'Test',
      version: 'invalid',
      nodes: [],
      edges: [],
      viewport: { x: 0, y: 0, zoom: 1 },
      metadata: {
        author: '',
        tags: [],
        awsRegion: 'us-east-1',
        deploymentStatus: 'not_deployed',
      },
    };
    const errors = WorkflowSerializer.validateSchema(JSON.stringify(workflow));
    expect(errors.some((e) => e.field === 'version' && e.message.includes('semver'))).toBe(true);
  });

  it('rejects invalid node type', () => {
    const workflow = {
      id: 'test-id',
      name: 'Test',
      version: '1.0.0',
      nodes: [{ id: 'node-1', type: 'invalid-type', position: { x: 0, y: 0 } }],
      edges: [],
      viewport: { x: 0, y: 0, zoom: 1 },
      metadata: {
        author: '',
        tags: [],
        awsRegion: 'us-east-1',
        deploymentStatus: 'not_deployed',
      },
    };
    const errors = WorkflowSerializer.validateSchema(JSON.stringify(workflow));
    expect(errors.some((e) => e.field.includes('type') && e.message.includes('must be one of'))).toBe(true);
  });

  it('rejects viewport zoom out of range', () => {
    const workflow = {
      id: 'test-id',
      name: 'Test',
      version: '1.0.0',
      nodes: [],
      edges: [],
      viewport: { x: 0, y: 0, zoom: 10 },
      metadata: {
        author: '',
        tags: [],
        awsRegion: 'us-east-1',
        deploymentStatus: 'not_deployed',
      },
    };
    const errors = WorkflowSerializer.validateSchema(JSON.stringify(workflow));
    expect(errors.some((e) => e.field === 'viewport.zoom')).toBe(true);
  });
});

// ============================================================================
// areWorkflowsEquivalent Tests
// ============================================================================

describe('areWorkflowsEquivalent', () => {
  it('returns true for identical workflows', () => {
    fc.assert(
      fc.property(
        fc.array(agentCoreNodeArb, { minLength: 0, maxLength: 5 }),
        fc.array(edgeArb, { minLength: 0, maxLength: 5 }),
        viewportArb,
        metadataArb,
        workflowInfoArb,
        (nodes, edges, viewport, metadata, workflowInfo) => {
          const workflow = WorkflowSerializer.toSerializedWorkflow(
            nodes,
            edges,
            viewport,
            metadata,
            workflowInfo
          );
          expect(areWorkflowsEquivalent(workflow, workflow)).toBe(true);
        }
      ),
      { numRuns: 50 }
    );
  });

  it('returns false for different node counts', () => {
    const workflow1: SerializedWorkflow = {
      id: 'test',
      name: 'Test',
      description: '',
      version: '1.0.0',
      nodes: [{ id: 'n1', type: 'runtime', position: { x: 0, y: 0 }, data: {}, selected: false, validationStatus: 'valid' }],
      edges: [],
      viewport: { x: 0, y: 0, zoom: 1 },
      metadata: { author: '', tags: [], awsRegion: 'us-east-1', deploymentStatus: 'not_deployed' },
      createdAt: '',
      updatedAt: '',
    };
    const workflow2: SerializedWorkflow = {
      ...workflow1,
      nodes: [],
    };
    expect(areWorkflowsEquivalent(workflow1, workflow2)).toBe(false);
  });
});
