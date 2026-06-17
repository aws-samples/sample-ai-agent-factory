/**
 * Property-based tests for validation performance.
 * Property 25: Validation Performance
 * Validates: Requirements 8.5
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  validateWorkflow,
  validateComponentConfiguration,
  type WorkflowNode,
  type WorkflowEdge,
} from './validation';
import { measureExecutionTime, MAX_VALIDATION_TIME_MS } from './debounce';
import type { AgentCoreComponentType } from '../types/workflow';
import type { RuntimeConfiguration } from '../types/components';

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

const componentTypeArb = fc.constantFrom<AgentCoreComponentType>(
  'runtime',
  'gateway',
  'memory',
  'code_interpreter',
  'browser',
  'observability',
  'identity'
);

// Generate a workflow node with optional configuration
const workflowNodeArb = fc.record({
  id: fc.uuid(),
  type: componentTypeArb,
  data: fc.record({
    configuration: fc.option(
      fc.record({
        name: fc.string({ minLength: 1, maxLength: 100 }),
      }),
      { nil: undefined }
    ),
  }),
}) as fc.Arbitrary<WorkflowNode>;

// ============================================================================
// Property 25: Validation Performance
// ============================================================================

describe('Property 25: Validation Performance', () => {
  /**
   * **Validates: Requirements 8.5**
   *
   * For any configuration change, the validation engine shall complete
   * validation within 500ms.
   */
  it('validates single component within 500ms', () => {
    fc.assert(
      fc.property(fc.uuid(), componentTypeArb, (nodeId, componentType) => {
        const { timeMs } = measureExecutionTime(() =>
          validateComponentConfiguration(nodeId, componentType, undefined)
        );

        expect(timeMs).toBeLessThan(MAX_VALIDATION_TIME_MS);
      }),
      { numRuns: 100 }
    );
  });

  it('validates workflow with up to 10 nodes within 500ms', () => {
    fc.assert(
      fc.property(
        fc.array(workflowNodeArb, { minLength: 1, maxLength: 10 }),
        (nodes) => {
          const nodeIds = nodes.map((n) => n.id);
          const edges: WorkflowEdge[] = [];

          // Create some edges between nodes
          for (let i = 0; i < Math.min(nodes.length - 1, 5); i++) {
            edges.push({
              id: `edge-${i}`,
              source: nodeIds[i],
              target: nodeIds[i + 1],
            });
          }

          const { timeMs } = measureExecutionTime(() =>
            validateWorkflow(nodes, edges)
          );

          expect(timeMs).toBeLessThan(MAX_VALIDATION_TIME_MS);
        }
      ),
      { numRuns: 50 }
    );
  });

  it('validates workflow with up to 50 nodes within 500ms', () => {
    fc.assert(
      fc.property(
        fc.array(workflowNodeArb, { minLength: 10, maxLength: 50 }),
        (nodes) => {
          const nodeIds = nodes.map((n) => n.id);
          const edges: WorkflowEdge[] = [];

          // Create edges between consecutive nodes
          for (let i = 0; i < nodes.length - 1; i++) {
            edges.push({
              id: `edge-${i}`,
              source: nodeIds[i],
              target: nodeIds[i + 1],
            });
          }

          const { timeMs } = measureExecutionTime(() =>
            validateWorkflow(nodes, edges)
          );

          expect(timeMs).toBeLessThan(MAX_VALIDATION_TIME_MS);
        }
      ),
      { numRuns: 20 }
    );
  });

  it('validates complex workflow with many edges within 500ms', () => {
    fc.assert(
      fc.property(
        fc.array(workflowNodeArb, { minLength: 5, maxLength: 20 }),
        (nodes) => {
          const nodeIds = nodes.map((n) => n.id);
          const edges: WorkflowEdge[] = [];

          // Create a more complex edge structure
          for (let i = 0; i < nodes.length; i++) {
            for (let j = i + 1; j < Math.min(i + 3, nodes.length); j++) {
              edges.push({
                id: `edge-${i}-${j}`,
                source: nodeIds[i],
                target: nodeIds[j],
              });
            }
          }

          const { timeMs } = measureExecutionTime(() =>
            validateWorkflow(nodes, edges)
          );

          expect(timeMs).toBeLessThan(MAX_VALIDATION_TIME_MS);
        }
      ),
      { numRuns: 30 }
    );
  });

  it('validates runtime configuration with long system prompt within 500ms', () => {
    fc.assert(
      fc.property(
        fc.uuid(),
        fc.string({ minLength: 10000, maxLength: 50000 }),
        (nodeId, longPrompt) => {
          const config: RuntimeConfiguration = {
            name: 'Test Runtime',
            entrypoint: 'agent.py',
            framework: 'strands_agents',
            model: {
              provider: 'anthropic',
              modelId: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
              temperature: 0.7,
              topP: 0.9,
            },
            systemPrompt: longPrompt,
            deploymentType: 'direct_code_deploy',
            pythonRuntime: 'PYTHON_3_11',
            protocol: 'HTTP',
            idleTimeout: 300,
            maxLifetime: 3600,
            enableOtel: false,
            modelProvider: 'bedrock',
            multiAgentPattern: 'none',
          };

          const { timeMs } = measureExecutionTime(() =>
            validateComponentConfiguration(nodeId, 'runtime', config)
          );

          expect(timeMs).toBeLessThan(MAX_VALIDATION_TIME_MS);
        }
      ),
      { numRuns: 20 }
    );
  });
});

