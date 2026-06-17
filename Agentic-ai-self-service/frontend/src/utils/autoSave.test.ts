/**
 * Property-based tests for auto-save functionality.
 * **Property 28: Auto-Save Timing**
 * **Property 29: Save Error Retry**
 * **Validates: Requirements 9.1, 9.4**
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as fc from 'fast-check';
import {
  createAutoSaveService,
  AUTO_SAVE_DELAY_MS,
  MAX_RETRY_ATTEMPTS,
  type SaveFunction,
} from './autoSave';
import type { AgentCoreNode } from '../store/workflowStore';
import type { AgentCoreComponentType } from '../types/workflow';
import type { Edge, Viewport } from '@xyflow/react';

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

const componentTypeArb = fc.constantFrom(
  'runtime',
  'gateway',
  'memory',
  'code_interpreter',
  'browser',
  'observability',
  'identity'
) as fc.Arbitrary<AgentCoreComponentType>;

const validationStatusArb = fc.constantFrom('valid', 'warning', 'error', 'pending') as fc.Arbitrary<
  'valid' | 'warning' | 'error' | 'pending'
>;

const positionArb = fc.record({
  x: fc.float({ min: Math.fround(0), max: Math.fround(2000), noNaN: true }),
  y: fc.float({ min: Math.fround(0), max: Math.fround(2000), noNaN: true }),
});

const viewportArb: fc.Arbitrary<Viewport> = fc.record({
  x: fc.float({ min: Math.fround(-1000), max: Math.fround(1000), noNaN: true }),
  y: fc.float({ min: Math.fround(-1000), max: Math.fround(1000), noNaN: true }),
  zoom: fc.float({ min: Math.fround(0.1), max: Math.fround(4), noNaN: true }),
});

const nodeDataArb = fc.record({
  label: fc.string({ minLength: 1, maxLength: 50 }),
  componentType: componentTypeArb,
  validationStatus: validationStatusArb,
});

const agentCoreNodeArb: fc.Arbitrary<AgentCoreNode> = fc.record({
  id: fc.uuid(),
  type: componentTypeArb,
  position: positionArb,
  data: nodeDataArb,
  selected: fc.boolean(),
});

const edgeArb: fc.Arbitrary<Edge> = fc.record({
  id: fc.uuid(),
  source: fc.uuid(),
  target: fc.uuid(),
  sourceHandle: fc.option(fc.string({ minLength: 1, maxLength: 20 }), { nil: null }),
  targetHandle: fc.option(fc.string({ minLength: 1, maxLength: 20 }), { nil: null }),
  type: fc.option(fc.constantFrom('data', 'authentication', 'policy'), { nil: undefined }),
  animated: fc.boolean(),
  data: fc.option(fc.record({ label: fc.string() }), { nil: undefined }),
  selected: fc.boolean(),
});

// ============================================================================
// Test Setup
// ============================================================================

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

// ============================================================================
// Property 28: Auto-Save Timing
// ============================================================================

describe('Property 28: Auto-Save Timing', () => {
  /**
   * **Validates: Requirements 9.1**
   *
   * For any workflow change, the auto-save shall trigger within 5 seconds of the change.
   */
  it('auto-save triggers within the configured delay', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.array(agentCoreNodeArb, { minLength: 0, maxLength: 5 }),
        fc.array(edgeArb, { minLength: 0, maxLength: 5 }),
        viewportArb,
        async (nodes, edges, viewport) => {
          const saveFn = vi.fn<SaveFunction>().mockResolvedValue({
            success: true,
            timestamp: new Date(),
          });

          const service = createAutoSaveService({
            saveDelay: AUTO_SAVE_DELAY_MS,
            saveFn,
          });

          // Schedule auto-save
          service.scheduleAutoSave(nodes, edges, viewport);

          // Status should be pending
          expect(service.getSaveStatus()).toBe('pending');

          // Save should not have been called yet
          expect(saveFn).not.toHaveBeenCalled();

          // Advance time to just before the delay
          await vi.advanceTimersByTimeAsync(AUTO_SAVE_DELAY_MS - 100);
          expect(saveFn).not.toHaveBeenCalled();

          // Advance time past the delay
          await vi.advanceTimersByTimeAsync(200);
          expect(saveFn).toHaveBeenCalledTimes(1);

          // Status should be saved
          expect(service.getSaveStatus()).toBe('saved');

          service.reset();
        }
      ),
      { numRuns: 20 }
    );
  });

  it('multiple rapid changes only trigger one save', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.array(agentCoreNodeArb, { minLength: 1, maxLength: 3 }),
        viewportArb,
        fc.integer({ min: 2, max: 5 }),
        async (nodes, viewport, changeCount) => {
          const saveFn = vi.fn<SaveFunction>().mockResolvedValue({
            success: true,
            timestamp: new Date(),
          });

          const service = createAutoSaveService({
            saveDelay: AUTO_SAVE_DELAY_MS,
            saveFn,
          });

          // Make multiple rapid changes
          for (let i = 0; i < changeCount; i++) {
            const modifiedNodes = nodes.map((n) => ({
              ...n,
              position: { x: n.position.x + i, y: n.position.y },
            }));
            service.scheduleAutoSave(modifiedNodes, [], viewport);
            await vi.advanceTimersByTimeAsync(100); // Small delay between changes
          }

          // Save should not have been called yet (debounced)
          expect(saveFn).not.toHaveBeenCalled();

          // Advance time past the delay
          await vi.advanceTimersByTimeAsync(AUTO_SAVE_DELAY_MS);

          // Only one save should have been triggered
          expect(saveFn).toHaveBeenCalledTimes(1);

          service.reset();
        }
      ),
      { numRuns: 20 }
    );
  });

  it('save status updates correctly through lifecycle', async () => {
    const saveFn = vi.fn<SaveFunction>().mockResolvedValue({
      success: true,
      timestamp: new Date(),
    });

    const statusChanges: string[] = [];
    const service = createAutoSaveService({
      saveDelay: 100,
      saveFn,
      onStatusChange: (state) => statusChanges.push(state.status),
    });

    // Schedule auto-save
    service.scheduleAutoSave([], [], { x: 0, y: 0, zoom: 1 });

    // Should have pending status
    expect(statusChanges).toContain('pending');

    // Advance time to trigger save
    await vi.advanceTimersByTimeAsync(150);

    // Should have saving and saved statuses
    expect(statusChanges).toContain('saving');
    expect(statusChanges).toContain('saved');

    service.reset();
  });
});

