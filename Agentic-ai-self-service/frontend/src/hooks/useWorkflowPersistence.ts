/**
 * Custom hook for workflow persistence with auto-save and restore.
 * Requirement 9.5: WHEN the application loads, THE Workflow_Canvas SHALL restore the last saved workflow state
 * Requirements: 9.1, 9.2, 9.3, 9.4, 9.5
 */

import { useEffect, useCallback, useRef, useState } from 'react';
import { useWorkflowStore } from '../store/workflowStore';
import {
  createAutoSaveService,
  loadWorkflowFromStorage,
  createBackendSaveFunction,
  getStoredWorkflowId,
  setStoredWorkflowId,
  type AutoSaveState,
  type AutoSaveServiceConfig,
} from '../utils/autoSave';
import { WorkflowSerializer } from '../utils/serialization';
import { getApiClient, isApiError } from '../services/api';
import type { SaveStatus } from '../types/workflow';

// ============================================================================
// Types
// ============================================================================

export interface WorkflowPersistenceState {
  saveStatus: SaveStatus;
  lastSaveTime: Date | null;
  error: string | null;
  isRestored: boolean;
  workflowId: string | null;
  isBackendConnected: boolean;
}

export interface UseWorkflowPersistenceOptions {
  autoSaveEnabled?: boolean;
  autoSaveDelay?: number;
  useBackend?: boolean;
  onSaveStatusChange?: (state: AutoSaveState) => void;
  onRestoreComplete?: () => void;
  onRestoreError?: (error: string) => void;
  onWorkflowIdChange?: (id: string) => void;
}

export interface UseWorkflowPersistenceReturn {
  state: WorkflowPersistenceState;
  saveNow: () => Promise<void>;
  restoreWorkflow: () => Promise<boolean>;
  clearSavedWorkflow: () => void;
  loadFromBackend: (workflowId: string) => Promise<boolean>;
}

// ============================================================================
// Hook Implementation
// ============================================================================

