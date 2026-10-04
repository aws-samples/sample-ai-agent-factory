/**
 * Zustand store for flow management state.
 * Manages flow list, active flow, and CRUD operations.
 * Requirements: 1.2, 2.1, 2.3, 3.1, 3.4, 4.1, 4.4, 5.3, 6.1, 6.3
 */

import { create } from 'zustand';
import type { Flow, FlowSummary, FlowUpdateRequest } from '../types/flow';
import type { DeploymentStatus, WorkflowDefinition } from '../types/workflow';
import { normalizeDeploymentGovernance } from '../types/workflow';
import { getApiClient, getErrorMessage } from '../services/api';
import { flushPendingSave, saveActiveFlowNow } from '../utils/pendingSave';
import {
  UNBOUND_WORK_HYDRATION_ERROR,
  hydrationWouldDiscardUnboundWork,
  useWorkflowStore,
} from './workflowStore';

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
// Optimistic concurrency (F-15) and pending-save flushing (F-14)
// ============================================================================

/**
 * A save was refused with 409: the server holds a newer version of the flow
 * than the one this tab loaded. Nothing was written. The user decides which
 * side wins; the store never picks silently.
 */
export interface FlowSaveConflict {
  flowId: string;
  /** The server's current version, or null if the row no longer exists. */
  serverVersion: number | null;
  serverUpdatedAt: string | null;
  message: string;
}

export type SaveConflictResolution = 'reload' | 'keep_mine';

export class FlowSaveConflictError extends Error {
  readonly conflict: FlowSaveConflict;

  constructor(conflict: FlowSaveConflict) {
    super(conflict.message);
    this.name = 'FlowSaveConflictError';
    this.conflict = conflict;
  }
}

export const FLOW_SAVE_CONFLICT_MESSAGE =
  'This flow was changed elsewhere since you loaded it. Reload to see the latest version, or keep yours to overwrite it.';

export const PENDING_SAVE_NOT_FLUSHED_ERROR =
  'Your latest changes could not be saved, so the flow was not switched. Retry, or discard them to continue.';

export const FLOW_GONE_ERROR = 'This flow no longer exists on the server.';

export interface OpenFlowOptions {
  /**
   * Skip the pending-save flush and hydrate over whatever the canvas holds.
   * Only for an explicit user decision (a confirmed discard, or "reload their
   * version" after a save conflict).
   */
  discardUnsaved?: boolean;
}

/** The 409 shape `apiRequest` throws, or null for any other error. */
function conflictFromError(flowId: string, err: unknown): FlowSaveConflict | null {
  if (typeof err !== 'object' || err === null) return null;
  const { status, details, message } = err as { status?: unknown; details?: unknown; message?: unknown };
  if (status !== 409) return null;
  const detail = (details as { detail?: unknown } | undefined)?.detail;
  const body = typeof detail === 'object' && detail !== null ? (detail as Record<string, unknown>) : {};
  return {
    flowId,
    serverVersion: typeof body.currentVersion === 'number' ? body.currentVersion : null,
    serverUpdatedAt: typeof body.updatedAt === 'string' ? body.updatedAt : null,
    message: typeof message === 'string' && message ? message : FLOW_SAVE_CONFLICT_MESSAGE,
  };
}

function savedVersionOf(flow: Flow | undefined): number | null {
  return typeof flow?.version === 'number' ? flow.version : null;
}

// ============================================================================
// Store State Interface
// ============================================================================

export interface FlowState {
  // State
  flows: FlowSummary[];
  activeFlowId: string | null;
  activeFlowName: string | null;
  /**
   * The server version the active canvas was loaded from or last saved as.
   * Every save names it; the server refuses (409) when the row has moved.
   */
  activeFlowVersion: number | null;
  saveConflict: FlowSaveConflict | null;
  isLoading: boolean;
  error: string | null;
  // Monotonic request fence: a slower open/create response cannot overwrite the
  // document selected by a newer navigation.
  navigationGeneration: number;
  pendingFlowId: string | null;

