/**
 * Property-based tests for deployment blocking.
 * Property 26: Deployment Blocked on Validation Errors
 * Validates: Requirements 8.6
 */

import { describe, it, expect, beforeEach } from 'vitest';
import * as fc from 'fast-check';
import { useWorkflowStore } from '../../store/workflowStore';
import type { AgentCoreNode } from '../../store/workflowStore';
import type { RuntimeConfiguration } from '../../types/components';

// ============================================================================
// Test Helpers
// ============================================================================

/**
 * Create a valid runtime configuration for testing.
 */
function createValidRuntimeConfig(): RuntimeConfiguration {
  return {
    name: 'Test Runtime',
    entrypoint: 'agent.py',
    framework: 'strands_agents',
    model: {
      provider: 'anthropic',
      modelId: 'us.anthropic.claude-sonnet-5',
      temperature: 0.7,
      topP: 0.9,
    },
    systemPrompt: 'You are a helpful assistant.',
    deploymentType: 'direct_code_deploy',
    pythonRuntime: 'PYTHON_3_11',
    protocol: 'HTTP',
    idleTimeout: 300,
    maxLifetime: 3600,
    enableOtel: false,
    modelProvider: 'bedrock',
    multiAgentPattern: 'none',
  };
}

/**
 * Create a node with valid configuration.
 */
function createValidNode(id: string): AgentCoreNode {
  return {
    id,
    type: 'agentCoreNode',
    position: { x: 100, y: 100 },
    data: {
      label: 'Runtime',
      componentType: 'runtime',
      configuration: createValidRuntimeConfig(),
      validationStatus: 'valid',
      validationErrors: [],
      validationWarnings: [],
    },
  };
}

/**
 * Create a node with missing configuration (will cause validation error).
 */
function createInvalidNode(id: string): AgentCoreNode {
  return {
    id,
    type: 'agentCoreNode',
    position: { x: 100, y: 100 },
    data: {
      label: 'Runtime',
      componentType: 'runtime',
      configuration: undefined,
      validationStatus: 'error',
      validationErrors: [
        {
          componentId: id,
          field: 'configuration',
          message: 'Configuration is required',
          severity: 'error',
        },
      ],
      validationWarnings: [],
    },
  };
}

/**
 * Reset store to initial state before each test.
 */
function resetStore() {
  useWorkflowStore.setState({
    nodes: [],
    edges: [],
    viewport: { x: 0, y: 0, zoom: 1 },
    selectedNodeId: null,
    selectedEdgeId: null,
    validationState: null,
    isReadyToDeploy: false,
  });
}

// ============================================================================
// Property 26: Deployment Blocked on Validation Errors
// ============================================================================

