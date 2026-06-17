/**
 * Zustand store for flow management state.
 * Manages flow list, active flow, and CRUD operations.
 * Requirements: 1.2, 2.1, 2.3, 3.1, 3.4, 4.1, 4.4, 5.3, 6.1, 6.3
 */

import { create } from 'zustand';
import type { FlowSummary } from '../types/flow';
import type { DeploymentStatus, WorkflowDefinition } from '../types/workflow';
import { getApiClient, getErrorMessage } from '../services/api';
import { useWorkflowStore } from './workflowStore';

// ============================================================================
// Backend → React Flow Conversion Helpers
// ============================================================================

/**
 * Converts a snake_case string to camelCase.
 */
function toCamelCase(str: string): string {
  if (typeof str !== 'string') return String(str);
  return str.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
}

/**
 * Recursively converts all object keys from snake_case to camelCase.
 */
function keysToCamelCase(obj: unknown): unknown {
  if (Array.isArray(obj)) {
    return obj.map(keysToCamelCase);
  }
  if (obj !== null && typeof obj === 'object') {
    const result: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
      result[toCamelCase(key)] = keysToCamelCase(value);
    }
    return result;
  }
  return obj;
}

/**
 * Converts a backend ComponentNode to a React Flow AgentCoreNode.
 * Backend format: { id, type: "runtime", data: RuntimeConfiguration (snake_case), ... }
 * React Flow format: { id, type: "agentComponent", data: { label, componentType, configuration (camelCase), validationStatus }, ... }
 */
function fromBackendNode(node: Record<string, unknown>): Record<string, unknown> {
  const data = node.data as Record<string, unknown> | undefined;
  const componentType = (node.type as string) || (data?.component_type as string) || (data?.componentType as string) || 'runtime';
  const position = node.position as { x: number; y: number } | undefined;

  // Convert snake_case config keys to camelCase for frontend compatibility
  const configuration = data ? keysToCamelCase(data) : undefined;

  return {
    id: node.id || `node-${Date.now()}`,
    type: componentType,
    position: { x: position?.x ?? 0, y: position?.y ?? 0 },
    data: {
      label: (data?.name as string) || componentType || 'Unknown',
      componentType: componentType || 'runtime',
      configuration,
      validationStatus: (node.validation_status as string) || (node.validationStatus as string) || 'valid',
    },
    selected: node.selected ?? false,
  };
}

/**
 * Converts a backend ConnectionEdge to a React Flow Edge.
 */
function fromBackendEdge(edge: Record<string, unknown>): Record<string, unknown> {
  const edgeData = edge.data as Record<string, unknown> | undefined;
  const backendType = (edge.type as string) || 'data';

  // Map backend ConnectionType back to frontend connectionType
  const connectionTypeMap: Record<string, string> = {
    data: 'data',
    authentication: 'identity',
    policy: 'policy',
  };

  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.source_handle ?? edge.sourceHandle ?? null,
    targetHandle: edge.target_handle ?? edge.targetHandle ?? null,
    type: 'connection',
    data: {
      connectionType: connectionTypeMap[backendType] || edgeData?.connectionType || 'data',
      validationStatus: edgeData?.validation_status ?? edgeData?.validationStatus ?? 'valid',
    },
  };
}

// ============================================================================
// Store State Interface
// ============================================================================

export interface FlowState {
  // State
  flows: FlowSummary[];
  activeFlowId: string | null;
  activeFlowName: string | null;
  isLoading: boolean;
  error: string | null;

  // Actions
  fetchFlows: () => Promise<void>;
  createFlow: (name: string) => Promise<void>;
  openFlow: (id: string) => Promise<void>;
  deleteFlow: (id: string) => Promise<void>;
  saveFlow: (id: string, workflow: WorkflowDefinition) => Promise<void>;
  renameFlow: (id: string, name: string) => Promise<void>;
  updateFlowStatus: (id: string, status: DeploymentStatus) => void;
}

// ============================================================================
// Store Implementation
// ============================================================================

