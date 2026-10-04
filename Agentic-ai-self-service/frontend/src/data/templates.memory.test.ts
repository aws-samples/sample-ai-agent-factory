import { describe, expect, it } from 'vitest';
import { WORKFLOW_TEMPLATES } from './templates';
import type { MemoryConfiguration } from '../types/components';

// A gallery card that promises persistent memory must configure a durable extraction strategy:
// with none, memory_step creates a short-term-only AgentCore Memory (memoryStrategies: []) and
// nothing survives the session -- the promise on the card would be false.
const DURABLE = new Set(['semantic', 'summary', 'episodic', 'user_preferences']);
const PROMISE = /persistent|across sessions|long-term|remember/i;

describe('gallery memory promises are backed by a durable strategy', () => {
  const withMemory = WORKFLOW_TEMPLATES.filter((t) => t.nodes.some((n) => n.type === 'memory'));

  it('covers the templates that ship a memory node', () => {
    expect(withMemory.map((t) => t.id).sort()).toEqual(['customer-support-assistant', 'customer-support-blueprint']);
  });

  it.each(withMemory.map((t) => [t.id, t] as const))('%s: every enabled memory node has a durable strategy', (_id, t) => {
    for (const node of t.nodes.filter((n) => n.type === 'memory')) {
      const cfg = node.configuration as MemoryConfiguration;
      if (!cfg.enabled) continue;
      expect(cfg.strategies?.length ?? 0).toBeGreaterThan(0);
      for (const s of cfg.strategies ?? []) {
        expect(DURABLE.has(s.type)).toBe(true);
        expect(s.name).toMatch(/^[a-zA-Z][a-zA-Z0-9_]{0,47}$/); // AgentCore strategy name rule
      }
    }
  });

  it.each(WORKFLOW_TEMPLATES.map((t) => [t.id, t] as const))('%s: a persistence promise implies a memory node with a strategy', (_id, t) => {
    const promises = [t.description, t.longDescription, ...t.builtInTools.map((b) => `${b.name} ${b.description}`)].filter((x) => PROMISE.test(x ?? ''));
    if (promises.length === 0) return;
    const memory = t.nodes.filter((n) => n.type === 'memory').map((n) => n.configuration as MemoryConfiguration);
    expect(memory.length, `${t.id} promises memory: ${promises.join(' | ')}`).toBeGreaterThan(0);
    expect(memory.every((m) => (m.strategies?.length ?? 0) > 0)).toBe(true);
  });
});
