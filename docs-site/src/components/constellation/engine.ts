/**
 * Constellation engine: the pure simulation behind the Home hero backdrop.
 *
 * Nodes drift slowly inside a rectangle and bounce softly at its edges; edges join nodes that
 * are closer than a distance threshold, fading with distance; every few seconds one "pulse"
 * travels along a random edge like a tool call between two agents. Nothing here touches the
 * DOM or a canvas, so the maths is unit-testable and deterministic given a seed.
 *
 * Units: CSS pixels and seconds. `step()` advances by a delta in seconds.
 */

export const MIN_NODES = 24;
export const MAX_NODES = 110;
/** Square CSS pixels per node before clamping. */
export const AREA_PER_NODE = 22_000;
/** Multiplier for constrained devices (few cores) and narrow viewports. */
export const LOW_POWER_FACTOR = 0.6;

/** Node drift speed range at 1x, in CSS pixels per second. */
export const SPEED_MIN = 6;
export const SPEED_MAX = 10;
export const RADIUS_MIN = 1.5;
export const RADIUS_MAX = 3;
export const NODE_ALPHA_MIN = 0.35;
export const NODE_ALPHA_MAX = 0.6;
/** Radius of the faint glow drawn under every node. */
export const GLOW_RADIUS = 8;
/** Peak alpha of an edge (two nodes touching). */
export const EDGE_ALPHA_MAX = 0.22;
/** Pulses are spawned this many seconds apart. */
export const PULSE_GAP_MIN = 2;
export const PULSE_GAP_MAX = 4;
/** Seconds a pulse takes to cross its edge. */
export const PULSE_DURATION = 0.9;
/** Fraction of the edge trailing behind the pulse head. */
export const PULSE_TAIL = 0.18;
/** Everything drawn inside the copy region is dimmed by this factor so the hero text stays readable. */
export const COPY_DIM = 0.25;
/** Number of stage colours (learn, build, govern, scale). */
export const STAGE_COUNT = 4;

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Node {
  x: number;
  y: number;
  /** CSS px per second. */
  vx: number;
  vy: number;
  radius: number;
  /** Index into the stage palette, 0..STAGE_COUNT-1. */
  stage: number;
  alpha: number;
}

export interface Edge {
  a: number;
  b: number;
  /** 0..EDGE_ALPHA_MAX, fading with distance. */
  alpha: number;
}

export interface Pulse {
  /** Source node: the pulse takes its colour. */
  from: number;
  to: number;
  /** Progress along the edge, 0..1. */
  progress: number;
}

export interface EngineState {
  width: number;
  height: number;
  nodes: Node[];
  /** Nodes closer than this are joined by an edge. */
  linkDistance: number;
  pulse: Pulse | null;
  /** Simulation clock in seconds. */
  time: number;
  /** Clock value at which the next pulse is spawned. */
  nextPulseAt: number;
  rng: Rng;
}

/** Deterministic 32-bit PRNG (mulberry32). `next()` returns a float in [0, 1). */
export interface Rng {
  next(): number;
}

export function createRng(seed: number): Rng {
  let state = seed >>> 0;
  return {
    next() {
      state = (state + 0x6d2b79f5) >>> 0;
      let t = state;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    },
  };
}

function between(rng: Rng, min: number, max: number): number {
  return min + (max - min) * rng.next();
}

/** How many nodes a band of `area` square CSS pixels gets; `lowPower` scales it down. */
export function nodeCountFor(area: number, lowPower = false): number {
  const base = Math.min(MAX_NODES, Math.max(MIN_NODES, Math.round(area / AREA_PER_NODE)));
  return lowPower ? Math.round(base * LOW_POWER_FACTOR) : base;
}

/** Edge threshold: grows with the spacing between nodes so density looks similar at any size. */
export function linkDistanceFor(width: number, height: number, nodeCount: number): number {
  const spacing = Math.sqrt((width * height) / Math.max(1, nodeCount));
  return Math.min(170, Math.max(80, spacing * 1.15));
}

function randomNode(rng: Rng, width: number, height: number): Node {
  const speed = between(rng, SPEED_MIN, SPEED_MAX);
  const angle = between(rng, 0, Math.PI * 2);
  return {
    x: between(rng, 0, width),
    y: between(rng, 0, height),
    vx: Math.cos(angle) * speed,
    vy: Math.sin(angle) * speed,
    radius: between(rng, RADIUS_MIN, RADIUS_MAX),
    stage: Math.floor(rng.next() * STAGE_COUNT),
    alpha: between(rng, NODE_ALPHA_MIN, NODE_ALPHA_MAX),
  };
}