export const useFlowStore = create<FlowState>((set) => ({
  // Initial state
  flows: [],
  activeFlowId: null,
  activeFlowName: null,
  isLoading: false,
  error: null,

  // Fetch all flows from the API
  fetchFlows: async () => {
    set({ isLoading: true, error: null });
    try {
      const api = getApiClient();
      const response = await api.listFlows();
      set({ flows: response.flows, isLoading: false });
    } catch (err: unknown) {
      set({ error: getErrorMessage(err), isLoading: false });
    }
  },

  // Create a new flow and navigate to editor
  createFlow: async (name: string) => {
    set({ isLoading: true, error: null });
    try {
      const api = getApiClient();
      const response = await api.createFlow({ name });
      const flow = response.flow;

      // Load the new flow workflow into workflowStore
      const workflowState = useWorkflowStore.getState();
      workflowState.setNodes([]);
      workflowState.setEdges([]);
      workflowState.setViewport(flow.workflow.viewport ?? { x: 0, y: 0, zoom: 1 });

      set((state) => ({
        flows: [{ id: flow.id, name: flow.name, deploymentStatus: flow.deploymentStatus, createdAt: flow.createdAt, updatedAt: flow.updatedAt }, ...state.flows],
        activeFlowId: flow.id,
        activeFlowName: flow.name,
        isLoading: false,
      }));
    } catch (err: unknown) {
      set({ error: getErrorMessage(err), isLoading: false });
    }
  },

  // Open an existing flow in the editor
  openFlow: async (id: string) => {
    set({ isLoading: true, error: null });
    try {
      const api = getApiClient();
      const flow = await api.getFlow(id);

      // Load workflow data into workflowStore (convert backend format to React Flow format)
      const workflowState = useWorkflowStore.getState();
      const backendNodes = (flow.workflow.nodes ?? []) as unknown as Record<string, unknown>[];
      const backendEdges = (flow.workflow.edges ?? []) as unknown as Record<string, unknown>[];
      workflowState.setNodes(backendNodes.map(fromBackendNode) as never[]);
      workflowState.setEdges(backendEdges.map(fromBackendEdge) as never[]);
      workflowState.setViewport(flow.workflow.viewport ?? { x: 0, y: 0, zoom: 1 });

      set({
        activeFlowId: flow.id,
        activeFlowName: flow.name,
        isLoading: false,
      });
    } catch (err: unknown) {
      set({ error: getErrorMessage(err), isLoading: false });
    }
  },

  // Delete a flow and remove from local list optimistically
  deleteFlow: async (id: string) => {
    set({ error: null });
    try {
      const api = getApiClient();
      await api.deleteFlow(id);

      // Optimistically remove from local list after API success
      set((state) => {
        const isDeletingActive = state.activeFlowId === id;

        if (isDeletingActive) {
          // Reset workflowStore canvas when deleting the active flow
          const workflowState = useWorkflowStore.getState();
          workflowState.setNodes([]);
          workflowState.setEdges([]);
          workflowState.setViewport({ x: 0, y: 0, zoom: 1 });
        }

        return {
          flows: state.flows.filter((c) => c.id !== id),
          activeFlowId: isDeletingActive ? null : state.activeFlowId,
          activeFlowName: isDeletingActive ? null : state.activeFlowName,
        };
      });
    } catch (err: unknown) {
      set({ error: getErrorMessage(err) });
    }
  },

  // Save the current workflow to a flow
  saveFlow: async (id: string, workflow: WorkflowDefinition) => {
    try {
      const api = getApiClient();
      await api.updateFlow(id, { workflow });
    } catch (err: unknown) {
      set({ error: getErrorMessage(err) });
    }
  },

  // Rename a flow
  renameFlow: async (id: string, name: string) => {
    try {
      const api = getApiClient();
      await api.updateFlow(id, { name });
      set((state) => ({
        activeFlowName: state.activeFlowId === id ? name : state.activeFlowName,
        flows: state.flows.map((c) =>
          c.id === id ? { ...c, name } : c
        ),
      }));
    } catch (err: unknown) {
      set({ error: getErrorMessage(err) });
    }
  },

  // Update deployment status for a flow in the local list
  updateFlowStatus: (id: string, status: DeploymentStatus) => {
    set((state) => ({
      flows: state.flows.map((c) =>
        c.id === id ? { ...c, deploymentStatus: status } : c
      ),
    }));
  },
}));
