import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  COPY_DIM,
  EDGE_ALPHA_MAX,
  MAX_NODES,
  MIN_NODES,
  PULSE_DURATION,
  PULSE_GAP_MAX,
  createEngine,
  createRng,
  edgesOf,
  nodeCountFor,
  resize,
  step,
  type EngineState,
} from './constellation/engine';
import { drawFrame, type Canvas2D } from './constellation/render';

const PALETTE = { stages: ['#FDBA74', '#86EFAC', '#C4B5FD', '#93C5FD'] };

function stubContext() {
  return {
    fillStyle: '' as string,
    strokeStyle: '' as string,
    lineWidth: 1,
    lineCap: 'butt' as CanvasLineCap,
    globalAlpha: 1,
    setTransform: vi.fn(),
    clearRect: vi.fn(),
    beginPath: vi.fn(),
    arc: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
  } satisfies Canvas2D;
}

/** Advance the simulation in 1/30 s ticks until `until` returns true or `maxSeconds` pass. */
function stepUntil(state: EngineState, until: () => boolean, maxSeconds: number): number {
  const tick = 1 / 30;
  let elapsed = 0;
  while (!until() && elapsed < maxSeconds) {
    step(state, tick);
    elapsed += tick;
  }
  return elapsed;
}

describe('constellation engine', () => {
  it('scales the node count with area and clamps it to [24, 110]', () => {
    expect(nodeCountFor(0)).toBe(MIN_NODES);
    expect(nodeCountFor(100 * 100)).toBe(MIN_NODES);
    expect(nodeCountFor(1440 * 600)).toBe(Math.round((1440 * 600) / 22_000));
    expect(nodeCountFor(4000 * 4000)).toBe(MAX_NODES);
  });

  it('multiplies the count by 0.6 for constrained devices', () => {
    expect(nodeCountFor(1_100_000, true)).toBe(Math.round(50 * 0.6));
    expect(nodeCountFor(4000 * 4000, true)).toBe(Math.round(MAX_NODES * 0.6));
  });

  it('is deterministic for a seed and different across seeds', () => {
    const a = createEngine({ width: 800, height: 400, nodeCount: 30, seed: 7 });
    const b = createEngine({ width: 800, height: 400, nodeCount: 30, seed: 7 });
    const c = createEngine({ width: 800, height: 400, nodeCount: 30, seed: 8 });
    expect(a.nodes).toEqual(b.nodes);
    expect(a.nodes).not.toEqual(c.nodes);
    for (let i = 0; i < 90; i += 1) {
      step(a, 1 / 30);
      step(b, 1 / 30);
    }
    expect(a.nodes).toEqual(b.nodes);
    expect(a.pulse).toEqual(b.pulse);
    const rng = createRng(42);
    const first = [rng.next(), rng.next(), rng.next()];
    const again = createRng(42);
    expect([again.next(), again.next(), again.next()]).toEqual(first);
    first.forEach((value) => expect(value).toBeGreaterThanOrEqual(0));
    first.forEach((value) => expect(value).toBeLessThan(1));
  });

  it('gives every node a slow drift, a small radius and a stage colour index', () => {
    const state = createEngine({ width: 1200, height: 320, nodeCount: 40, seed: 3 });
    for (const node of state.nodes) {
      const speed = Math.hypot(node.vx, node.vy);
      expect(speed).toBeGreaterThanOrEqual(6);
      expect(speed).toBeLessThanOrEqual(10);
      expect(node.radius).toBeGreaterThanOrEqual(1.5);
      expect(node.radius).toBeLessThanOrEqual(3);
      expect(node.alpha).toBeGreaterThanOrEqual(0.35);
      expect(node.alpha).toBeLessThanOrEqual(0.6);
      expect([0, 1, 2, 3]).toContain(node.stage);
    }
  });

  it('draws edges only between nodes closer than the link distance, fading with distance', () => {
    const state = createEngine({ width: 600, height: 300, nodeCount: 40, seed: 11 });
    const edges = edgesOf(state);
    expect(edges.length).toBeGreaterThan(0);
    let expected = 0;
    for (let a = 0; a < state.nodes.length; a += 1) {
      for (let b = a + 1; b < state.nodes.length; b += 1) {
        const d = Math.hypot(state.nodes[a].x - state.nodes[b].x, state.nodes[a].y - state.nodes[b].y);
        if (d < state.linkDistance) expected += 1;
      }
    }
    expect(edges).toHaveLength(expected);
    for (const edge of edges) {
      const d = Math.hypot(state.nodes[edge.a].x - state.nodes[edge.b].x, state.nodes[edge.a].y - state.nodes[edge.b].y);
      expect(d).toBeLessThan(state.linkDistance);
      expect(edge.alpha).toBeGreaterThan(0);
      expect(edge.alpha).toBeLessThanOrEqual(EDGE_ALPHA_MAX);
      expect(edge.alpha).toBeCloseTo(EDGE_ALPHA_MAX * (1 - d / state.linkDistance), 6);
    }
  });

  it('keeps nodes inside the bounds while they drift and bounce', () => {
    const state = createEngine({ width: 200, height: 50, nodeCount: 30, seed: 5 });
    for (let i = 0; i < 30 * 60; i += 1) step(state, 1 / 30);
    for (const node of state.nodes) {
      expect(node.x).toBeGreaterThanOrEqual(0);
      expect(node.x).toBeLessThanOrEqual(200);
      expect(node.y).toBeGreaterThanOrEqual(0);
      expect(node.y).toBeLessThanOrEqual(50);
    }
  });

  it('spawns a pulse on an existing edge every 2 to 4 seconds and retires it after it crosses', () => {
    const state = createEngine({ width: 400, height: 200, nodeCount: 40, seed: 9 });
    expect(state.pulse).toBeNull();
    const spawnedAt = stepUntil(state, () => state.pulse !== null, PULSE_GAP_MAX + 1);
    expect(spawnedAt).toBeGreaterThanOrEqual(2 - 1 / 30);
    expect(spawnedAt).toBeLessThanOrEqual(PULSE_GAP_MAX + 1 / 30);
    const pulse = state.pulse;
    if (!pulse) throw new Error('expected a pulse');
    const from = state.nodes[pulse.from];
    const to = state.nodes[pulse.to];
    expect(Math.hypot(from.x - to.x, from.y - to.y)).toBeLessThan(state.linkDistance);
    const progressBefore = pulse.progress;
    expect(progressBefore).toBeGreaterThanOrEqual(0);
    step(state, 1 / 30);
    expect(state.pulse?.progress ?? 0).toBeGreaterThan(progressBefore);
    const retiredAfter = stepUntil(state, () => state.pulse === null, PULSE_DURATION + 1);
    expect(retiredAfter).toBeLessThanOrEqual(PULSE_DURATION + 2 / 30);
    const nextAfter = stepUntil(state, () => state.pulse !== null, PULSE_GAP_MAX + 1);
    expect(nextAfter).toBeLessThanOrEqual(PULSE_GAP_MAX + 1 / 30);
  });

  it('clamps large time deltas so nodes never jump', () => {
    const state = createEngine({ width: 800, height: 400, nodeCount: 24, seed: 2 });
    const before = state.nodes.map((node) => ({ x: node.x, y: node.y }));
    step(state, 5);
    state.nodes.forEach((node, index) => {
      expect(Math.hypot(node.x - before[index].x, node.y - before[index].y)).toBeLessThanOrEqual(1.01);
    });
  });

  it('resizes by scaling positions proportionally and adjusting the node count', () => {
    const state = createEngine({ width: 800, height: 400, nodeCount: 30, seed: 4 });
    const before = state.nodes.map((node) => ({ x: node.x, y: node.y }));
    resize(state, 400, 200, 20);
    expect(state.nodes).toHaveLength(20);
    state.nodes.forEach((node, index) => {
      expect(node.x).toBeCloseTo(before[index].x / 2, 6);
      expect(node.y).toBeCloseTo(before[index].y / 2, 6);
    });
    resize(state, 1600, 800, 36);
    expect(state.nodes).toHaveLength(36);
    expect(state.width).toBe(1600);
    expect(state.height).toBe(800);
    for (const node of state.nodes) {
      expect(node.x).toBeGreaterThanOrEqual(0);
      expect(node.x).toBeLessThanOrEqual(1600);
    }
  });
});

