import { COPY_DIM, GLOW_RADIUS, PULSE_TAIL, edgesOf, inRect, type EngineState, type Rect } from './engine';

/**
 * The subset of CanvasRenderingContext2D the renderer uses. Typed structurally so unit tests can
 * pass a stub (jsdom has no canvas) and so the engine folder stays free of DOM types.
 */
export interface Canvas2D {
  fillStyle: string | CanvasGradient | CanvasPattern;
  strokeStyle: string | CanvasGradient | CanvasPattern;
  lineWidth: number;
  lineCap: CanvasLineCap;
  globalAlpha: number;
  setTransform(a: number, b: number, c: number, d: number, e: number, f: number): void;
  clearRect(x: number, y: number, w: number, h: number): void;
  beginPath(): void;
  arc(x: number, y: number, radius: number, start: number, end: number): void;
  moveTo(x: number, y: number): void;
  lineTo(x: number, y: number): void;
  fill(): void;
  stroke(): void;
}

export interface Palette {
  /** One colour per stage index (learn, build, govern, scale), as used on the dark band. */
  stages: readonly string[];
}

export interface FrameOptions {
  /** Device pixel ratio the canvas backing store was sized with. */
  dpr: number;
  /** Region (in CSS px, canvas coordinates) under the hero copy; drawing there is dimmed. */
  copyRegion: Rect | null;
}

function dimFor(copyRegion: Rect | null, x: number, y: number): number {
  return inRect(copyRegion, x, y) ? COPY_DIM : 1;
}

/** Draw one frame of the constellation. The canvas is cleared first; the band colour shows through. */
export function drawFrame(ctx: Canvas2D, state: EngineState, palette: Palette, { dpr, copyRegion }: FrameOptions): void {
  const { nodes, width, height } = state;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.lineCap = 'round';

  // Edges: colour of the lower-index node, alpha by distance, dimmed under the copy.
  ctx.lineWidth = 1;
  for (const edge of edgesOf(state)) {
    const a = nodes[edge.a];
    const b = nodes[edge.b];
    const midX = (a.x + b.x) / 2;
    const midY = (a.y + b.y) / 2;
    ctx.globalAlpha = edge.alpha * dimFor(copyRegion, midX, midY);
    ctx.strokeStyle = palette.stages[a.stage % palette.stages.length];
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  }

  // Nodes: a faint glow disc and a small solid core.
  for (const node of nodes) {
    const dim = dimFor(copyRegion, node.x, node.y);
    const colour = palette.stages[node.stage % palette.stages.length];
    ctx.fillStyle = colour;
    ctx.globalAlpha = node.alpha * 0.12 * dim;
    ctx.beginPath();
    ctx.arc(node.x, node.y, GLOW_RADIUS, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = node.alpha * dim;
    ctx.beginPath();
    ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
    ctx.fill();
  }

  // Pulse: a bright dot with a short tail, in the source node's colour.
  const pulse = state.pulse;
  if (pulse && pulse.from < nodes.length && pulse.to < nodes.length) {
    const from = nodes[pulse.from];
    const to = nodes[pulse.to];
    const headX = from.x + (to.x - from.x) * pulse.progress;
    const headY = from.y + (to.y - from.y) * pulse.progress;
    const tailT = Math.max(0, pulse.progress - PULSE_TAIL);
    const tailX = from.x + (to.x - from.x) * tailT;
    const tailY = from.y + (to.y - from.y) * tailT;
    const dim = dimFor(copyRegion, headX, headY);
    const colour = palette.stages[from.stage % palette.stages.length];
    ctx.strokeStyle = colour;
    ctx.fillStyle = colour;
    ctx.lineWidth = 1.5;
    ctx.globalAlpha = 0.45 * dim;
    ctx.beginPath();
    ctx.moveTo(tailX, tailY);
    ctx.lineTo(headX, headY);
    ctx.stroke();
    ctx.globalAlpha = 0.9 * dim;
    ctx.beginPath();
    ctx.arc(headX, headY, 2, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.globalAlpha = 1;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
}
