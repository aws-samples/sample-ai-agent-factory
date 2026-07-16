import { describe, it, expect } from 'vitest';
import { summarizeBlueprint } from './blueprintSummary';

// The reported clone pattern: Runtime -> Memory, Runtime -> Gateway -> Weather.
const SNAPSHOT = {
  name: 'weather-agent',
  nodes: [
    { id: 'runtime-1', type: 'runtime', data: { componentType: 'runtime', label: 'Runtime', configuration: { name: 'weatheragent', modelId: 'claude-haiku' } } },
    { id: 'memory-1', type: 'memory', data: { componentType: 'memory', label: 'Memory', configuration: { strategy: 'summary' } } },
    { id: 'gateway-1', type: 'gateway', data: { componentType: 'gateway', label: 'Gateway', configuration: { name: 'wxgw' } } },
    { id: 'tool-1', type: 'tool', data: { componentType: 'tool', label: 'Weather', configuration: { toolId: 'weather_api' } } },
  ],
  edges: [
    { id: 'e1', source: 'runtime-1', target: 'memory-1' },
    { id: 'e2', source: 'runtime-1', target: 'gateway-1' },
    { id: 'e3', source: 'gateway-1', target: 'tool-1' },
  ],
};

describe('summarizeBlueprint', () => {
  it('lists every component with type, label, and a config highlight', () => {
    const { components } = summarizeBlueprint(SNAPSHOT);
    expect(components).toHaveLength(4);
    const byType = Object.fromEntries(components.map((c) => [c.type, c]));
    expect(byType.runtime.label).toBe('weatheragent');
    expect(byType.runtime.detail).toBe('claude-haiku');
    expect(byType.tool.detail).toBe('weather_api');
    expect(byType.gateway.detail).toBe('wxgw');
  });

  it('resolves wiring to human labels (all 3 edges)', () => {
    const { wiring } = summarizeBlueprint(SNAPSHOT);
    expect(wiring).toHaveLength(3);
    const pairs = wiring.map((w) => `${w.sourceLabel}->${w.targetLabel}`);
    expect(pairs).toContain('weatheragent->Memory');
    expect(pairs).toContain('wxgw->Weather');
  });

  it('is defensive against null / empty / malformed snapshots', () => {
    expect(summarizeBlueprint(null)).toEqual({ components: [], wiring: [] });
    expect(summarizeBlueprint({ name: 'x', nodes: undefined as never, edges: undefined as never }))
      .toEqual({ components: [], wiring: [] });
    // non-array must not throw
    expect(summarizeBlueprint({ nodes: 'bad', edges: 5 } as never)).toEqual({ components: [], wiring: [] });
  });

  it('falls back across shape variants (node.type, data.config, node.id)', () => {
    const { components } = summarizeBlueprint({
      name: 'legacy',
      nodes: [{ id: 'n1', type: 'gateway', data: { config: { name: 'legacy-gw' } } }],
      edges: [],
    });
    expect(components[0].type).toBe('gateway');
    expect(components[0].label).toBe('legacy-gw');
  });
});
