/**
 * Zustand store for workflow state management.
 * Manages nodes, edges, viewport, selection state, and undo/redo operations.
 * Requirements: 10.1, 10.2, 10.3, 10.4, 10.5
 */

import { create } from 'zustand';
import type { Node, Edge, Viewport, NodeChange, EdgeChange } from '@xyflow/react';
import { applyNodeChanges, applyEdgeChanges } from '@xyflow/react';
import type { AgentCoreComponentType, ValidationStatus, ConnectionType } from '../types/workflow';
import type { ComponentConfiguration } from '../types/components';
import type { ValidationError } from '../types/validation';
import {
  validateWorkflow,
  type WorkflowValidationState,
  type WorkflowNode,
  type WorkflowEdge,
} from '../utils/validation';
import {
  createUndoRedoManager,
  createAction,
  type UndoRedoManager,
  type WorkflowState as UndoRedoWorkflowState,
  type ActionType,
} from '../utils/undoRedo';

// ============================================================================
// Node Data Type
// ============================================================================

export type ExecutionState = 'idle' | 'running' | 'completed' | 'failed' | 'skipped';

export interface AgentCoreNodeData extends Record<string, unknown> {
  label: string;
  componentType: AgentCoreComponentType;
  configuration?: ComponentConfiguration;
  validationStatus: ValidationStatus;
  validationErrors?: ValidationError[];
  validationWarnings?: ValidationError[];
  executionState?: ExecutionState;
}

// ============================================================================
// Type Aliases for Convenience
// ============================================================================

export type AgentCoreNode = Node<AgentCoreNodeData>;

// ============================================================================
// Store State Interface
// ============================================================================

export interface WorkflowState {
  // Core workflow data
  nodes: AgentCoreNode[];
  edges: Edge[];
  viewport: Viewport;

  // Selection state
  selectedNodeId: string | null;
  selectedEdgeId: string | null;

  // Validation state
  validationState: WorkflowValidationState | null;
  isReadyToDeploy: boolean;

  // Undo/Redo state
  canUndo: boolean;
  canRedo: boolean;

  // Actions
  setNodes: (nodes: AgentCoreNode[]) => void;
  setEdges: (edges: Edge[]) => void;
  setViewport: (viewport: Viewport) => void;

  // Node operations
  onNodesChange: (changes: NodeChange<AgentCoreNode>[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  addNode: (node: AgentCoreNode) => void;
  deleteNode: (nodeId: string) => void;
  updateNodePosition: (nodeId: string, position: { x: number; y: number }) => void;
  updateNodeConfiguration: (nodeId: string, configuration: ComponentConfiguration) => void;

  // Selection operations
  selectNode: (nodeId: string | null) => void;
  selectEdge: (edgeId: string | null) => void;

  // Edge operations
  addEdge: (edge: Edge) => void;
  deleteEdge: (edgeId: string) => void;

  // Template operations
  activeTemplateId: string | null;
  loadTemplate: (nodes: AgentCoreNode[], edges: Edge[], templateId?: string) => void;

  // Validation operations
  runValidation: () => void;

  // Undo/Redo operations
  undo: () => void;
  redo: () => void;
  recordAction: (type: ActionType) => void;

  // Execution state operations
  setNodeExecutionState: (nodeId: string, state: ExecutionState) => void;
  setNodeExecutionStateByType: (componentType: AgentCoreComponentType, state: ExecutionState) => void;
  resetAllExecutionStates: () => void;

  // Internal: Get current state for undo/redo
  _getUndoRedoState: () => UndoRedoWorkflowState;
  _setFromUndoRedoState: (state: UndoRedoWorkflowState) => void;
}

// ============================================================================
// Store Implementation
// ============================================================================

// Helper function to convert store nodes to validation nodes
function toValidationNodes(nodes: AgentCoreNode[]): WorkflowNode[] {
  return nodes.map((node) => ({
    id: node.id,
    type: node.data.componentType,
    data: {
      configuration: node.data.configuration,
      label: node.data.label,
    },
  }));
}

// Helper function to convert store edges to validation edges
function toValidationEdges(edges: Edge[]): WorkflowEdge[] {
  return edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    type: edge.type as ConnectionType | undefined,
  }));
}

// Create a single undo/redo manager instance for the store
const undoRedoManager: UndoRedoManager = createUndoRedoManager();

