import { describe, it, expect } from 'vitest';
import { snapshotToCanvas } from './cloneSnapshot';
import { createEmptyDeploymentGovernance } from '../types/workflow';

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
    const clonedConfig = nodes[0].data.config as Record<string, unknown>;
    clonedConfig.name = 'mutated';
    // The nested source config, not only its parent data object, must be untouched.
    expect(RUNTIME_MEM_GATEWAY_SNAPSHOT.nodes[0].data.config.name).toBe('weatheragent');
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
    const empty = {
      nodes: [],
      edges: [],
      viewport: { x: 0, y: 0, zoom: 1 },
      governance: createEmptyDeploymentGovernance(),
    };
    expect(snapshotToCanvas(null)).toEqual(empty);
    expect(snapshotToCanvas({})).toEqual(empty);
    expect(snapshotToCanvas({ name: 'x' })).toEqual(empty);
    // non-array nodes/edges must not throw
    expect(snapshotToCanvas({ nodes: 'bad', edges: 5 } as never)).toEqual(empty);
  });

  it('is pattern-agnostic — a bare single-runtime snapshot round-trips', () => {
    const { nodes, edges } = snapshotToCanvas({
      name: 'solo', nodes: [{ id: 'r', type: 'runtime', position: { x: 0, y: 0 }, data: { config: {} } }], edges: [],
    });
    expect(nodes).toHaveLength(1);
    expect(edges).toHaveLength(0);
  });

  it('migrates a v1 snapshot to empty governance', () => {
    const { governance } = snapshotToCanvas({
      schemaVersion: 1,
      name: 'legacy',
      nodes: [],
      edges: [],
    });
    expect(governance).toEqual(createEmptyDeploymentGovernance());
  });

  it('preserves exact governance from a schema v2 snapshot', () => {
    const governance = {
      version: 1 as const,
      namingProfile: { prefix: 'ecb' },
      tags: {
        explicitValues: { owner: 'alice' },
        effectiveValues: { owner: 'alice' },
        profile: { name: 'regulated', updatedAt: '2026-09-23T12:00:00Z' },
        policyRevision: 'sha256:v7',
      },
    };

    const clone = snapshotToCanvas({
      schemaVersion: 2,
      name: 'governed',
      nodes: [],
      edges: [],
      viewport: { x: 8, y: -3, zoom: 1.4 },
      governance,
    });

    expect(clone.governance).toEqual(governance);
    expect(clone.governance).not.toBe(governance);
    expect(clone.viewport).toEqual({ x: 8, y: -3, zoom: 1.4 });
  });

  it('refuses malformed or unsupported versioned snapshots', () => {
    expect(() => snapshotToCanvas({
      schemaVersion: 2,
      name: 'missing-governance',
      nodes: [],
      edges: [],
      viewport: { x: 0, y: 0, zoom: 1 },
    })).toThrow(/requires governance/);
    expect(() => snapshotToCanvas({
      schemaVersion: 2,
      name: 'missing-viewport',
      nodes: [],
      edges: [],
      governance: createEmptyDeploymentGovernance(),
    })).toThrow(/requires a valid viewport/);
    expect(() => snapshotToCanvas({
      schemaVersion: 3,
      name: 'future',
      nodes: [],
      edges: [],
    })).toThrow(/Unsupported/);
  });
});
