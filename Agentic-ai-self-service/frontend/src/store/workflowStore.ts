/**
 * Zustand store for workflow state management.
 * Manages nodes, edges, viewport, selection state, and undo/redo operations.
 * Requirements: 10.1, 10.2, 10.3, 10.4, 10.5
 */

import { create } from 'zustand';
import type { Node, Edge, Viewport, NodeChange, EdgeChange } from '@xyflow/react';
import { applyNodeChanges, applyEdgeChanges } from '@xyflow/react';
import {
  createEmptyDeploymentGovernance,
  normalizeDeploymentGovernance,
  type AgentCoreComponentType,
  type ConnectionType,
  type DeploymentGovernanceV1,
  type ValidationStatus,
} from '../types/workflow';
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

export interface WorkflowDocument {
  nodes: AgentCoreNode[];
  edges: Edge[];
  viewport: Viewport;
  governance: DeploymentGovernanceV1;
}

export interface ReplaceWorkflowDocumentOptions {
  flowId: string | null;
  /**
   * Hydration must never auto-save itself. User-initiated full replacements
   * (for example a registry clone) are marked dirty and do auto-save.
   */
  markDirty?: boolean;
}

// ============================================================================
// Store State Interface
// ============================================================================

export interface WorkflowState {
  // Core workflow data
  nodes: AgentCoreNode[];
  edges: Edge[];
  viewport: Viewport;
  governance: DeploymentGovernanceV1;

  // Persistence identity/versioning. A hydration bump cancels pending saves from
  // the previous document; persistenceRevision advances only for user changes.
  documentFlowId: string | null;
  hydrationVersion: number;
  persistenceRevision: number;

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
  setGovernance: (
    governance:
      | DeploymentGovernanceV1
      | ((current: DeploymentGovernanceV1) => DeploymentGovernanceV1),
  ) => void;
  replaceWorkflowDocument: (
    document: WorkflowDocument,
    options: ReplaceWorkflowDocumentOptions,
  ) => void;
  resetWorkflowDocument: (flowId: string | null) => void;

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

/**
 * A hydration (`markDirty: false`) replaces the whole canvas. Content on a canvas
 * that no flow owns can never be auto-saved (useAutoSave requires
 * documentFlowId === activeFlowId), so replacing it is silent data loss.
 *
 * Measured live: FlowSidebar auto-opens the most recent flow after sign-in; a
 * template chosen while that GET was in flight loaded onto the empty canvas and
 * was then overwritten by the arriving flow with no confirmation. flowStore
 * checks this before activating a flow; the store refuses as a tripwire.
 */
export function hydrationWouldDiscardUnboundWork(
  state: Pick<WorkflowState, 'documentFlowId' | 'nodes'>,
): boolean {
  return state.documentFlowId === null && state.nodes.length > 0;
}

export const UNBOUND_WORK_HYDRATION_ERROR =
  'Cannot open a flow over unsaved work on a canvas that is not bound to any flow';

// Create a single undo/redo manager instance for the store
const undoRedoManager: UndoRedoManager = createUndoRedoManager();

// Track the previous state for recording actions
let previousState: UndoRedoWorkflowState | null = null;

export const useWorkflowStore = create<WorkflowState>((set, get) => ({
  // Initial state
  nodes: [],
  edges: [],
  viewport: { x: 0, y: 0, zoom: 1 },
  governance: createEmptyDeploymentGovernance(),
  documentFlowId: null,
  hydrationVersion: 0,
  persistenceRevision: 0,
  selectedNodeId: null,
  selectedEdgeId: null,
  validationState: null,
  isReadyToDeploy: false,
  canUndo: false,
  canRedo: false,
  activeTemplateId: null,

  // Setters
  setNodes: (nodes) => set((state) => ({
    nodes,
    persistenceRevision: state.persistenceRevision + 1,
  })),
  setEdges: (edges) => set((state) => ({
    edges,
    persistenceRevision: state.persistenceRevision + 1,
  })),
  setViewport: (viewport) => set((state) => ({
    viewport,
    persistenceRevision: state.persistenceRevision + 1,
  })),
  setGovernance: (governance) => {
    set((state) => {
      const next = typeof governance === 'function'
        ? governance(state.governance)
        : governance;
      const normalized = normalizeDeploymentGovernance(next);
      if (JSON.stringify(state.governance) === JSON.stringify(normalized)) return state;
      return {
        governance: normalized,
        persistenceRevision: state.persistenceRevision + 1,
      };
    });
  },
  replaceWorkflowDocument: (document, options) => {
    const governance = normalizeDeploymentGovernance(document.governance);
    const current = get();
    if (options.markDirty && options.flowId === null) {
      throw new Error('A user-initiated workflow replacement requires an open flow');
    }
    if (options.markDirty && options.flowId !== current.documentFlowId) {
      throw new Error(
        'A user-initiated workflow replacement must target the currently hydrated flow',
      );
    }
    if (!options.markDirty && hydrationWouldDiscardUnboundWork(current)) {
      throw new Error(UNBOUND_WORK_HYDRATION_ERROR);
    }
    undoRedoManager.clear();
    previousState = null;
    set((state) => ({
      nodes: document.nodes,
      edges: document.edges,
      viewport: document.viewport,
      governance,
      documentFlowId: options.flowId,
      hydrationVersion: options.markDirty
        ? state.hydrationVersion
        : state.hydrationVersion + 1,
      persistenceRevision: options.markDirty
        ? state.persistenceRevision + 1
        : 0,
      selectedNodeId: null,
      selectedEdgeId: null,
      validationState: null,
      isReadyToDeploy: false,
      canUndo: false,
      canRedo: false,
      activeTemplateId: null,
    }));
    get().runValidation();
  },
  resetWorkflowDocument: (flowId) => {
    undoRedoManager.clear();
    previousState = null;
    set((state) => ({
      nodes: [],
      edges: [],
      viewport: { x: 0, y: 0, zoom: 1 },
      governance: createEmptyDeploymentGovernance(),
      documentFlowId: flowId,
      hydrationVersion: state.hydrationVersion + 1,
      persistenceRevision: 0,
      selectedNodeId: null,
      selectedEdgeId: null,
      validationState: null,
      isReadyToDeploy: false,
      canUndo: false,
      canRedo: false,
      activeTemplateId: null,
    }));
  },

  // React Flow change handlers
  onNodesChange: (changes) => {
    set((state) => ({
      nodes: applyNodeChanges(changes, state.nodes),
      persistenceRevision: state.persistenceRevision + 1,
    }));
  },

  onEdgesChange: (changes) => {
    set((state) => ({
      edges: applyEdgeChanges(changes, state.edges),
      persistenceRevision: state.persistenceRevision + 1,
    }));
  },

  // Node operations
  addNode: (node) => {
    const state = get();
    // Capture previous state before change
    previousState = state._getUndoRedoState();

    set((state) => ({
      nodes: [...state.nodes, node],
      activeTemplateId: null,
      persistenceRevision: state.persistenceRevision + 1,
    }));

    // Record the action
    get().recordAction('ADD_NODE');
    // Every structural mutation must re-validate. The debounced `useValidation`
    // hook exists but is mounted nowhere, so before this the only validation
    // triggers were `loadTemplate` and `updateNodeConfiguration` — dragging a
    // pre-configured tool node on (which deliberately opens no config modal) left
    // the canvas indicator reading a verdict for the PREVIOUS shape of the graph,
    // or "Validation Pending" if none had ever run. Calling it here is safe;
    // mounting `useValidation` is not, because `runValidation` rebuilds the nodes
    // array and would re-trigger its own `[nodes, edges]` effect forever.
    get().runValidation();
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
      persistenceRevision: state.persistenceRevision + 1,
    }));

    // Record the action
    get().recordAction('REMOVE_NODE');
    // Deleting a node can only change the verdict — e.g. removing the node that
    // held the only error must clear the error count, not leave it on screen.
    get().runValidation();
  },