export function useWorkflowPersistence(
  options: UseWorkflowPersistenceOptions = {}
): UseWorkflowPersistenceReturn {
  const {
    autoSaveEnabled = true,
    autoSaveDelay,
    useBackend = false,
    onSaveStatusChange,
    onRestoreComplete,
    onRestoreError,
    onWorkflowIdChange,
  } = options;

  const { nodes, edges, viewport, setNodes, setEdges, setViewport } = useWorkflowStore();

  const [state, setState] = useState<WorkflowPersistenceState>({
    saveStatus: 'saved',
    lastSaveTime: null,
    error: null,
    isRestored: false,
    workflowId: getStoredWorkflowId(),
    isBackendConnected: false,
  });

  // Handle workflow ID changes
  const handleWorkflowIdChange = useCallback((id: string) => {
    setStoredWorkflowId(id);
    setState((prev) => ({ ...prev, workflowId: id }));
    onWorkflowIdChange?.(id);
  }, [onWorkflowIdChange]);

  // Create save function based on configuration
  const saveFn = useBackend
    ? createBackendSaveFunction(state.workflowId ?? undefined, handleWorkflowIdChange)
    : undefined;

  // Create auto-save service
  const autoSaveServiceRef = useRef(
    createAutoSaveService({
      saveDelay: autoSaveDelay,
      saveFn,
      onStatusChange: (autoSaveState) => {
        setState((prev) => ({
          ...prev,
          saveStatus: autoSaveState.status,
          lastSaveTime: autoSaveState.lastSaveTime,
          error: autoSaveState.error,
        }));
        onSaveStatusChange?.(autoSaveState);
      },
    } as AutoSaveServiceConfig)
  );

  // Update save function when backend mode changes
  useEffect(() => {
    if (useBackend) {
      const newSaveFn = createBackendSaveFunction(
        state.workflowId ?? undefined,
        handleWorkflowIdChange
      );
      autoSaveServiceRef.current = createAutoSaveService({
        saveDelay: autoSaveDelay,
        saveFn: newSaveFn,
        onStatusChange: (autoSaveState) => {
          setState((prev) => ({
            ...prev,
            saveStatus: autoSaveState.status,
            lastSaveTime: autoSaveState.lastSaveTime,
            error: autoSaveState.error,
          }));
          onSaveStatusChange?.(autoSaveState);
        },
      });
    }
  }, [useBackend, state.workflowId, autoSaveDelay, handleWorkflowIdChange, onSaveStatusChange]);

  // Track previous state for change detection
  const prevStateRef = useRef({ nodes, edges, viewport });

  /**
   * Loads workflow from backend by ID.
   */
  const loadFromBackend = useCallback(async (workflowId: string): Promise<boolean> => {
    try {
      const apiClient = getApiClient();
      const workflow = await apiClient.getWorkflow(workflowId);

      // Convert backend workflow to frontend format
      const restoredNodes = workflow.nodes.map((node: any) => ({
        id: node.id,
        type: node.type,
        position: { x: node.position.x, y: node.position.y },
        data: {
          label: node.data?.label ?? node.type,
          componentType: node.type,
          configuration: node.data?.configuration,
          validationStatus: node.data?.validationStatus ?? 'pending',
        },
        selected: false,
      }));

      const restoredEdges = workflow.edges.map((edge: any) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        sourceHandle: edge.source_handle ?? edge.sourceHandle ?? null,
        targetHandle: edge.target_handle ?? edge.targetHandle ?? null,
        type: edge.type ?? edge.connection_type,
        animated: edge.animated ?? false,
        data: edge.data,
        selected: false,
      }));

      const restoredViewport = {
        x: workflow.viewport.x,
        y: workflow.viewport.y,
        zoom: Math.max(0.1, Math.min(4, workflow.viewport.zoom)),
      };

      setNodes(restoredNodes);
      setEdges(restoredEdges);
      setViewport(restoredViewport);

      setStoredWorkflowId(workflowId);
      setState((prev) => ({
        ...prev,
        isRestored: true,
        workflowId,
        isBackendConnected: true,
        error: null,
      }));

      onRestoreComplete?.();
      return true;
    } catch (error) {
      const errorMessage = isApiError(error)
        ? error.message
        : error instanceof Error
        ? error.message
        : 'Failed to load workflow from backend';

      setState((prev) => ({ ...prev, error: errorMessage }));
      onRestoreError?.(errorMessage);
      return false;
    }
  }, [setNodes, setEdges, setViewport, onRestoreComplete, onRestoreError]);

  /**
   * Restores workflow from local storage or backend.
   * Requirement 9.5: WHEN the application loads, THE Workflow_Canvas SHALL restore the last saved workflow state
   */
  const restoreWorkflow = useCallback(async (): Promise<boolean> => {
    // First, try to restore from backend if we have a workflow ID
    const storedWorkflowId = getStoredWorkflowId();
    if (useBackend && storedWorkflowId) {
      try {
        const success = await loadFromBackend(storedWorkflowId);
        if (success) {
          return true;
        }
        // If backend fails, fall through to local storage
      } catch {
        // Fall through to local storage
      }
    }

    // Fall back to local storage
    try {
      const savedJson = loadWorkflowFromStorage();
      if (!savedJson) {
        setState((prev) => ({ ...prev, isRestored: true }));
        return false;
      }

      // Validate the saved JSON
      const errors = WorkflowSerializer.validateSchema(savedJson);
      if (errors.length > 0) {
        const errorMessage = `Invalid saved workflow: ${errors[0].message}`;
        setState((prev) => ({ ...prev, error: errorMessage, isRestored: true }));
        onRestoreError?.(errorMessage);
        return false;
      }

      // Deserialize and restore
      const { nodes: restoredNodes, edges: restoredEdges, viewport: restoredViewport } =
        WorkflowSerializer.deserialize(savedJson);

      setNodes(restoredNodes);
      setEdges(restoredEdges);
      setViewport(restoredViewport);

      setState((prev) => ({ ...prev, isRestored: true, error: null }));
      onRestoreComplete?.();
      return true;
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : 'Failed to restore workflow';
      setState((prev) => ({ ...prev, error: errorMessage, isRestored: true }));
      onRestoreError?.(errorMessage);
      return false;
    }
  }, [useBackend, loadFromBackend, setNodes, setEdges, setViewport, onRestoreComplete, onRestoreError]);

  /**
   * Forces an immediate save.
   */
  const saveNow = useCallback(async (): Promise<void> => {
    await autoSaveServiceRef.current.saveNow(nodes, edges, viewport);
  }, [nodes, edges, viewport]);

  /**
   * Clears saved workflow from storage.
   */
  const clearSavedWorkflow = useCallback((): void => {
    try {
      localStorage.removeItem('agentcore-workflow');
      localStorage.removeItem('agentcore-workflow-id');
      setState((prev) => ({ ...prev, lastSaveTime: null, workflowId: null }));
    } catch {
      // Ignore errors
    }
  }, []);

  // Restore workflow on mount
  useEffect(() => {
    if (!state.isRestored) {
      restoreWorkflow();
    }
  }, [state.isRestored, restoreWorkflow]);

  // Auto-save on changes
  useEffect(() => {
    if (!autoSaveEnabled || !state.isRestored) return;

    // Check if state has changed
    const prevState = prevStateRef.current;
    const hasChanged =
      nodes !== prevState.nodes ||
      edges !== prevState.edges ||
      viewport !== prevState.viewport;

    if (hasChanged) {
      prevStateRef.current = { nodes, edges, viewport };
      autoSaveServiceRef.current.scheduleAutoSave(nodes, edges, viewport);
    }
  }, [nodes, edges, viewport, autoSaveEnabled, state.isRestored]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      autoSaveServiceRef.current.cancelPendingAutoSave();
    };
  }, []);

  return {
    state,
    saveNow,
    restoreWorkflow,
    clearSavedWorkflow,
    loadFromBackend,
  };
}

export default useWorkflowPersistence;