// ============================================================================
// Property 29: Save Error Retry
// ============================================================================

describe('Property 29: Save Error Retry', () => {
  /**
   * **Validates: Requirements 9.4**
   *
   * For any failed auto-save operation, the system shall display an error notification and retry.
   */
  it('retries on save failure', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.array(agentCoreNodeArb, { minLength: 0, maxLength: 3 }),
        viewportArb,
        fc.integer({ min: 1, max: MAX_RETRY_ATTEMPTS }),
        async (nodes, viewport, failCount) => {
          let callCount = 0;
          const saveFn = vi.fn<SaveFunction>().mockImplementation(async () => {
            callCount++;
            if (callCount <= failCount) {
              return { success: false, timestamp: new Date(), error: 'Network error' };
            }
            return { success: true, timestamp: new Date() };
          });

          const service = createAutoSaveService({
            saveDelay: 100,
            retryDelay: 50,
            maxRetries: MAX_RETRY_ATTEMPTS,
            saveFn,
          });

          // Schedule auto-save
          service.scheduleAutoSave(nodes, [], viewport);

          // Advance time to trigger initial save
          await vi.advanceTimersByTimeAsync(150);

          // Advance time for retries
          for (let i = 0; i < failCount; i++) {
            await vi.advanceTimersByTimeAsync(100);
          }

          // Should have retried the correct number of times
          expect(saveFn).toHaveBeenCalledTimes(failCount + 1);

          // Final status should be saved (since we succeed after failCount attempts)
          expect(service.getSaveStatus()).toBe('saved');

          service.reset();
        }
      ),
      { numRuns: 10 }
    );
  });

  it('stops retrying after max attempts', async () => {
    const saveFn = vi.fn<SaveFunction>().mockResolvedValue({
      success: false,
      timestamp: new Date(),
      error: 'Persistent error',
    });

    const service = createAutoSaveService({
      saveDelay: 100,
      retryDelay: 50,
      maxRetries: MAX_RETRY_ATTEMPTS,
      saveFn,
    });

    // Schedule auto-save
    service.scheduleAutoSave([], [], { x: 0, y: 0, zoom: 1 });

    // Advance time to trigger initial save and all retries
    await vi.advanceTimersByTimeAsync(100); // Initial delay
    for (let i = 0; i < MAX_RETRY_ATTEMPTS; i++) {
      await vi.advanceTimersByTimeAsync(100); // Retry delays
    }

    // Should have called save 1 + MAX_RETRY_ATTEMPTS times
    expect(saveFn).toHaveBeenCalledTimes(1 + MAX_RETRY_ATTEMPTS);

    // Status should be error
    expect(service.getSaveStatus()).toBe('error');

    // Error message should indicate max retries exceeded
    const state = service.getState();
    expect(state.error).toContain(`${MAX_RETRY_ATTEMPTS}`);

    service.reset();
  });

  it('error state includes error message', async () => {
    const errorMessage = 'Storage quota exceeded';
    const saveFn = vi.fn<SaveFunction>().mockResolvedValue({
      success: false,
      timestamp: new Date(),
      error: errorMessage,
    });

    const service = createAutoSaveService({
      saveDelay: 100,
      retryDelay: 50,
      maxRetries: 1,
      saveFn,
    });

    // Schedule auto-save
    service.scheduleAutoSave([], [], { x: 0, y: 0, zoom: 1 });

    // Advance time to trigger save and retry
    await vi.advanceTimersByTimeAsync(300);

    // Error state should contain the error message
    const state = service.getState();
    expect(state.status).toBe('error');
    expect(state.error).toContain(errorMessage);

    service.reset();
  });
});

