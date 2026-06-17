/**
 * AutoSaveService for automatic workflow persistence.
 * Implements debounced auto-save with retry on failure.
 * Requirements: 9.1, 9.2, 9.3, 9.4
 */

import type { Viewport } from '@xyflow/react';
import type { AgentCoreNode } from '../store/workflowStore';
import type { Edge } from '@xyflow/react';
import type { SaveStatus } from '../types/workflow';
import { WorkflowSerializer, type SerializedMetadata } from './serialization';
import { getApiClient, isApiError } from '../services/api';

// ============================================================================
// Constants
// ============================================================================

/**
 * Auto-save delay in milliseconds.
 * Requirement 9.1: THE Workflow_Canvas SHALL auto-save the workflow within 5 seconds
 */
export const AUTO_SAVE_DELAY_MS = 5000;

/**
 * Maximum retry attempts for failed saves.
 */
export const MAX_RETRY_ATTEMPTS = 3;

/**
 * Delay between retry attempts in milliseconds.
 */
export const RETRY_DELAY_MS = 1000;

/**
 * Local storage key for workflow data.
 */
export const WORKFLOW_STORAGE_KEY = 'agentcore-workflow';

/**
 * Local storage key for workflow metadata.
 */
export const WORKFLOW_METADATA_KEY = 'agentcore-workflow-metadata';

// ============================================================================
// Types
// ============================================================================

export interface AutoSaveState {
  status: SaveStatus;
  lastSaveTime: Date | null;
  error: string | null;
  retryCount: number;
}

export interface SaveResult {
  success: boolean;
  timestamp: Date;
  error?: string;
}

export type SaveFunction = (data: string) => Promise<SaveResult>;

export interface AutoSaveServiceConfig {
  saveDelay?: number;
  maxRetries?: number;
  retryDelay?: number;
  onStatusChange?: (state: AutoSaveState) => void;
  saveFn?: SaveFunction;
}

// ============================================================================
// AutoSaveService Class
// ============================================================================

export class AutoSaveService {
  private saveDelay: number;
  private maxRetries: number;
  private retryDelay: number;
  private onStatusChange?: (state: AutoSaveState) => void;
  private saveFn: SaveFunction;

  private timeoutId: ReturnType<typeof setTimeout> | null = null;
  private state: AutoSaveState = {
    status: 'saved',
    lastSaveTime: null,
    error: null,
    retryCount: 0,
  };

  private pendingData: string | null = null;

  constructor(config: AutoSaveServiceConfig = {}) {
    this.saveDelay = config.saveDelay ?? AUTO_SAVE_DELAY_MS;
    this.maxRetries = config.maxRetries ?? MAX_RETRY_ATTEMPTS;
    this.retryDelay = config.retryDelay ?? RETRY_DELAY_MS;
    this.onStatusChange = config.onStatusChange;
    this.saveFn = config.saveFn ?? defaultSaveFunction;
  }

  /**
   * Schedules an auto-save operation.
   * Requirement 9.1: WHEN a user makes any change to the workflow, THE Workflow_Canvas SHALL auto-save within 5 seconds
   */
  scheduleAutoSave(
    nodes: AgentCoreNode[],
    edges: Edge[],
    viewport: Viewport,
    metadata?: Partial<SerializedMetadata>,
    workflowInfo?: { id?: string; name?: string; description?: string; version?: string }
  ): void {
    // Serialize the workflow data
    const data = WorkflowSerializer.serialize(nodes, edges, viewport, metadata, workflowInfo);
    this.pendingData = data;

    // Update status to pending
    this.updateState({ status: 'pending', error: null, retryCount: 0 });

    // Cancel any existing timeout
    this.cancelPendingAutoSave();

    // Schedule new save
    this.timeoutId = setTimeout(() => {
      this.executeSave();
    }, this.saveDelay);
  }

  /**
   * Cancels any pending auto-save operation.
   */
  cancelPendingAutoSave(): void {
    if (this.timeoutId !== null) {
      clearTimeout(this.timeoutId);
      this.timeoutId = null;
    }
  }

