/**
 * Property-based tests for undo/redo operations.
 * Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  createUndoRedoManager,
  createAction,
  cloneWorkflowState,
  areStatesEqual,
  MAX_UNDO_STACK_SIZE,
  type WorkflowState,
  type ActionType,
} from './undoRedo';
import type { AgentCoreComponentType } from '../types/workflow';

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

const positionArb = fc.record({
  x: fc.float({ min: Math.fround(0), max: Math.fround(2000), noNaN: true }),
  y: fc.float({ min: Math.fround(0), max: Math.fround(2000), noNaN: true }),
});

const viewportArb = fc.record({
  x: fc.float({ min: Math.fround(-1000), max: Math.fround(1000), noNaN: true }),
  y: fc.float({ min: Math.fround(-1000), max: Math.fround(1000), noNaN: true }),
  zoom: fc.float({ min: Math.fround(0.1), max: Math.fround(4), noNaN: true }),
});

const componentTypeArb = fc.constantFrom(
  'runtime',
  'gateway',
  'memory',
  'code_interpreter',
  'browser',
  'observability',
  'identity'
) as fc.Arbitrary<AgentCoreComponentType>;

const validationStatusArb = fc.constantFrom('valid', 'warning', 'error', 'pending') as fc.Arbitrary<
  'valid' | 'warning' | 'error' | 'pending'
>;

const nodeDataArb = fc.record({
  label: fc.string({ minLength: 1, maxLength: 50 }),
  componentType: componentTypeArb,
  validationStatus: validationStatusArb,
});

const nodeArb = fc.record({
  id: fc.uuid(),
  type: componentTypeArb,
  position: positionArb,
  data: nodeDataArb,
  selected: fc.boolean(),
});

const edgeArb = fc.record({
  id: fc.uuid(),
  source: fc.uuid(),
  target: fc.uuid(),
  type: fc.constantFrom('connection', 'data', 'tool', 'identity'),
});

const workflowStateArb: fc.Arbitrary<WorkflowState> = fc.record({
  nodes: fc.array(nodeArb, { minLength: 0, maxLength: 10 }),
  edges: fc.array(edgeArb, { minLength: 0, maxLength: 10 }),
  viewport: viewportArb,
});

const actionTypeArb = fc.constantFrom(
  'ADD_NODE',
  'REMOVE_NODE',
  'MOVE_NODE',
  'UPDATE_CONFIG',
  'ADD_EDGE',
  'REMOVE_EDGE',
  'BATCH'
) as fc.Arbitrary<ActionType>;

// ============================================================================
// Property 30: Undo Restores Previous State
// ============================================================================

describe('Property 30: Undo Restores Previous State', () => {
  /**
   * **Validates: Requirements 10.1, 10.4**
   *
   * For any action in the undo stack, performing undo shall restore the workflow
   * to the exact state before that action, including all node positions and configurations.
   */
  it('undo restores the exact previous state', () => {
    fc.assert(
      fc.property(
        workflowStateArb,
        workflowStateArb,
        actionTypeArb,
        (previousState, newState, actionType) => {
          const manager = createUndoRedoManager();

          // Create and push an action
          const action = createAction(actionType, previousState, newState);
          manager.push(action);

          // Perform undo
          const restoredState = manager.undo();

          // The restored state should equal the previous state
          expect(restoredState).not.toBeNull();
          if (restoredState) {
            expect(areStatesEqual(restoredState, previousState)).toBe(true);
          }
        }
      ),
      { numRuns: 100 }
    );
  });

  it('undo returns null when stack is empty', () => {
    fc.assert(
      fc.property(fc.constant(null), () => {
        const manager = createUndoRedoManager();

        // Undo on empty stack should return null
        const result = manager.undo();
        expect(result).toBeNull();
      }),
      { numRuns: 10 }
    );
  });

  it('multiple undos restore states in reverse order', () => {
    fc.assert(
      fc.property(
        fc.array(workflowStateArb, { minLength: 2, maxLength: 5 }),
        actionTypeArb,
        (states, actionType) => {
          const manager = createUndoRedoManager();

          // Push multiple actions
          for (let i = 0; i < states.length - 1; i++) {
            const action = createAction(actionType, states[i], states[i + 1]);
            manager.push(action);
          }

          // Undo all actions and verify states are restored in reverse order
          for (let i = states.length - 2; i >= 0; i--) {
            const restoredState = manager.undo();
            expect(restoredState).not.toBeNull();
            if (restoredState) {
              expect(areStatesEqual(restoredState, states[i])).toBe(true);
            }
          }
        }
      ),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Property 31: Redo Restores Undone Action
// ============================================================================

describe('Property 31: Redo Restores Undone Action', () => {
  /**
   * **Validates: Requirements 10.2**
   *
   * For any undone action, performing redo shall restore the workflow
   * to the state after that action was originally performed.
   */
  it('redo restores the new state after undo', () => {
    fc.assert(
      fc.property(
        workflowStateArb,
        workflowStateArb,
        actionTypeArb,
        (previousState, newState, actionType) => {
          const manager = createUndoRedoManager();

          // Create and push an action
          const action = createAction(actionType, previousState, newState);
          manager.push(action);

          // Perform undo
          manager.undo();

          // Perform redo
          const restoredState = manager.redo();

          // The restored state should equal the new state
          expect(restoredState).not.toBeNull();
          if (restoredState) {
            expect(areStatesEqual(restoredState, newState)).toBe(true);
          }
        }
      ),
      { numRuns: 100 }
    );
  });

  it('redo returns null when redo stack is empty', () => {
    fc.assert(
      fc.property(workflowStateArb, workflowStateArb, actionTypeArb, (previousState, newState, actionType) => {
        const manager = createUndoRedoManager();

        // Push an action but don't undo
        const action = createAction(actionType, previousState, newState);
        manager.push(action);

        // Redo on empty redo stack should return null
        const result = manager.redo();
        expect(result).toBeNull();
      }),
      { numRuns: 100 }
    );
  });

  it('multiple redos restore states in original order', () => {
    fc.assert(
      fc.property(
        fc.array(workflowStateArb, { minLength: 2, maxLength: 5 }),
        actionTypeArb,
        (states, actionType) => {
          const manager = createUndoRedoManager();

          // Push multiple actions
          for (let i = 0; i < states.length - 1; i++) {
            const action = createAction(actionType, states[i], states[i + 1]);
            manager.push(action);
          }

          // Undo all actions
          for (let i = 0; i < states.length - 1; i++) {
            manager.undo();
          }

          // Redo all actions and verify states are restored in original order
          for (let i = 1; i < states.length; i++) {
            const restoredState = manager.redo();
            expect(restoredState).not.toBeNull();
            if (restoredState) {
              expect(areStatesEqual(restoredState, states[i])).toBe(true);
            }
          }
        }
      ),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Property 32: Undo Stack Capacity
// ============================================================================

describe('Property 32: Undo Stack Capacity', () => {
  /**
   * **Validates: Requirements 10.3**
   *
   * For any sequence of N actions where N > 50, the undo stack shall maintain
   * at least the 50 most recent actions.
   */
  it('maintains at least 50 actions in history', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: MAX_UNDO_STACK_SIZE + 1, max: MAX_UNDO_STACK_SIZE + 20 }),
        workflowStateArb,
        actionTypeArb,
        (numActions, baseState, actionType) => {
          const manager = createUndoRedoManager();

          // Push more than MAX_UNDO_STACK_SIZE actions
          for (let i = 0; i < numActions; i++) {
            const prevState = { ...baseState, viewport: { ...baseState.viewport, x: i } };
            const newState = { ...baseState, viewport: { ...baseState.viewport, x: i + 1 } };
            const action = createAction(actionType, prevState, newState);
            manager.push(action);
          }

          // Stack should have exactly MAX_UNDO_STACK_SIZE actions
          expect(manager.getUndoStackSize()).toBe(MAX_UNDO_STACK_SIZE);

          // Should be able to undo MAX_UNDO_STACK_SIZE times
          let undoCount = 0;
          while (manager.canUndo()) {
            manager.undo();
            undoCount++;
          }
          expect(undoCount).toBe(MAX_UNDO_STACK_SIZE);
        }
      ),
      { numRuns: 20 }
    );
  });

  it('preserves most recent actions when stack overflows', () => {
    fc.assert(
      fc.property(workflowStateArb, actionTypeArb, (baseState, actionType) => {
        const manager = createUndoRedoManager();

        // Push MAX_UNDO_STACK_SIZE + 5 actions
        const totalActions = MAX_UNDO_STACK_SIZE + 5;
        for (let i = 0; i < totalActions; i++) {
          const prevState = { ...baseState, viewport: { ...baseState.viewport, x: i } };
          const newState = { ...baseState, viewport: { ...baseState.viewport, x: i + 1 } };
          const action = createAction(actionType, prevState, newState);
          manager.push(action);
        }

        // The most recent action should be undoable and restore the correct state
        const lastPrevState = { ...baseState, viewport: { ...baseState.viewport, x: totalActions - 1 } };
        const restoredState = manager.undo();

        expect(restoredState).not.toBeNull();
        if (restoredState) {
          expect(restoredState.viewport.x).toBeCloseTo(lastPrevState.viewport.x, 5);
        }
      }),
      { numRuns: 20 }
    );
  });
});

// ============================================================================
// Property 33: New Action Clears Redo Stack
// ============================================================================

describe('Property 33: New Action Clears Redo Stack', () => {
  /**
   * **Validates: Requirements 10.5**
   *
   * For any new action performed after one or more undo operations,
   * the redo stack shall be cleared.
   */
  it('new action clears redo stack', () => {
    fc.assert(
      fc.property(
        workflowStateArb,
        workflowStateArb,
        workflowStateArb,
        actionTypeArb,
        (state1, state2, state3, actionType) => {
          const manager = createUndoRedoManager();

          // Push first action
          const action1 = createAction(actionType, state1, state2);
          manager.push(action1);

          // Undo to create redo stack
          manager.undo();
          expect(manager.canRedo()).toBe(true);

          // Push new action
          const action2 = createAction(actionType, state1, state3);
          manager.push(action2);

          // Redo stack should be cleared
          expect(manager.canRedo()).toBe(false);
          expect(manager.getRedoStackSize()).toBe(0);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('redo stack is cleared even after multiple undos', () => {
    fc.assert(
      fc.property(
        fc.array(workflowStateArb, { minLength: 3, maxLength: 5 }),
        workflowStateArb,
        actionTypeArb,
        (states, newState, actionType) => {
          const manager = createUndoRedoManager();

          // Push multiple actions
          for (let i = 0; i < states.length - 1; i++) {
            const action = createAction(actionType, states[i], states[i + 1]);
            manager.push(action);
          }

          // Undo multiple times
          const undoCount = Math.min(2, states.length - 1);
          for (let i = 0; i < undoCount; i++) {
            manager.undo();
          }
          expect(manager.getRedoStackSize()).toBe(undoCount);

          // Push new action
          const newAction = createAction(actionType, states[states.length - 1 - undoCount], newState);
          manager.push(newAction);

          // Redo stack should be completely cleared
          expect(manager.canRedo()).toBe(false);
          expect(manager.getRedoStackSize()).toBe(0);
        }
      ),
      { numRuns: 100 }
    );
  });
});

// ============================================================================
// Additional Helper Function Tests
// ============================================================================

describe('Helper Functions', () => {
  describe('cloneWorkflowState', () => {
    it('creates a deep copy of workflow state', () => {
      fc.assert(
        fc.property(workflowStateArb, (state) => {
          const cloned = cloneWorkflowState(state);

          // Should be equal
          expect(areStatesEqual(cloned, state)).toBe(true);

          // But not the same reference
          expect(cloned).not.toBe(state);
          expect(cloned.nodes).not.toBe(state.nodes);
          expect(cloned.edges).not.toBe(state.edges);
          expect(cloned.viewport).not.toBe(state.viewport);
        }),
        { numRuns: 100 }
      );
    });
  });

  describe('areStatesEqual', () => {
    it('returns true for identical states', () => {
      fc.assert(
        fc.property(workflowStateArb, (state) => {
          const cloned = cloneWorkflowState(state);
          expect(areStatesEqual(state, cloned)).toBe(true);
        }),
        { numRuns: 100 }
      );
    });

    it('returns false for different node counts', () => {
      fc.assert(
        fc.property(workflowStateArb, nodeArb, (state, extraNode) => {
          const modified = cloneWorkflowState(state);
          modified.nodes.push(extraNode as typeof modified.nodes[number]);
          expect(areStatesEqual(state, modified)).toBe(false);
        }),
        { numRuns: 100 }
      );
    });
  });

  describe('canUndo and canRedo', () => {
    it('canUndo returns correct state', () => {
      fc.assert(
        fc.property(workflowStateArb, workflowStateArb, actionTypeArb, (state1, state2, actionType) => {
          const manager = createUndoRedoManager();

          // Initially cannot undo
          expect(manager.canUndo()).toBe(false);

          // After push, can undo
          const action = createAction(actionType, state1, state2);
          manager.push(action);
          expect(manager.canUndo()).toBe(true);

          // After undo, cannot undo
          manager.undo();
          expect(manager.canUndo()).toBe(false);
        }),
        { numRuns: 100 }
      );
    });

    it('canRedo returns correct state', () => {
      fc.assert(
        fc.property(workflowStateArb, workflowStateArb, actionTypeArb, (state1, state2, actionType) => {
          const manager = createUndoRedoManager();

          // Initially cannot redo
          expect(manager.canRedo()).toBe(false);

          // After push, cannot redo
          const action = createAction(actionType, state1, state2);
          manager.push(action);
          expect(manager.canRedo()).toBe(false);

          // After undo, can redo
          manager.undo();
          expect(manager.canRedo()).toBe(true);

          // After redo, cannot redo
          manager.redo();
          expect(manager.canRedo()).toBe(false);
        }),
        { numRuns: 100 }
      );
    });
  });
});
