import { describe, it, expect } from 'vitest';
import { snapshotToCanvas } from './cloneSnapshot';

// The exact pattern the user reported broken: Runtime -> Memory, Runtime ->
// Gateway, Gateway -> Weather tool. A registry snapshot stores the RAW canvas.
const RUNTIME_MEM_GATEWAY_SNAPSHOT = {
  name: 'weather-agent',
  nodes: [
    { id: 'runtime-1', type: 'runtime', position: { x: 0, y: 0 },
      data: { label: 'Runtime', config: { name: 'weatheragent', systemPrompt: 'hi' } } },
    { id: 'memory-1', type: 'memory', position: { x: 200, y: -80 },
      data: { label: 'Memory', config: { enabled: true } } },
    { id: 'gateway-1', type: 'gateway', position: { x: 200, y: 80 },
      data: { label: 'Gateway', config: { name: 'wxgw' } } },
    { id: 'tool-1', type: 'tool', position: { x: 400, y: 80 },
      data: { label: 'Weather', config: { toolId: 'weather_api' } } },
  ],
  edges: [
    { id: 'e-rt-mem', source: 'runtime-1', target: 'memory-1' },
    { id: 'e-rt-gw', source: 'runtime-1', target: 'gateway-1' },
    { id: 'e-gw-tool', source: 'gateway-1', target: 'tool-1' },
  ],
};

describe('snapshotToCanvas (registry clone)', () => {
  it('preserves ALL edges (the Runtime->Memory / Gateway->Weather wiring)', () => {
    const { edges } = snapshotToCanvas(RUNTIME_MEM_GATEWAY_SNAPSHOT);
    expect(edges).toHaveLength(3);
    const pairs = edges.map((e) => `${e.source}->${e.target}`);
    expect(pairs).toContain('runtime-1->memory-1');
    expect(pairs).toContain('runtime-1->gateway-1');
    expect(pairs).toContain('gateway-1->tool-1'); // the wiring the old code dropped
  });

  it('preserves every node with its config (Gateway name, tool id, memory flag)', () => {
    const { nodes } = snapshotToCanvas(RUNTIME_MEM_GATEWAY_SNAPSHOT);
    expect(nodes).toHaveLength(4);
    const byType = Object.fromEntries(nodes.map((n) => [n.type, n]));
    expect(byType.tool.data.config).toMatchObject({ toolId: 'weather_api' });
    expect(byType.gateway.data.config).toMatchObject({ name: 'wxgw' });
    expect(byType.memory.data.config).toMatchObject({ enabled: true });
    expect(byType.runtime.data.config).toMatchObject({ name: 'weatheragent' });
  });

  it('deep-clones so edits to the clone never mutate the source snapshot', () => {
    const { nodes } = snapshotToCanvas(RUNTIME_MEM_GATEWAY_SNAPSHOT);
    (nodes[0].data as Record<string, unknown>).mutated = true;
    // original snapshot node data must be untouched
    expect((RUNTIME_MEM_GATEWAY_SNAPSHOT.nodes[0].data as Record<string, unknown>).mutated).toBeUndefined();
  });

  it('clears transient selection flags', () => {
    const { nodes, edges } = snapshotToCanvas({
      name: 'x',
      nodes: [{ id: 'a', type: 'runtime', position: { x: 0, y: 0 }, data: {}, selected: true }],
      edges: [{ id: 'e', source: 'a', target: 'a', selected: true }],
    });
    expect(nodes[0].selected).toBe(false);
    expect(edges[0].selected).toBe(false);
  });

  it('is defensive against empty / legacy / malformed snapshots', () => {
    expect(snapshotToCanvas(null)).toEqual({ nodes: [], edges: [] });
    expect(snapshotToCanvas({})).toEqual({ nodes: [], edges: [] });
    expect(snapshotToCanvas({ name: 'x' })).toEqual({ nodes: [], edges: [] });
    // non-array nodes/edges must not throw
    expect(snapshotToCanvas({ nodes: 'bad', edges: 5 } as never)).toEqual({ nodes: [], edges: [] });
  });

  it('is pattern-agnostic — a bare single-runtime snapshot round-trips', () => {
    const { nodes, edges } = snapshotToCanvas({
      name: 'solo', nodes: [{ id: 'r', type: 'runtime', position: { x: 0, y: 0 }, data: { config: {} } }], edges: [],
    });
    expect(nodes).toHaveLength(1);
    expect(edges).toHaveLength(0);
  });
});