describe('constellation renderer', () => {
  it('draws every node as a glow and a core, edges as lines, and keeps alpha within [0, 1]', () => {
    const state = createEngine({ width: 600, height: 300, nodeCount: 30, seed: 12 });
    const ctx = stubContext();
    const alphas: number[] = [];
    const tracked = new Proxy(ctx, {
      set(target, key, value) {
        if (key === 'globalAlpha') alphas.push(value as number);
        return Reflect.set(target, key, value);
      },
    });
    drawFrame(tracked, state, PALETTE, { dpr: 2, copyRegion: null });
    expect(ctx.setTransform).toHaveBeenCalledWith(2, 0, 0, 2, 0, 0);
    expect(ctx.clearRect).toHaveBeenCalledWith(0, 0, 600, 300);
    expect(ctx.arc).toHaveBeenCalledTimes(state.nodes.length * 2);
    expect(ctx.stroke).toHaveBeenCalledTimes(edgesOf(state).length);
    for (const alpha of alphas) {
      expect(alpha).toBeGreaterThanOrEqual(0);
      expect(alpha).toBeLessThanOrEqual(1);
    }
  });

  it('dims everything inside the copy region by half', () => {
    const state = createEngine({ width: 600, height: 300, nodeCount: 24, seed: 12 });
    const whole = { x: 0, y: 0, width: 600, height: 300 };
    const collect = (region: typeof whole | null) => {
      const ctx = stubContext();
      const alphas: number[] = [];
      const tracked = new Proxy(ctx, {
        set(target, key, value) {
          if (key === 'globalAlpha' && value !== 1) alphas.push(value as number);
          return Reflect.set(target, key, value);
        },
      });
      drawFrame(tracked, state, PALETTE, { dpr: 1, copyRegion: region });
      return alphas;
    };
    const plain = collect(null);
    const dimmed = collect(whole);
    expect(dimmed).toHaveLength(plain.length);
    dimmed.forEach((alpha, index) => expect(alpha).toBeCloseTo(plain[index] * COPY_DIM, 9));
  });
});

