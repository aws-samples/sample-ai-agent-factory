/**
 * UndoRedoManager - Manages action history for undo/redo operations.
 * Implements action recording for all workflow changes with a stack capacity of at least 50 actions.
 * Requirements: 10.1, 10.2, 10.3, 10.4, 10.5
 */

import type { Edge, Viewport } from '@xyflow/react';
import type { AgentCoreNode } from '../store/workflowStore';

// ============================================================================
// Types
// ============================================================================

/**
 * Represents the complete state of a workflow at a point in time.
 */
export interface WorkflowState {
  nodes: AgentCoreNode[];
  edges: Edge[];
  viewport: Viewport;
}

/**
 * Types of actions that can be recorded in the undo/redo stack.
 */
export type ActionType =
  | 'ADD_NODE'
  | 'REMOVE_NODE'
  | 'MOVE_NODE'
  | 'UPDATE_CONFIG'
  | 'ADD_EDGE'
  | 'REMOVE_EDGE'
  | 'BATCH'; // For multiple changes in one action

/**
 * Represents a single action in the undo/redo history.
 */
export interface WorkflowAction {
  type: ActionType;
  previousState: WorkflowState;
  newState: WorkflowState;
  timestamp: number;
}

/**
 * Interface for the UndoRedoManager.
 */
export interface UndoRedoManager {
  push: (action: WorkflowAction) => void;
  undo: () => WorkflowState | null;
  redo: () => WorkflowState | null;
  canUndo: () => boolean;
  canRedo: () => boolean;
  clear: () => void;
  getUndoStackSize: () => number;
  getRedoStackSize: () => number;
}

// ============================================================================
// Constants
// ============================================================================

/**
 * Maximum number of actions to maintain in the undo stack.
 * Requirement 10.3: THE Undo_Redo_Stack SHALL maintain at least 50 actions in history
 */
export const MAX_UNDO_STACK_SIZE = 50;

// ============================================================================
// Helper Functions
// ============================================================================

/**
 * Deep clones a workflow state to ensure immutability.
 */
export function cloneWorkflowState(state: WorkflowState): WorkflowState {
  return {
    nodes: state.nodes.map((node) => ({
      ...node,
      position: { ...node.position },
      data: { ...node.data },
    })),
    edges: state.edges.map((edge) => ({
      ...edge,
      data: edge.data ? { ...edge.data } : undefined,
    })),
    viewport: { ...state.viewport },
  };
}

/**
 * Compares two workflow states for equality.
 */
export function areStatesEqual(state1: WorkflowState, state2: WorkflowState): boolean {
  // Compare nodes
  if (state1.nodes.length !== state2.nodes.length) return false;
  for (let i = 0; i < state1.nodes.length; i++) {
    const n1 = state1.nodes[i];
    const n2 = state2.nodes[i];
    if (n1.id !== n2.id) return false;
    if (n1.position.x !== n2.position.x || n1.position.y !== n2.position.y) return false;
    if (JSON.stringify(n1.data) !== JSON.stringify(n2.data)) return false;
  }

  // Compare edges
  if (state1.edges.length !== state2.edges.length) return false;
  for (let i = 0; i < state1.edges.length; i++) {
    const e1 = state1.edges[i];
    const e2 = state2.edges[i];
    if (e1.id !== e2.id || e1.source !== e2.source || e1.target !== e2.target) return false;
  }

  // Compare viewport
  if (
    state1.viewport.x !== state2.viewport.x ||
    state1.viewport.y !== state2.viewport.y ||
    state1.viewport.zoom !== state2.viewport.zoom
  ) {
    return false;
  }

  return true;
}

/**
 * Creates a workflow action from previous and new states.
 */
export function createAction(
  type: ActionType,
  previousState: WorkflowState,
  newState: WorkflowState
): WorkflowAction {
  return {
    type,
    previousState: cloneWorkflowState(previousState),
    newState: cloneWorkflowState(newState),
    timestamp: Date.now(),
  };
}

// ============================================================================
// UndoRedoManager Implementation
// ============================================================================

/**
 * Creates a new UndoRedoManager instance.
 * Manages undo/redo stacks with a maximum capacity of MAX_UNDO_STACK_SIZE.
 */
export function createUndoRedoManager(): UndoRedoManager {
  let undoStack: WorkflowAction[] = [];
  let redoStack: WorkflowAction[] = [];

  return {
    /**
     * Pushes a new action onto the undo stack.
     * Requirement 10.5: WHEN a new action is performed after undo, THE Undo_Redo_Stack SHALL clear the redo history
     * Requirement 10.3: THE Undo_Redo_Stack SHALL maintain at least 50 actions in history
     */
    push(action: WorkflowAction): void {
      // Clear redo stack when new action is performed
      redoStack = [];

      // Add action to undo stack
      undoStack.push(action);

      // Trim stack if it exceeds maximum size
      if (undoStack.length > MAX_UNDO_STACK_SIZE) {
        undoStack = undoStack.slice(-MAX_UNDO_STACK_SIZE);
      }
    },

    /**
     * Undoes the last action and returns the previous state.
     * Requirement 10.1: WHEN a user presses Ctrl+Z, THE Workflow_Canvas SHALL undo the last action
     * Requirement 10.4: WHEN an action is undone, THE Workflow_Canvas SHALL restore the previous state
     */
    undo(): WorkflowState | null {
      const action = undoStack.pop();
      if (!action) return null;

      // Move action to redo stack
      redoStack.push(action);

      // Return the previous state
      return cloneWorkflowState(action.previousState);
    },

    /**
     * Redoes the last undone action and returns the new state.
     * Requirement 10.2: WHEN a user presses Ctrl+Shift+Z, THE Workflow_Canvas SHALL redo the last undone action
     */
    redo(): WorkflowState | null {
      const action = redoStack.pop();
      if (!action) return null;

      // Move action back to undo stack
      undoStack.push(action);

      // Return the new state
      return cloneWorkflowState(action.newState);
    },

    /**
     * Returns whether undo is available.
     */
    canUndo(): boolean {
      return undoStack.length > 0;
    },

    /**
     * Returns whether redo is available.
     */
    canRedo(): boolean {
      return redoStack.length > 0;
    },

    /**
     * Clears both undo and redo stacks.
     */
    clear(): void {
      undoStack = [];
      redoStack = [];
    },

    /**
     * Returns the current size of the undo stack.
     */
    getUndoStackSize(): number {
      return undoStack.length;
    },

    /**
     * Returns the current size of the redo stack.
     */
    getRedoStackSize(): number {
      return redoStack.length;
    },
  };
}

// ============================================================================
// Singleton Instance for Global Use
// ============================================================================

let globalUndoRedoManager: UndoRedoManager | null = null;

/**
 * Gets or creates the global UndoRedoManager instance.
 */
export function getUndoRedoManager(): UndoRedoManager {
  if (!globalUndoRedoManager) {
    globalUndoRedoManager = createUndoRedoManager();
  }
  return globalUndoRedoManager;
}

/**
 * Resets the global UndoRedoManager (useful for testing).
 */
export function resetUndoRedoManager(): void {
  globalUndoRedoManager = null;
}