// ============================================================================
// Additional Tests
// ============================================================================

describe('AutoSaveService', () => {
  it('cancelPendingAutoSave prevents save', async () => {
    const saveFn = vi.fn<SaveFunction>().mockResolvedValue({
      success: true,
      timestamp: new Date(),
    });

    const service = createAutoSaveService({
      saveDelay: 100,
      saveFn,
    });

    // Schedule auto-save
    service.scheduleAutoSave([], [], { x: 0, y: 0, zoom: 1 });

    // Cancel before it triggers
    service.cancelPendingAutoSave();

    // Advance time past the delay
    await vi.advanceTimersByTimeAsync(200);

    // Save should not have been called
    expect(saveFn).not.toHaveBeenCalled();

    service.reset();
  });

  it('saveNow triggers immediate save', async () => {
    const saveFn = vi.fn<SaveFunction>().mockResolvedValue({
      success: true,
      timestamp: new Date(),
    });

    const service = createAutoSaveService({
      saveDelay: 5000,
      saveFn,
    });

    // Save immediately
    const result = await service.saveNow([], [], { x: 0, y: 0, zoom: 1 });

    // Save should have been called immediately
    expect(saveFn).toHaveBeenCalledTimes(1);
    expect(result.success).toBe(true);

    service.reset();
  });

  it('getLastSaveTime returns correct time after successful save', async () => {
    const saveTime = new Date('2024-01-15T10:30:00Z');
    const saveFn = vi.fn<SaveFunction>().mockResolvedValue({
      success: true,
      timestamp: saveTime,
    });

    const service = createAutoSaveService({
      saveDelay: 100,
      saveFn,
    });

    // Initially null
    expect(service.getLastSaveTime()).toBeNull();

    // Schedule and wait for save
    service.scheduleAutoSave([], [], { x: 0, y: 0, zoom: 1 });
    await vi.advanceTimersByTimeAsync(150);

    // Should have the save time
    expect(service.getLastSaveTime()).toEqual(saveTime);

    service.reset();
  });

  it('reset clears all state', async () => {
    const saveFn = vi.fn<SaveFunction>().mockResolvedValue({
      success: true,
      timestamp: new Date(),
    });

    const service = createAutoSaveService({
      saveDelay: 100,
      saveFn,
    });

    // Schedule auto-save
    service.scheduleAutoSave([], [], { x: 0, y: 0, zoom: 1 });
    await vi.advanceTimersByTimeAsync(150);

    // Reset
    service.reset();

    // State should be cleared
    expect(service.getSaveStatus()).toBe('saved');
    expect(service.getLastSaveTime()).toBeNull();
    expect(service.getState().error).toBeNull();
    expect(service.getState().retryCount).toBe(0);
  });
});