  updateNodePosition: (nodeId, position) => {
    const state = get();
    // Capture previous state before change
    previousState = state._getUndoRedoState();

    set((state) => ({
      nodes: state.nodes.map((node) =>
        node.id === nodeId ? { ...node, position } : node
      ),
      persistenceRevision: state.persistenceRevision + 1,
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
      persistenceRevision: state.persistenceRevision + 1,
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
      persistenceRevision: state.persistenceRevision + 1,
    }));

    // Record the action
    get().recordAction('ADD_EDGE');
    // Connection validity (validateConnection) is only computed by runValidation,
    // so without this an illegal connection the user just drew renders as valid.
    get().runValidation();
  },

  deleteEdge: (edgeId) => {
    const state = get();
    // Capture previous state before change
    previousState = state._getUndoRedoState();

    set((state) => ({
      edges: state.edges.filter((edge) => edge.id !== edgeId),
      selectedEdgeId: state.selectedEdgeId === edgeId ? null : state.selectedEdgeId,
      activeTemplateId: null,
      persistenceRevision: state.persistenceRevision + 1,
    }));

    // Record the action
    get().recordAction('REMOVE_EDGE');
    get().runValidation();
  },

  // Template operations
  loadTemplate: (templateNodes, templateEdges, templateId) => {
    const state = get();
    previousState = state._getUndoRedoState();

    set((current) => ({
      nodes: templateNodes,
      edges: templateEdges,
      selectedNodeId: null,
      selectedEdgeId: null,
      activeTemplateId: templateId || null,
      persistenceRevision: current.persistenceRevision + 1,
    }));

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
      // The restored graph is a different graph; its verdict must be recomputed
      // or the indicator keeps describing the state we just undid.
      get().runValidation();
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
      get().runValidation();
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
    set((state) => ({
      nodes: undoRedoState.nodes,
      edges: undoRedoState.edges,
      viewport: undoRedoState.viewport,
      persistenceRevision: state.persistenceRevision + 1,
    }));
  },
}));

// Export the undo/redo manager for testing purposes
export { undoRedoManager };