  /**
   * Forces an immediate save operation.
   */
  async saveNow(
    nodes: AgentCoreNode[],
    edges: Edge[],
    viewport: Viewport,
    metadata?: Partial<SerializedMetadata>,
    workflowInfo?: { id?: string; name?: string; description?: string; version?: string }
  ): Promise<SaveResult> {
    this.cancelPendingAutoSave();
    const data = WorkflowSerializer.serialize(nodes, edges, viewport, metadata, workflowInfo);
    this.pendingData = data;
    return this.executeSave();
  }

  /**
   * Gets the last save time.
   * Requirement 9.2: THE Workflow_Canvas SHALL display a save status indicator showing last save time
   */
  getLastSaveTime(): Date | null {
    return this.state.lastSaveTime;
  }

  /**
   * Gets the current save status.
   */
  getSaveStatus(): SaveStatus {
    return this.state.status;
  }

  /**
   * Gets the current state.
   */
  getState(): AutoSaveState {
    return { ...this.state };
  }

  /**
   * Resets the service state.
   */
  reset(): void {
    this.cancelPendingAutoSave();
    this.pendingData = null;
    this.state = {
      status: 'saved',
      lastSaveTime: null,
      error: null,
      retryCount: 0,
    };
  }

  // ============================================================================
  // Private Methods
  // ============================================================================

  private async executeSave(): Promise<SaveResult> {
    if (!this.pendingData) {
      return { success: false, timestamp: new Date(), error: 'No data to save' };
    }

    // Update status to saving
    this.updateState({ status: 'saving' });

    try {
      const result = await this.saveFn(this.pendingData);

      if (result.success) {
        // Requirement 9.3: WHEN auto-save completes successfully, THE Workflow_Canvas SHALL briefly display a saved confirmation
        this.updateState({
          status: 'saved',
          lastSaveTime: result.timestamp,
          error: null,
          retryCount: 0,
        });
        this.pendingData = null;
        return result;
      } else {
        throw new Error(result.error ?? 'Save failed');
      }
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      return this.handleSaveError(errorMessage);
    }
  }

  /**
   * Handles save errors with retry logic.
   * Requirement 9.4: IF auto-save fails, THEN THE Workflow_Canvas SHALL display an error notification and retry
   */
  private async handleSaveError(errorMessage: string): Promise<SaveResult> {
    const newRetryCount = this.state.retryCount + 1;

    if (newRetryCount <= this.maxRetries) {
      // Update state with retry count
      this.updateState({
        status: 'error',
        error: `Save failed, retrying (${newRetryCount}/${this.maxRetries})...`,
        retryCount: newRetryCount,
      });

      // Wait before retrying
      await this.delay(this.retryDelay);

      // Retry the save
      return this.executeSave();
    } else {
      // Max retries exceeded
      this.updateState({
        status: 'error',
        error: `Save failed after ${this.maxRetries} attempts: ${errorMessage}`,
        retryCount: newRetryCount,
      });

      return {
        success: false,
        timestamp: new Date(),
        error: errorMessage,
      };
    }
  }

  private updateState(updates: Partial<AutoSaveState>): void {
    this.state = { ...this.state, ...updates };
    this.onStatusChange?.(this.state);
  }