type IntersectionCallback = (entries: Array<Pick<IntersectionObserverEntry, 'isIntersecting'>>) => void;

describe('<Constellation>', () => {
  let ctx: ReturnType<typeof stubContext>;
  let intersect: IntersectionCallback | undefined;
  let rafCalls: number;
  let Constellation: typeof import('./Constellation').Constellation;

  function mockMatchMedia(matches: boolean) {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      writable: true,
      value: vi.fn().mockReturnValue({ matches, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
    });
  }

  beforeEach(async () => {
    ctx = stubContext();
    rafCalls = 0;
    intersect = undefined;
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
      () => ctx as unknown as CanvasRenderingContext2D,
    );
    Object.defineProperty(window, 'requestAnimationFrame', {
      configurable: true,
      writable: true,
      value: vi.fn(() => {
        rafCalls += 1;
        return rafCalls;
      }),
    });
    Object.defineProperty(window, 'cancelAnimationFrame', { configurable: true, writable: true, value: vi.fn() });
    class IntersectionObserverMock {
      constructor(callback: IntersectionCallback) {
        intersect = callback;
      }
      observe = vi.fn();
      disconnect = vi.fn();
    }
    class ResizeObserverMock {
      observe = vi.fn();
      disconnect = vi.fn();
    }
    vi.stubGlobal('IntersectionObserver', IntersectionObserverMock);
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);
    mockMatchMedia(false);
    // The visitor's pause choice lives in module scope; a fresh module keeps tests independent.
    vi.resetModules();
    ({ Constellation } = await import('./Constellation'));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders a hidden canvas and a pause button, and starts running', () => {
    const { container } = render(<Constellation />);
    const canvas = container.querySelector('canvas');
    expect(canvas).toHaveAttribute('aria-hidden', 'true');
    const button = screen.getByRole('button', { name: 'Pause background' });
    expect(button).toHaveAttribute('type', 'button');
    expect(button).toHaveAttribute('aria-pressed', 'false');
    expect(container.firstElementChild).toHaveAttribute('data-motion', 'running');
    expect(rafCalls).toBeGreaterThan(0);
  });

  it('pauses on click, with aria-pressed and the label switching to play', () => {
    const { container } = render(<Constellation />);
    fireEvent.click(screen.getByRole('button', { name: 'Pause background' }));
    const button = screen.getByRole('button', { name: 'Play background' });
    expect(button).toHaveAttribute('aria-pressed', 'true');
    expect(container.firstElementChild).toHaveAttribute('data-motion', 'paused');
    expect(window.cancelAnimationFrame).toHaveBeenCalled();
    fireEvent.click(button);
    expect(screen.getByRole('button', { name: 'Pause background' })).toHaveAttribute('aria-pressed', 'false');
    expect(container.firstElementChild).toHaveAttribute('data-motion', 'running');
  });

  it('draws exactly one static frame under prefers-reduced-motion and plays only on request', () => {
    mockMatchMedia(true);
    const { container } = render(<Constellation />);
    expect(container.firstElementChild).toHaveAttribute('data-motion', 'static');
    expect(ctx.clearRect).toHaveBeenCalledTimes(1);
    expect(rafCalls).toBe(0);
    const button = screen.getByRole('button', { name: 'Play background' });
    expect(button).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(button);
    expect(container.firstElementChild).toHaveAttribute('data-motion', 'running');
    expect(screen.getByRole('button', { name: 'Pause background' })).toHaveAttribute('aria-pressed', 'false');
    expect(rafCalls).toBeGreaterThan(0);
  });

  it('pauses while off-screen and resumes when back in view', () => {
    const { container } = render(<Constellation />);
    expect(intersect).toBeDefined();
    act(() => intersect?.([{ isIntersecting: false }]));
    expect(container.firstElementChild).toHaveAttribute('data-motion', 'paused');
    // Auto-pause is not the visitor's choice, so the control still offers to pause.
    expect(screen.getByRole('button', { name: 'Pause background' })).toHaveAttribute('aria-pressed', 'false');
    act(() => intersect?.([{ isIntersecting: true }]));
    expect(container.firstElementChild).toHaveAttribute('data-motion', 'running');
  });

  it('pauses in a hidden tab and does not resume while the visitor has paused it', () => {
    const { container } = render(<Constellation />);
    const visibility = vi.spyOn(document, 'visibilityState', 'get');
    visibility.mockReturnValue('hidden');
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(container.firstElementChild).toHaveAttribute('data-motion', 'paused');
    visibility.mockReturnValue('visible');
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(container.firstElementChild).toHaveAttribute('data-motion', 'running');
    fireEvent.click(screen.getByRole('button', { name: 'Pause background' }));
    act(() => intersect?.([{ isIntersecting: true }]));
    expect(container.firstElementChild).toHaveAttribute('data-motion', 'paused');
  });
});