describe('Property 26: Deployment Blocked on Validation Errors', () => {
  beforeEach(() => {
    resetStore();
  });

  /**
   * **Validates: Requirements 8.6**
   *
   * For any workflow with validation errors, the deploy action shall be blocked
   * and an error summary shall be displayed.
   */
  describe('Deploy button state based on validation', () => {
    it('isReadyToDeploy is false when workflow has no nodes', () => {
      const store = useWorkflowStore.getState();
      store.runValidation();

      const state = useWorkflowStore.getState();
      expect(state.isReadyToDeploy).toBe(false);
    });

    it('isReadyToDeploy is true when all nodes have valid configurations', () => {
      const store = useWorkflowStore.getState();

      // Add a valid node
      store.addNode(createValidNode('node-1'));
      store.runValidation();

      const state = useWorkflowStore.getState();
      expect(state.isReadyToDeploy).toBe(true);
      expect(state.validationState?.errors.length).toBe(0);
    });

    it('isReadyToDeploy is false when any node has validation errors', () => {
      const store = useWorkflowStore.getState();

      // Add an invalid node (missing configuration)
      store.addNode(createInvalidNode('node-1'));
      store.runValidation();

      const state = useWorkflowStore.getState();
      expect(state.isReadyToDeploy).toBe(false);
      expect(state.validationState?.errors.length).toBeGreaterThan(0);
    });

    it('isReadyToDeploy is false when edges have validation errors', () => {
      const store = useWorkflowStore.getState();

      // Add two memory nodes (memory-to-memory is incompatible)
      const memoryNode1: AgentCoreNode = {
        id: 'memory-1',
        type: 'agentCoreNode',
        position: { x: 100, y: 100 },
        data: {
          label: 'Memory 1',
          componentType: 'memory',
          configuration: { name: 'Memory 1', enabled: true },
          validationStatus: 'valid',
          validationErrors: [],
          validationWarnings: [],
        },
      };

      const memoryNode2: AgentCoreNode = {
        id: 'memory-2',
        type: 'agentCoreNode',
        position: { x: 300, y: 100 },
        data: {
          label: 'Memory 2',
          componentType: 'memory',
          configuration: { name: 'Memory 2', enabled: true },
          validationStatus: 'valid',
          validationErrors: [],
          validationWarnings: [],
        },
      };

      store.addNode(memoryNode1);
      store.addNode(memoryNode2);

      // Add incompatible edge
      store.addEdge({
        id: 'edge-1',
        source: 'memory-1',
        target: 'memory-2',
      });

      store.runValidation();

      const state = useWorkflowStore.getState();
      expect(state.isReadyToDeploy).toBe(false);
      expect(state.validationState?.errors.length).toBeGreaterThan(0);
    });
  });

  describe('Property-based tests for deployment blocking', () => {
    /**
     * Property: For any number of valid nodes, deployment should be allowed.
     */
    it('deployment is allowed for any number of valid nodes', () => {
      fc.assert(
        fc.property(
          fc.integer({ min: 1, max: 10 }),
          (nodeCount) => {
            resetStore();
            const store = useWorkflowStore.getState();

            // Add valid nodes
            for (let i = 0; i < nodeCount; i++) {
              store.addNode(createValidNode(`node-${i}`));
            }

            store.runValidation();

            const state = useWorkflowStore.getState();
            expect(state.isReadyToDeploy).toBe(true);
            expect(state.validationState?.errors.length).toBe(0);

            return true;
          }
        ),
        { numRuns: 20 }
      );
    });

    /**
     * Property: For any workflow with at least one invalid node, deployment should be blocked.
     */
    it('deployment is blocked when any node is invalid', () => {
      fc.assert(
        fc.property(
          fc.integer({ min: 0, max: 5 }),
          fc.integer({ min: 1, max: 5 }),
          (validCount, invalidCount) => {
            resetStore();
            const store = useWorkflowStore.getState();

            // Add valid nodes
            for (let i = 0; i < validCount; i++) {
              store.addNode(createValidNode(`valid-${i}`));
            }

            // Add invalid nodes
            for (let i = 0; i < invalidCount; i++) {
              store.addNode(createInvalidNode(`invalid-${i}`));
            }

            store.runValidation();

            const state = useWorkflowStore.getState();
            expect(state.isReadyToDeploy).toBe(false);
            expect(state.validationState?.errors.length).toBeGreaterThan(0);

            return true;
          }
        ),
        { numRuns: 30 }
      );
    });

    /**
     * Property: Error count in validation state matches actual errors.
     */
    it('error count accurately reflects validation issues', () => {
      fc.assert(
        fc.property(
          fc.integer({ min: 1, max: 5 }),
          (invalidCount) => {
            resetStore();
            const store = useWorkflowStore.getState();

            // Add invalid nodes
            for (let i = 0; i < invalidCount; i++) {
              store.addNode(createInvalidNode(`invalid-${i}`));
            }

            store.runValidation();

            const state = useWorkflowStore.getState();

            // Each invalid node should contribute at least one error
            expect(state.validationState?.errors.length).toBeGreaterThanOrEqual(invalidCount);

            return true;
          }
        ),
        { numRuns: 20 }
      );
    });
  });

  describe('Error summary modal data', () => {
    it('validation state contains all errors grouped by component', () => {
      const store = useWorkflowStore.getState();

      // Add multiple invalid nodes
      store.addNode(createInvalidNode('node-1'));
      store.addNode(createInvalidNode('node-2'));
      store.runValidation();

      const state = useWorkflowStore.getState();

      // Should have errors for both nodes
      expect(state.validationState?.errors.length).toBeGreaterThanOrEqual(2);

      // Errors should have component IDs
      const componentIds = new Set(
        state.validationState?.errors.map((e) => e.componentId)
      );
      expect(componentIds.has('node-1')).toBe(true);
      expect(componentIds.has('node-2')).toBe(true);
    });

    it('validation state separates errors from warnings', () => {
      const store = useWorkflowStore.getState();

      // Add a valid node (may have warnings but no errors)
      store.addNode(createValidNode('node-1'));
      store.runValidation();

      const state = useWorkflowStore.getState();

      // Should have separate arrays for errors and warnings
      expect(Array.isArray(state.validationState?.errors)).toBe(true);
      expect(Array.isArray(state.validationState?.warnings)).toBe(true);

      // Valid node should have no errors
      expect(state.validationState?.errors.length).toBe(0);
    });
  });

  describe('Deploy button behavior simulation', () => {
    /**
     * Simulates the deploy button logic to verify it correctly blocks deployment.
     */
    it('deploy button would be disabled when errors exist', () => {
      const store = useWorkflowStore.getState();

      store.addNode(createInvalidNode('node-1'));
      store.runValidation();

      const state = useWorkflowStore.getState();

      // Simulate deploy button logic
      const hasNodes = state.nodes.length > 0;
      const hasErrors = state.validationState
        ? state.validationState.errors.length > 0
        : false;
      const isDisabled = !hasNodes || hasErrors;

      expect(isDisabled).toBe(true);
      expect(hasErrors).toBe(true);
    });

    it('deploy button would be enabled when no errors exist', () => {
      const store = useWorkflowStore.getState();

      store.addNode(createValidNode('node-1'));
      store.runValidation();

      const state = useWorkflowStore.getState();

      // Simulate deploy button logic
      const hasNodes = state.nodes.length > 0;
      const hasErrors = state.validationState
        ? state.validationState.errors.length > 0
        : false;
      const isDisabled = !hasNodes || hasErrors;

      expect(isDisabled).toBe(false);
      expect(hasErrors).toBe(false);
    });

    it('deploy button would be disabled when no nodes exist', () => {
      const store = useWorkflowStore.getState();
      store.runValidation();

      const state = useWorkflowStore.getState();

      // Simulate deploy button logic
      const hasNodes = state.nodes.length > 0;
      const isDisabled = !hasNodes;

      expect(isDisabled).toBe(true);
      expect(hasNodes).toBe(false);
    });
  });
});