  private delay(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}

// ============================================================================
// Default Save Function (Local Storage)
// ============================================================================

/**
 * Default save function that persists to local storage.
 */
export async function defaultSaveFunction(data: string): Promise<SaveResult> {
  try {
    localStorage.setItem(WORKFLOW_STORAGE_KEY, data);
    return {
      success: true,
      timestamp: new Date(),
    };
  } catch (error) {
    return {
      success: false,
      timestamp: new Date(),
      error: error instanceof Error ? error.message : 'Failed to save to local storage',
    };
  }
}

/**
 * Loads workflow from local storage.
 * Requirement 9.5: WHEN the application loads, THE Workflow_Canvas SHALL restore the last saved workflow state
 */
export function loadWorkflowFromStorage(): string | null {
  try {
    return localStorage.getItem(WORKFLOW_STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * Clears workflow from local storage.
 */
export function clearWorkflowStorage(): void {
  try {
    localStorage.removeItem(WORKFLOW_STORAGE_KEY);
    localStorage.removeItem(WORKFLOW_METADATA_KEY);
  } catch {
    // Ignore errors
  }
}

// ============================================================================
// Backend Save Function
// ============================================================================

/**
 * Local storage key for workflow ID (used for backend sync).
 */
export const WORKFLOW_ID_KEY = 'agentcore-workflow-id';

/**
 * Creates a save function that persists to the backend API.
 * Falls back to local storage if backend is unavailable.
 * Requirements: 9.1, 9.4
 */
export function createBackendSaveFunction(
  workflowId?: string,
  onWorkflowIdChange?: (id: string) => void
): SaveFunction {
  let currentWorkflowId = workflowId;

  return async (data: string): Promise<SaveResult> => {
    const apiClient = getApiClient();

    try {
      // Parse the serialized workflow data
      const workflowData = JSON.parse(data);

      // Prepare the request data
      const requestData = {
        name: workflowData.name || 'Untitled Workflow',
        description: workflowData.description || '',
        version: workflowData.version || '1.0.0',
        nodes: workflowData.nodes || [],
        edges: workflowData.edges || [],
        viewport: workflowData.viewport || { x: 0, y: 0, zoom: 1 },
        metadata: workflowData.metadata || {
          author: '',
          tags: [],
          awsRegion: 'us-east-1',
          deploymentStatus: 'not_deployed',
        },
      };

      if (currentWorkflowId) {
        // Update existing workflow
        await apiClient.updateWorkflow(currentWorkflowId, requestData);

        // Also save to local storage as backup
        localStorage.setItem(WORKFLOW_STORAGE_KEY, data);
        localStorage.setItem(WORKFLOW_ID_KEY, currentWorkflowId);

        return {
          success: true,
          timestamp: new Date(),
        };
      } else {
        // Create new workflow
        const response = await apiClient.createWorkflow(requestData);
        currentWorkflowId = response.workflow.id;

        // Notify about new workflow ID
        onWorkflowIdChange?.(currentWorkflowId);

        // Save to local storage as backup
        localStorage.setItem(WORKFLOW_STORAGE_KEY, data);
        localStorage.setItem(WORKFLOW_ID_KEY, currentWorkflowId);

        return {
          success: true,
          timestamp: new Date(),
        };
      }
    } catch (error) {
      // If backend fails, fall back to local storage
      console.warn('Backend save failed, falling back to local storage:', error);

      try {
        localStorage.setItem(WORKFLOW_STORAGE_KEY, data);

        // Return success but with a warning
        return {
          success: true,
          timestamp: new Date(),
          error: isApiError(error)
            ? `Backend unavailable (${error.message}), saved locally`
            : 'Backend unavailable, saved locally',
        };
      } catch (localError) {
        return {
          success: false,
          timestamp: new Date(),
          error: isApiError(error)
            ? error.message
            : (error instanceof Error ? error.message : 'Failed to save'),
        };
      }
    }
  };
}

/**
 * Gets the stored workflow ID from local storage.
 */
export function getStoredWorkflowId(): string | null {
  try {
    return localStorage.getItem(WORKFLOW_ID_KEY);
  } catch {
    return null;
  }
}

/**
 * Sets the workflow ID in local storage.
 */
export function setStoredWorkflowId(id: string): void {
  try {
    localStorage.setItem(WORKFLOW_ID_KEY, id);
  } catch {
    // Ignore errors
  }
}

/**
 * Clears the stored workflow ID.
 */
export function clearStoredWorkflowId(): void {
  try {
    localStorage.removeItem(WORKFLOW_ID_KEY);
  } catch {
    // Ignore errors
  }
}

// ============================================================================
// Singleton Instance
// ============================================================================

let autoSaveServiceInstance: AutoSaveService | null = null;

/**
 * Gets the singleton AutoSaveService instance.
 */
export function getAutoSaveService(config?: AutoSaveServiceConfig): AutoSaveService {
  if (!autoSaveServiceInstance) {
    autoSaveServiceInstance = new AutoSaveService(config);
  }
  return autoSaveServiceInstance;
}

/**
 * Resets the singleton instance (for testing).
 */
export function resetAutoSaveService(): void {
  if (autoSaveServiceInstance) {
    autoSaveServiceInstance.reset();
    autoSaveServiceInstance = null;
  }
}

/**
 * Creates a new AutoSaveService instance (for testing).
 */
export function createAutoSaveService(config?: AutoSaveServiceConfig): AutoSaveService {
  return new AutoSaveService(config);
}