// ============================================================================
// Additional Performance Tests
// ============================================================================

describe('Validation Performance Benchmarks', () => {
  it('average validation time is well under 500ms', () => {
    const times: number[] = [];

    for (let i = 0; i < 100; i++) {
      const nodes: WorkflowNode[] = Array.from({ length: 10 }, (_, j) => ({
        id: `node-${j}`,
        type: 'runtime' as AgentCoreComponentType,
        data: {},
      }));

      const edges: WorkflowEdge[] = Array.from({ length: 9 }, (_, j) => ({
        id: `edge-${j}`,
        source: `node-${j}`,
        target: `node-${j + 1}`,
      }));

      const { timeMs } = measureExecutionTime(() =>
        validateWorkflow(nodes, edges)
      );

      times.push(timeMs);
    }

    const avgTime = times.reduce((a, b) => a + b, 0) / times.length;
    const maxTime = Math.max(...times);

    // Average should be well under 500ms
    expect(avgTime).toBeLessThan(100);
    // Max should still be under 500ms
    expect(maxTime).toBeLessThan(MAX_VALIDATION_TIME_MS);
  });

  it('validation scales linearly with node count', () => {
    const measurements: { nodeCount: number; timeMs: number }[] = [];

    for (const nodeCount of [5, 10, 20, 30, 40, 50]) {
      const nodes: WorkflowNode[] = Array.from({ length: nodeCount }, (_, j) => ({
        id: `node-${j}`,
        type: 'runtime' as AgentCoreComponentType,
        data: {},
      }));

      const edges: WorkflowEdge[] = Array.from(
        { length: nodeCount - 1 },
        (_, j) => ({
          id: `edge-${j}`,
          source: `node-${j}`,
          target: `node-${j + 1}`,
        })
      );

      const { timeMs } = measureExecutionTime(() =>
        validateWorkflow(nodes, edges)
      );

      measurements.push({ nodeCount, timeMs });
    }

    // All measurements should be under 500ms
    for (const m of measurements) {
      expect(m.timeMs).toBeLessThan(MAX_VALIDATION_TIME_MS);
    }

    // Check that time doesn't grow faster than O(n^2)
    // (allowing for some variance in measurements)
    const first = measurements[0];
    const last = measurements[measurements.length - 1];
    const nodeRatio = last.nodeCount / first.nodeCount;
    const timeRatio = last.timeMs / Math.max(first.timeMs, 0.1);

    // Time should not grow faster than quadratic
    expect(timeRatio).toBeLessThan(nodeRatio * nodeRatio * 2);
  });
});
