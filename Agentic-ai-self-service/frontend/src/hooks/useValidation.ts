/**
 * Hook for debounced workflow validation.
 * Requirements: 8.5 - Validation should complete within 500ms of any change.
 */

import { useEffect, useRef, useCallback } from 'react';
import { useWorkflowStore } from '../store/workflowStore';
import { debounce, VALIDATION_DEBOUNCE_MS } from '../utils/debounce';

/**
 * Hook that automatically runs validation when workflow changes.
 * Uses debouncing to prevent excessive validation calls during rapid changes.
 *
 * Property 25: Validation Performance
 * For any configuration change, the validation engine shall complete
 * validation within 500ms.
 */
export function useValidation(): void {
  const nodes = useWorkflowStore((state) => state.nodes);
  const edges = useWorkflowStore((state) => state.edges);
  const runValidation = useWorkflowStore((state) => state.runValidation);

  // Create a stable debounced validation function
  const debouncedValidation = useRef(
    debounce(() => {
      runValidation();
    }, VALIDATION_DEBOUNCE_MS)
  );

  // Run validation when nodes or edges change
  useEffect(() => {
    debouncedValidation.current();

    // Cleanup on unmount
    return () => {
      debouncedValidation.current.cancel();
    };
  }, [nodes, edges]);
}

/**
 * Hook that provides manual validation control.
 * Returns a function to trigger validation immediately.
 */
export function useManualValidation(): {
  validate: () => void;
  validateDebounced: () => void;
} {
  const runValidation = useWorkflowStore((state) => state.runValidation);

  const debouncedValidation = useRef(
    debounce(() => {
      runValidation();
    }, VALIDATION_DEBOUNCE_MS)
  );

  const validate = useCallback(() => {
    runValidation();
  }, [runValidation]);

  const validateDebounced = useCallback(() => {
    debouncedValidation.current();
  }, []);

  return { validate, validateDebounced };
}

/**
 * Hook that returns the current validation state.
 */
export function useValidationState() {
  const validationState = useWorkflowStore((state) => state.validationState);
  const isReadyToDeploy = useWorkflowStore((state) => state.isReadyToDeploy);

  return {
    validationState,
    isReadyToDeploy,
    hasErrors: validationState ? validationState.errors.length > 0 : false,
    hasWarnings: validationState ? validationState.warnings.length > 0 : false,
    errorCount: validationState?.errors.length ?? 0,
    warningCount: validationState?.warnings.length ?? 0,
  };
}