// Track the previous state for recording actions
let previousState: UndoRedoWorkflowState | null = null;

export const useWorkflowStore = create<WorkflowState>((set, get) => ({
  // Initial state
  nodes: [],
  edges: [],
  viewport: { x: 0, y: 0, zoom: 1 },
  selectedNodeId: null,
  selectedEdgeId: null,
  validationState: null,
  isReadyToDeploy: false,
  canUndo: false,
  canRedo: false,
  activeTemplateId: null,

  // Setters
  setNodes: (nodes) => set({ nodes }),
  setEdges: (edges) => set({ edges }),
  setViewport: (viewport) => set({ viewport }),

  // React Flow change handlers
  onNodesChange: (changes) => {
    set({
      nodes: applyNodeChanges(changes, get().nodes),
    });
  },

  onEdgesChange: (changes) => {
    set({
      edges: applyEdgeChanges(changes, get().edges),
    });
  },

  // Node operations
  addNode: (node) => {
    const state = get();
    // Capture previous state before change
    previousState = state._getUndoRedoState();

    set((state) => ({
      nodes: [...state.nodes, node],
      activeTemplateId: null,
    }));

    // Record the action
    get().recordAction('ADD_NODE');
  },

  deleteNode: (nodeId) => {
    const state = get();
    // Capture previous state before change
    previousState = state._getUndoRedoState();

    set((state) => ({
      nodes: state.nodes.filter((node) => node.id !== nodeId),
      edges: state.edges.filter(
        (edge) => edge.source !== nodeId && edge.target !== nodeId
      ),
      selectedNodeId: state.selectedNodeId === nodeId ? null : state.selectedNodeId,
      activeTemplateId: null,
    }));

    // Record the action
    get().recordAction('REMOVE_NODE');
  },

  updateNodePosition: (nodeId, position) => {
    const state = get();
    // Capture previous state before change
    previousState = state._getUndoRedoState();

    set((state) => ({
      nodes: state.nodes.map((node) =>
        node.id === nodeId ? { ...node, position } : node
      ),
    }));

    // Record the action
    get().recordAction('MOVE_NODE');
  },

  updateNodeConfiguration: (nodeId, configuration) => {
    const state = get();
    // Capture previous state before change
    previousState = state._getUndoRedoState();

    // Extract name from configuration for label
    const configName = (configuration as { name?: string })?.name;

    set((state) => ({
      nodes: state.nodes.map((node) =>
        node.id === nodeId
          ? {
              ...node,
              data: {
                ...node.data,
                configuration,
                label: configName || node.data.label,
              }
            }
          : node
      ),
    }));

    // Record the action and run validation
    get().recordAction('UPDATE_CONFIG');
    get().runValidation();
  },

  // Selection operations
  selectNode: (nodeId) => {
    set((state) => ({
      selectedNodeId: nodeId,
      selectedEdgeId: nodeId ? null : state.selectedEdgeId,
      nodes: state.nodes.map((node) => ({
        ...node,
        selected: node.id === nodeId,
      })),
    }));
  },

  selectEdge: (edgeId) => {
    set((state) => ({
      selectedEdgeId: edgeId,
      selectedNodeId: edgeId ? null : state.selectedNodeId,
      edges: state.edges.map((edge) => ({
        ...edge,
        selected: edge.id === edgeId,
      })),
    }));
  },

  // Edge operations
  addEdge: (edge) => {
    const state = get();
    // Capture previous state before change
    previousState = state._getUndoRedoState();

    set((state) => ({
      edges: [...state.edges, edge],
      activeTemplateId: null,
    }));

    // Record the action
    get().recordAction('ADD_EDGE');
  },

  deleteEdge: (edgeId) => {
    const state = get();
    // Capture previous state before change
    previousState = state._getUndoRedoState();

    set((state) => ({
      edges: state.edges.filter((edge) => edge.id !== edgeId),
      selectedEdgeId: state.selectedEdgeId === edgeId ? null : state.selectedEdgeId,
      activeTemplateId: null,
    }));

    // Record the action
    get().recordAction('REMOVE_EDGE');
  },

  // Template operations
  loadTemplate: (templateNodes, templateEdges, templateId) => {
    const state = get();
    previousState = state._getUndoRedoState();

    set({
      nodes: templateNodes,
      edges: templateEdges,
      selectedNodeId: null,
      selectedEdgeId: null,
      activeTemplateId: templateId || null,
    });

    get().recordAction('ADD_NODE');
    get().runValidation();
  },

  // Validation operations
  runValidation: () => {
    const state = get();
    const validationNodes = toValidationNodes(state.nodes);
    const validationEdges = toValidationEdges(state.edges);
    const validationState = validateWorkflow(validationNodes, validationEdges);

    // Update nodes with validation status
    const updatedNodes = state.nodes.map((node) => {
      const nodeState = validationState.nodeStates.get(node.id);
      return {
        ...node,
        data: {
          ...node.data,
          validationStatus: nodeState?.status ?? 'pending',
          validationErrors: nodeState?.errors ?? [],
          validationWarnings: nodeState?.warnings ?? [],
        },
      };
    });

    // Update edges with validation status
    const updatedEdges = state.edges.map((edge) => {
      const edgeState = validationState.edgeStates.get(edge.id);
      return {
        ...edge,
        data: {
          ...edge.data,
          validationStatus: edgeState?.status ?? 'valid',
          validationErrors: edgeState?.errors ?? [],
        },
      };
    });

    set({
      nodes: updatedNodes,
      edges: updatedEdges,
      validationState,
      isReadyToDeploy: validationState.isReadyToDeploy,
    });
  },

  // Execution state operations
  setNodeExecutionState: (nodeId, executionState) => {
    set((state) => ({
      nodes: state.nodes.map((node) =>
        node.id === nodeId
          ? { ...node, data: { ...node.data, executionState } }
          : node
      ),
    }));
  },

  setNodeExecutionStateByType: (componentType, executionState) => {
    set((state) => ({
      nodes: state.nodes.map((node) =>
        node.data.componentType === componentType
          ? { ...node, data: { ...node.data, executionState } }
          : node
      ),
    }));
  },

  resetAllExecutionStates: () => {
    set((state) => ({
      nodes: state.nodes.map((node) => ({
        ...node,
        data: { ...node.data, executionState: 'idle' as ExecutionState },
      })),
    }));
  },

  // Undo/Redo operations
  /**
   * Undoes the last action and restores the previous workflow state.
   * Requirement 10.1: WHEN a user presses Ctrl+Z, THE Workflow_Canvas SHALL undo the last action
   * Requirement 10.4: WHEN an action is undone, THE Workflow_Canvas SHALL restore the previous state
   */
  undo: () => {
    const restoredState = undoRedoManager.undo();
    if (restoredState) {
      get()._setFromUndoRedoState(restoredState);
      set({
        canUndo: undoRedoManager.canUndo(),
        canRedo: undoRedoManager.canRedo(),
      });
    }
  },

  /**
   * Redoes the last undone action and restores the new workflow state.
   * Requirement 10.2: WHEN a user presses Ctrl+Shift+Z, THE Workflow_Canvas SHALL redo the last undone action
   */
  redo: () => {
    const restoredState = undoRedoManager.redo();
    if (restoredState) {
      get()._setFromUndoRedoState(restoredState);
      set({
        canUndo: undoRedoManager.canUndo(),
        canRedo: undoRedoManager.canRedo(),
      });
    }
  },

  /**
   * Records an action for undo/redo.
   */
  recordAction: (type: ActionType) => {
    if (!previousState) return;

    const currentState = get()._getUndoRedoState();
    const action = createAction(type, previousState, currentState);
    undoRedoManager.push(action);

    set({
      canUndo: undoRedoManager.canUndo(),
      canRedo: undoRedoManager.canRedo(),
    });

    // Clear previous state
    previousState = null;
  },

  // Internal helpers for undo/redo state management
  _getUndoRedoState: (): UndoRedoWorkflowState => {
    const state = get();
    return {
      nodes: state.nodes,
      edges: state.edges,
      viewport: state.viewport,
    };
  },

  _setFromUndoRedoState: (undoRedoState: UndoRedoWorkflowState) => {
    set({
      nodes: undoRedoState.nodes,
      edges: undoRedoState.edges,
      viewport: undoRedoState.viewport,
    });
  },
}));

// Export the undo/redo manager for testing purposes
export { undoRedoManager };
