/**
 * Hook for auto-saving the active flow workflow at a debounced interval.
 * Requirements: 6.1, 6.2, 6.3
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useWorkflowStore } from '../store/workflowStore';
import type { AgentCoreNode } from '../store/workflowStore';
import { useFlowStore } from '../store/flowStore';
import type { Edge } from '@xyflow/react';

/**
 * Return value of {@link useAutoSave}.
 *
 * Audit issue #8: previously the hook returned `void` and silently swallowed
 * save errors (only flowStore.error got set, which is overwritten by every
 * other flow operation). Consumers can now read `lastSaveError` to render
 * an autosave-specific banner/toast, and call `clearLastSaveError()` to
 * dismiss it.
 */
export interface UseAutoSaveResult {
  lastSaveError: Error | null;
  clearLastSaveError: () => void;
}

const DEFAULT_INTERVAL = 5000;

/**
 * Converts a camelCase key to snake_case.
 */
function toSnakeCase(str: string): string {
  return str.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`);
}

/**
 * Recursively converts all object keys from camelCase to snake_case.
 */
function keysToSnakeCase(obj: unknown): unknown {
  if (Array.isArray(obj)) {
    return obj.map(keysToSnakeCase);
  }
  if (obj !== null && typeof obj === 'object') {
    const result: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
      result[toSnakeCase(key)] = keysToSnakeCase(value);
    }
    return result;
  }
  return obj;
}

/**
 * Converts React Flow nodes to the backend ComponentNode format.
 * Only includes nodes that have a configuration set.
 * Converts all keys to snake_case to match backend Pydantic models.
 */
function toBackendNodes(nodes: AgentCoreNode[]): unknown[] {
  return nodes
    .filter((node) => node.data?.configuration)
    .map((node) => {
      const config = keysToSnakeCase(node.data.configuration) as Record<string, unknown>;
      // Ensure component_type is set (discriminator field)
      if (!config.component_type) {
        config.component_type = node.data.componentType;
      }
      return {
        id: node.id,
        type: node.data.componentType,
        position: { x: node.position?.x ?? 0, y: node.position?.y ?? 0 },
        data: config,
        selected: node.selected ?? false,
        validation_status: node.data?.validationStatus ?? 'pending',
      };
    });
}

/**
 * Maps frontend connection types to backend ConnectionType enum values.
 */
const CONNECTION_TYPE_MAP: Record<string, string> = {
  data: 'data',
  identity: 'authentication',
  tool: 'data',
  authentication: 'authentication',
  policy: 'policy',
};

/**
 * Converts React Flow edges to the backend ConnectionEdge format.
 * Backend requires: source_handle (non-empty string), target_handle (non-empty string), type (ConnectionType)
 */
function toBackendEdges(edges: Edge[]): unknown[] {
  return edges.map((edge) => {
    const edgeData = edge.data as Record<string, unknown> | undefined;
    const frontendType = (edgeData?.connectionType as string) || 'data';
    const backendType = CONNECTION_TYPE_MAP[frontendType] || 'data';

    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      source_handle: edge.sourceHandle || 'output',
      target_handle: edge.targetHandle || 'input',
      type: backendType,
      animated: false,
    };
  });
}

/**
 * Subscribes directly to workflowStore changes and debounces saves
 * to flowStore.saveFlow() when an active flow is set.
 * Converts React Flow nodes/edges to backend-compatible format with snake_case keys.
 *
 * Returns {@link UseAutoSaveResult} so callers can render autosave-specific
 * error UI. Backwards-compatible: existing callers that ignore the return
 * value still work.
 */
export function useAutoSave(
  flowId: string | null,
  interval: number = DEFAULT_INTERVAL,
): UseAutoSaveResult {
  const flowIdRef = useRef(flowId);
  flowIdRef.current = flowId;

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hasReceivedFirstState = useRef(false);

  const [lastSaveError, setLastSaveError] = useState<Error | null>(null);
  const clearLastSaveError = useCallback(() => setLastSaveError(null), []);

  useEffect(() => {
    const unsubscribe = useWorkflowStore.subscribe(
      (state, prevState) => {
        if (!hasReceivedFirstState.current) {
          hasReceivedFirstState.current = true;
          return;
        }

        if (!flowIdRef.current) return;

        if (
          state.nodes === prevState.nodes &&
          state.edges === prevState.edges &&
          state.viewport === prevState.viewport
        ) {
          return;
        }

        if (timerRef.current !== null) {
          clearTimeout(timerRef.current);
        }

        timerRef.current = setTimeout(() => {
          const currentFlowId = flowIdRef.current;
          if (!currentFlowId) return;

          // Guard: skip save if the flow was deleted or is no longer active
          const { activeFlowId, flows } = useFlowStore.getState();
          if (!activeFlowId || activeFlowId !== currentFlowId) return;
          if (!flows.some(f => f.id === currentFlowId)) return;

          const workflowState = useWorkflowStore.getState();
          const { saveFlow } = useFlowStore.getState();

          const now = new Date().toISOString();
          const workflow = {
            id: currentFlowId,
            name: 'auto-save',
            description: '',
            version: '1.0.0',
            nodes: toBackendNodes(workflowState.nodes),
            edges: toBackendEdges(workflowState.edges),
            viewport: {
              x: workflowState.viewport.x,
              y: workflowState.viewport.y,
              zoom: workflowState.viewport.zoom,
            },
            metadata: {
              author: 'system',
              tags: [],
              aws_region: 'us-east-1',
              deployment_status: 'not_deployed',
            },
            created_at: now,
            updated_at: now,
          };

          saveFlow(currentFlowId, workflow as never)
            .then(() => {
              // Successful save: clear any previously surfaced auto-save error
              // so a transient network blip doesn't leave a stale banner.
              setLastSaveError((prev) => (prev === null ? prev : null));
            })
            .catch((err: unknown) => {
              // Audit issue #8: surface auto-save failures to the consumer
              // so it can render a dedicated toast/banner. flowStore.error
              // is shared with every other flow operation and gets clobbered;
              // this state is autosave-specific.
              const error = err instanceof Error ? err : new Error(String(err));
              // Log so the failure is also visible in dev tools / observability.
              // eslint-disable-next-line no-console
              console.error('[useAutoSave] flow save failed', error);
              setLastSaveError(error);
            });
        }, interval);
      }
    );

    return () => {
      unsubscribe();
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [interval]);

  return { lastSaveError, clearLastSaveError };
}