  // Actions
  fetchFlows: () => Promise<void>;
  createFlow: (name: string, options?: OpenFlowOptions) => Promise<void>;
  openFlow: (id: string, options?: OpenFlowOptions) => Promise<void>;
  deleteFlow: (id: string) => Promise<void>;
  saveFlow: (
    id: string,
    workflow: WorkflowDefinition,
    options?: { keepalive?: boolean },
  ) => Promise<void>;
  renameFlow: (id: string, name: string) => Promise<void>;
  updateFlowStatus: (id: string, status: DeploymentStatus) => void;
  resolveSaveConflict: (choice: SaveConflictResolution) => Promise<void>;
  clearSaveConflict: () => void;
}

// ============================================================================
// Store Implementation
// ============================================================================

export const useFlowStore = create<FlowState>((set, get) => ({
  // Initial state
  flows: [],
  activeFlowId: null,
  activeFlowName: null,
  activeFlowVersion: null,
  saveConflict: null,
  isLoading: false,
  error: null,
  navigationGeneration: 0,
  pendingFlowId: null,

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
  createFlow: async (name: string, options?: OpenFlowOptions) => {
    const generation = get().navigationGeneration + 1;
    set({
      isLoading: true,
      error: null,
      navigationGeneration: generation,
      pendingFlowId: null,
    });
    try {
      // An edit debounced on the current canvas must reach the server before
      // the canvas is replaced by the new, empty flow (F-14). The active flow
      // is still the current one here, so the flushed save is filed under it.
      if (!options?.discardUnsaved) {
        const flushed = await flushPendingSave();
        if (get().navigationGeneration !== generation) return;
        if (!flushed) throw new Error(PENDING_SAVE_NOT_FLUSHED_ERROR);
      }

      const api = getApiClient();
      const response = await api.createFlow({ name });
      const flow = response.flow;
      if (get().navigationGeneration !== generation) return;

      const backendNodes = (flow.workflow.nodes ?? []) as unknown as Record<string, unknown>[];
      const backendEdges = (flow.workflow.edges ?? []) as unknown as Record<string, unknown>[];
      const document = {
        nodes: backendNodes.map(fromBackendNode) as never[],
        edges: backendEdges.map(fromBackendEdge) as never[],
        viewport: flow.workflow.viewport ?? { x: 0, y: 0, zoom: 1 },
        governance: normalizeDeploymentGovernance(flow.workflow.governance),
      };

      const summary: FlowSummary = {
        id: flow.id,
        name: flow.name,
        deploymentStatus: flow.deploymentStatus,
        createdAt: flow.createdAt,
        updatedAt: flow.updatedAt,
        version: flow.version ?? 0,
      };
      // The canvas may have gained content while this request was in flight
      // (the sidebar auto-creates a flow on an empty account). Work that no flow
      // owns cannot be saved, so it must not be replaced by the empty new flow.
      // The flow exists on the server: list it, but keep the canvas and refuse.
      if (hydrationWouldDiscardUnboundWork(useWorkflowStore.getState())) {
        set((state) => ({ flows: [summary, ...state.flows] }));
        throw new Error(UNBOUND_WORK_HYDRATION_ERROR);
      }

      // Set the active id first. The workflowStore hydration event then carries
      // the same documentFlowId, so an autosave subscriber can never associate
      // this new document with the previously active flow.
      set((state) => ({
        flows: [summary, ...state.flows],
        activeFlowId: flow.id,
        activeFlowName: flow.name,
        activeFlowVersion: flow.version ?? 0,
        saveConflict: null,
        isLoading: false,
        pendingFlowId: null,
      }));
      const workflowState = useWorkflowStore.getState();
      workflowState.replaceWorkflowDocument(document, {
        flowId: flow.id,
        markDirty: false,
      });
    } catch (err: unknown) {
      if (get().navigationGeneration === generation) {
        set({
          error: getErrorMessage(err),
          isLoading: false,
          pendingFlowId: null,
        });
      }
    }
  },

  // Open an existing flow in the editor
  openFlow: async (id: string, options?: OpenFlowOptions) => {
    const generation = get().navigationGeneration + 1;
    set({
      isLoading: true,
      error: null,
      navigationGeneration: generation,
      pendingFlowId: id,
    });
    try {
      // Flush before anything else replaces the canvas (F-14). A debounced edit
      // on the flow being left is saved under that flow while it is still the
      // active one; if that save fails, the switch is refused rather than the
      // edit dropped, unless the caller carries an explicit discard decision.
      if (!options?.discardUnsaved) {
        const flushed = await flushPendingSave();
        if (get().navigationGeneration !== generation) return;
        if (!flushed) throw new Error(PENDING_SAVE_NOT_FLUSHED_ERROR);
      }

      const api = getApiClient();
      const flow = await api.getFlow(id);
      if (get().navigationGeneration !== generation) return;

      // The sidebar auto-opens the most recent flow after sign-in. A template or
      // node placed on the still-unbound canvas while this GET was in flight has
      // no flow to be saved into; hydrating over it would discard it silently.
      // Refuse BEFORE activating the flow so the canvas stays what the user made
      // and the sidebar shows the error.
      if (hydrationWouldDiscardUnboundWork(useWorkflowStore.getState())) {
        throw new Error(UNBOUND_WORK_HYDRATION_ERROR);
      }

      // Load workflow data into workflowStore (convert backend format to React Flow format)
      const backendNodes = (flow.workflow.nodes ?? []) as unknown as Record<string, unknown>[];
      const backendEdges = (flow.workflow.edges ?? []) as unknown as Record<string, unknown>[];
      const document = {
        nodes: backendNodes.map(fromBackendNode) as never[],
        edges: backendEdges.map(fromBackendEdge) as never[],
        viewport: flow.workflow.viewport ?? { x: 0, y: 0, zoom: 1 },
        governance: normalizeDeploymentGovernance(flow.workflow.governance),
      };

      set((state) => ({
        activeFlowId: flow.id,
        activeFlowName: flow.name,
        activeFlowVersion: flow.version ?? 0,
        saveConflict: null,
        flows: state.flows.map((c) =>
          c.id === flow.id
            ? { ...c, name: flow.name, updatedAt: flow.updatedAt, version: flow.version ?? c.version }
            : c
        ),
        isLoading: false,
        pendingFlowId: null,
      }));
      const workflowState = useWorkflowStore.getState();
      workflowState.replaceWorkflowDocument(document, {
        flowId: flow.id,
        markDirty: false,
      });
    } catch (err: unknown) {
      if (get().navigationGeneration === generation) {
        set({
          error: getErrorMessage(err),
          isLoading: false,
          pendingFlowId: null,
        });
      }
    }
  },

  // Delete a flow and remove from local list optimistically
  deleteFlow: async (id: string) => {
    set({ error: null });
    const stateBeforeDelete = get();
    if (stateBeforeDelete.activeFlowId === id || stateBeforeDelete.pendingFlowId === id) {
      set({
        navigationGeneration: stateBeforeDelete.navigationGeneration + 1,
        pendingFlowId: null,
        isLoading: false,
      });
    }
    try {
      const api = getApiClient();
      await api.deleteFlow(id);

      const isDeletingActive = get().activeFlowId === id;
      set((state) => {
        return {
          flows: state.flows.filter((c) => c.id !== id),
          activeFlowId: isDeletingActive ? null : state.activeFlowId,
          activeFlowName: isDeletingActive ? null : state.activeFlowName,
          activeFlowVersion: isDeletingActive ? null : state.activeFlowVersion,
          saveConflict: state.saveConflict?.flowId === id ? null : state.saveConflict,
        };
      });
      if (isDeletingActive) {
        useWorkflowStore.getState().resetWorkflowDocument(null);
      }
    } catch (err: unknown) {
      set({ error: getErrorMessage(err) });
    }
  },

  // Save the current workflow to a flow
  saveFlow: async (id: string, workflow: WorkflowDefinition, options) => {
    try {
      const api = getApiClient();
      const state = get();
      const body: FlowUpdateRequest = { workflow };
      // Name the version this canvas was built on (F-15). The server refuses
      // the write with 409 if another tab or session saved since.
      const knownVersion = state.activeFlowId === id
        ? state.activeFlowVersion
        : state.flows.find((c) => c.id === id)?.version ?? null;
      if (typeof knownVersion === 'number') {
        body.expectedVersion = knownVersion;
      }
      const response = options?.keepalive
        ? await api.updateFlow(id, body, { keepalive: true })
        : await api.updateFlow(id, body);
      const saved = response?.flow;
      const savedVersion = savedVersionOf(saved);
      if (savedVersion !== null) {
        set((s) => ({
          activeFlowVersion: s.activeFlowId === id ? savedVersion : s.activeFlowVersion,
          flows: s.flows.map((c) =>
            c.id === id
              ? { ...c, version: savedVersion, updatedAt: saved?.updatedAt ?? c.updatedAt }
              : c
          ),
          saveConflict: s.saveConflict?.flowId === id ? null : s.saveConflict,
        }));
      }
    } catch (err: unknown) {
      const conflict = conflictFromError(id, err);
      if (conflict) {
        // Do NOT retry or overwrite: surface the conflict for the user to resolve.
        set({ saveConflict: conflict, error: conflict.message });
        throw new FlowSaveConflictError(conflict);
      }
      const message = getErrorMessage(err);
      set({ error: message });
      // useAutoSave owns the dedicated "your work is not being saved" banner,
      // but it can only surface the failure if this promise rejects. Swallowing
      // the API error here made useAutoSave's catch branch unreachable and its
      // following success branch immediately cleared any prior warning.
      throw err instanceof Error ? err : new Error(message);
    }
  },

  // Rename a flow
  renameFlow: async (id: string, name: string) => {
    try {
      const api = getApiClient();
      const state = get();
      const body: FlowUpdateRequest = { name };
      // A rename rewrites the whole row on the server, so it is fenced like a
      // canvas save: renaming from a stale list must not clobber a newer canvas.
      const knownVersion = state.activeFlowId === id
        ? state.activeFlowVersion
        : state.flows.find((c) => c.id === id)?.version ?? null;
      if (typeof knownVersion === 'number') {
        body.expectedVersion = knownVersion;
      }
      const response = await api.updateFlow(id, body);
      const savedVersion = savedVersionOf(response?.flow);
      set((s) => ({
        activeFlowName: s.activeFlowId === id ? name : s.activeFlowName,
        activeFlowVersion: s.activeFlowId === id && savedVersion !== null ? savedVersion : s.activeFlowVersion,
        flows: s.flows.map((c) =>
          c.id === id
            ? {
              ...c,
              name,
              ...(savedVersion !== null
                ? { version: savedVersion, updatedAt: response.flow.updatedAt ?? c.updatedAt }
                : {}),
            }
            : c
        ),
      }));
    } catch (err: unknown) {
      if (conflictFromError(id, err)) {
        // The list was stale. Refresh it so the next attempt carries the current
        // version; the other writer's canvas has not been touched.
        await get().fetchFlows();
        set({ error: 'This flow was changed elsewhere. The list has been refreshed; rename it again.' });
        return;
      }
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

  // The user picked a side after a 409 (F-15).
  resolveSaveConflict: async (choice: SaveConflictResolution) => {
    const conflict = get().saveConflict;
    if (!conflict) return;

    if (choice === 'reload') {
      // Their version wins: re-fetch and hydrate over the local edits. This is an
      // explicit discard, so the pending-save flush (which would only 409 again)
      // is skipped and the hydration cancels the debounced timer.
      set({ saveConflict: null, error: null });
      await get().openFlow(conflict.flowId, { discardUnsaved: true });
      return;
    }

    // Mine wins: adopt the server's version as the fence and re-submit the
    // current canvas through the autosave writer. If the row moved again in
    // the meantime the save 409s and the conflict is surfaced once more.
    if (conflict.serverVersion === null) {
      set({ saveConflict: null, error: FLOW_GONE_ERROR });
      return;
    }
    set((s) => ({
      activeFlowVersion: s.activeFlowId === conflict.flowId ? conflict.serverVersion : s.activeFlowVersion,
      saveConflict: null,
      error: null,
    }));
    await saveActiveFlowNow();
  },

  clearSaveConflict: () => set({ saveConflict: null }),
}));
