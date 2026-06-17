/**
 * Debounce utility for validation performance optimization.
 * Requirements: 8.5
 */

/**
 * Creates a debounced version of a function.
 * The function will only be called after the specified delay has passed
 * since the last invocation.
 *
 * @param fn - The function to debounce
 * @param delay - The delay in milliseconds
 * @returns A debounced version of the function with a cancel method
 */
export function debounce<T extends (...args: unknown[]) => void>(
  fn: T,
  delay: number
): T & { cancel: () => void } {
  let timeoutId: ReturnType<typeof setTimeout> | null = null;

  const debouncedFn = ((...args: Parameters<T>) => {
    if (timeoutId !== null) {
      clearTimeout(timeoutId);
    }
    timeoutId = setTimeout(() => {
      fn(...args);
      timeoutId = null;
    }, delay);
  }) as T & { cancel: () => void };

  debouncedFn.cancel = () => {
    if (timeoutId !== null) {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
  };

  return debouncedFn;
}

/**
 * Validation debounce delay in milliseconds.
 * Property 25: Validation Performance
 * Validation should complete within 500ms of any change.
 * We use a shorter debounce to ensure responsiveness.
 */
export const VALIDATION_DEBOUNCE_MS = 150;

/**
 * Maximum validation time in milliseconds.
 * If validation takes longer than this, it should be considered slow.
 */
export const MAX_VALIDATION_TIME_MS = 500;

/**
 * Measures the execution time of a function.
 *
 * @param fn - The function to measure
 * @returns The result of the function and the execution time in milliseconds
 */
export function measureExecutionTime<T>(fn: () => T): { result: T; timeMs: number } {
  const start = performance.now();
  const result = fn();
  const end = performance.now();
  return { result, timeMs: end - start };
}

/**
 * Creates a validation runner that measures performance.
 *
 * @param validateFn - The validation function to run
 * @param onComplete - Callback when validation completes
 * @param onSlow - Callback when validation is slow (optional)
 */
export function createValidationRunner<T>(
  validateFn: () => T,
  onComplete: (result: T, timeMs: number) => void,
  onSlow?: (timeMs: number) => void
): () => void {
  return () => {
    const { result, timeMs } = measureExecutionTime(validateFn);
    onComplete(result, timeMs);

    if (timeMs > MAX_VALIDATION_TIME_MS && onSlow) {
      onSlow(timeMs);
    }
  };
}
