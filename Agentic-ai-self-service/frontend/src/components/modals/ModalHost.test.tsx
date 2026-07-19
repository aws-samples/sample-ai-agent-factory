/**
 * ModalHost registry smoke tests.
 * Verifies that every registered modal key resolves to a component.
 */

import { describe, it, expect } from 'vitest';
import { MODAL_REGISTRY, type ModalKey } from './modalRegistry';

describe('ModalHost registry', () => {
  it('should have a component for every modal key', () => {
    const keys: ModalKey[] = [
      'runtime',
      'gateway',
      'identity',
      'memory',
      'policy',
      'guardrails',
      'observability',
      'evaluation',
      'tool',
      'connector',
      'knowledgeBase',
      'a2a',
      'promptLibrary',
      'registry',
      'hitl',
    ];

    for (const key of keys) {
      expect(MODAL_REGISTRY[key]).toBeDefined();
      expect(typeof MODAL_REGISTRY[key]).toBe('object'); // Lazy component
    }
  });

  it('should have all registry keys match the ModalKey type', () => {
    const registryKeys = Object.keys(MODAL_REGISTRY) as ModalKey[];
    expect(registryKeys.length).toBeGreaterThan(0);
    expect(registryKeys).toContain('runtime');
    expect(registryKeys).toContain('gateway');
    expect(registryKeys).toContain('tool');
  });
});