export interface CreateOptions {
  width: number;
  height: number;
  nodeCount: number;
  seed: number;
}

export function createEngine({ width, height, nodeCount, seed }: CreateOptions): EngineState {
  const rng = createRng(seed);
  const nodes: Node[] = [];
  for (let i = 0; i < nodeCount; i += 1) nodes.push(randomNode(rng, width, height));
  return {
    width,
    height,
    nodes,
    linkDistance: linkDistanceFor(width, height, nodeCount),
    pulse: null,
    time: 0,
    nextPulseAt: between(rng, PULSE_GAP_MIN, PULSE_GAP_MAX),
    rng,
  };
}

/** Every pair of nodes closer than the link distance, with alpha fading linearly by distance. */
export function edgesOf(state: EngineState): Edge[] {
  const { nodes, linkDistance } = state;
  const edges: Edge[] = [];
  for (let a = 0; a < nodes.length; a += 1) {
    for (let b = a + 1; b < nodes.length; b += 1) {
      const dx = nodes[a].x - nodes[b].x;
      const dy = nodes[a].y - nodes[b].y;
      const distance = Math.hypot(dx, dy);
      if (distance < linkDistance) {
        edges.push({ a, b, alpha: EDGE_ALPHA_MAX * (1 - distance / linkDistance) });
      }
    }
  }
  return edges;
}

/** Soft bounce: reflect the velocity component and keep the node inside the band. */
function bounce(node: Node, width: number, height: number): void {
  if (node.x < 0) {
    node.x = -node.x;
    node.vx = Math.abs(node.vx);
  } else if (node.x > width) {
    node.x = 2 * width - node.x;
    node.vx = -Math.abs(node.vx);
  }
  if (node.y < 0) {
    node.y = -node.y;
    node.vy = Math.abs(node.vy);
  } else if (node.y > height) {
    node.y = 2 * height - node.y;
    node.vy = -Math.abs(node.vy);
  }
  // Degenerate sizes (a zero-height band) would otherwise leave nodes outside.
  node.x = Math.min(width, Math.max(0, node.x));
  node.y = Math.min(height, Math.max(0, node.y));
}

/**
 * Advance the simulation by `dt` seconds: drift nodes, bounce at bounds, move or finish the current
 * pulse and spawn a new one on a random existing edge when its timer is due. Large deltas (a tab
 * coming back) are clamped so nodes never jump.
 */
export function step(state: EngineState, dt: number): void {
  const delta = Math.min(Math.max(dt, 0), 0.1);
  const { width, height } = state;
  for (const node of state.nodes) {
    node.x += node.vx * delta;
    node.y += node.vy * delta;
    bounce(node, width, height);
  }
  state.time += delta;

  if (state.pulse) {
    state.pulse.progress += delta / PULSE_DURATION;
    if (state.pulse.progress >= 1) state.pulse = null;
  }

  if (!state.pulse && state.time >= state.nextPulseAt) {
    const edges = edgesOf(state);
    if (edges.length > 0) {
      const edge = edges[Math.floor(state.rng.next() * edges.length)];
      const forward = state.rng.next() < 0.5;
      state.pulse = { from: forward ? edge.a : edge.b, to: forward ? edge.b : edge.a, progress: 0 };
    }
    state.nextPulseAt = state.time + between(state.rng, PULSE_GAP_MIN, PULSE_GAP_MAX);
  }
}

/**
 * Fit the simulation to a new size: positions scale proportionally, so nothing jumps, and the node
 * count follows the new area (extra nodes are dropped, missing ones seeded from the same PRNG).
 */
export function resize(state: EngineState, width: number, height: number, nodeCount: number): void {
  const sx = state.width > 0 ? width / state.width : 1;
  const sy = state.height > 0 ? height / state.height : 1;
  for (const node of state.nodes) {
    node.x *= sx;
    node.y *= sy;
  }
  if (state.nodes.length > nodeCount) state.nodes.length = nodeCount;
  while (state.nodes.length < nodeCount) state.nodes.push(randomNode(state.rng, width, height));
  state.width = width;
  state.height = height;
  state.linkDistance = linkDistanceFor(width, height, nodeCount);
  if (state.pulse && (state.pulse.from >= nodeCount || state.pulse.to >= nodeCount)) state.pulse = null;
}

/** True when the point lies inside the (optional) copy region. */
export function inRect(rect: Rect | null, x: number, y: number): boolean {
  return rect !== null && x >= rect.x && x <= rect.x + rect.width && y >= rect.y && y <= rect.y + rect.height;
}
