import { Pause, Play } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { createEngine, nodeCountFor, resize, step, type EngineState, type Rect } from './constellation/engine';
import { drawFrame, type Canvas2D, type Palette } from './constellation/render';
import styles from './Constellation.module.css';

/** What the backdrop is doing right now; tests and styles read it from `data-motion`. */
export type Motion = 'running' | 'paused' | 'static';

/** The visitor's explicit choice; `null` until they press the button. */
type Choice = 'play' | 'pause' | null;

/**
 * The visitor's pause/play choice for this page session. Kept in module scope so it survives
 * client-side navigation but never a reload; the site writes nothing to web storage.
 */
let sessionChoice: Choice = null;

/** Frames are rendered at most this often (30 fps). */
const FRAME_MS = 1000 / 30;
const RESIZE_DEBOUNCE_MS = 150;
const MAX_DPR = 2;
const STAGE_VARIABLES = ['--constellation-learn', '--constellation-build', '--constellation-govern', '--constellation-scale'];
/** Same values as tokens.css, used only if a custom property cannot be read. */
const STAGE_FALLBACK = ['#FFB341', '#3DF58F', '#C084FC', '#38D6FF'];

function readPalette(): Palette {
  const computed = getComputedStyle(document.documentElement);
  return {
    stages: STAGE_VARIABLES.map((name, index) => computed.getPropertyValue(name).trim() || STAGE_FALLBACK[index]),
  };
}

function prefersReducedMotion(): MediaQueryList | null {
  return typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
}

function isLowPower(): boolean {
  const cores = navigator.hardwareConcurrency;
  return (typeof cores === 'number' && cores <= 4) || window.innerWidth < 640;
}

/** Padding (CSS px) added around the copy block so the dimmed area clears the text comfortably. */
const COPY_MARGIN = 16;

/**
 * Bounding box of the header's copy block (the element wrapping the h1), relative to the backdrop,
 * so drawing under the text can be dimmed. Measured from the DOM rather than assumed, so it follows
 * the block wherever the header places it (left column or centred).
 */
function measureCopyRegion(root: HTMLElement): Rect | null {
  const header = root.closest<HTMLElement>('[data-page-header]') ?? root.parentElement?.parentElement ?? null;
  const copy = header?.querySelector<HTMLElement>('[data-copy]') ?? header?.querySelector('h1')?.parentElement ?? null;
  if (!copy) return null;
  const rootRect = root.getBoundingClientRect();
  const copyRect = copy.getBoundingClientRect();
  return {
    x: copyRect.left - rootRect.left - COPY_MARGIN,
    y: copyRect.top - rootRect.top - COPY_MARGIN,
    width: copyRect.width + COPY_MARGIN * 2,
    height: copyRect.height + COPY_MARGIN * 2,
  };
}

/** Whether the animation should run given the visitor's choice and their motion preference. */
function wantsMotion(choice: Choice, reduce: boolean): boolean {
  return choice === 'play' || (choice === null && !reduce);
}

/**
 * Ambient constellation for the Home hero: drifting stage-coloured nodes, distance-faded edges and
 * an occasional pulse travelling along an edge, drawn on a canvas behind the copy. Mounted through
 * PageHeader's `backdrop` slot, lazily and after first paint, so it never touches the prerendered
 * HTML or the largest contentful paint.
 *
 * Behaviour: honours `prefers-reduced-motion` (one static frame, `data-motion="static"`), pauses
 * off-screen and in hidden tabs (`data-motion="paused"`), and offers a visible pause/play button
 * (WCAG 2.2.2) whose choice lasts for the page session. Makes no network requests and writes no storage.
 */
export function Constellation() {
  const rootRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const applyChoiceRef = useRef<(choice: Choice) => void>(() => undefined);
  const [motion, setMotion] = useState<Motion>('static');
  const [choice, setChoice] = useState<Choice>(() => sessionChoice);
  const [reduce, setReduce] = useState(false);

  useEffect(() => {
    const root = rootRef.current;
    const canvas = canvasRef.current;
    if (!root || !canvas) return;
    const context = canvas.getContext('2d') as (CanvasRenderingContext2D & Canvas2D) | null;
    if (!context) return;
    const ctx: Canvas2D = context;

    const palette = readPalette();
    const media = prefersReducedMotion();
    let reduceMotion = media?.matches ?? false;
    setReduce(reduceMotion);

    let currentChoice: Choice = sessionChoice;
    let inView = true;
    let documentVisible = document.visibilityState !== 'hidden';
    let rafId = 0;
    let lastFrame = 0;
    let dpr = 1;
    let copyRegion: Rect | null = null;
    let engine: EngineState | null = null;
    let resizeTimer = 0;

    const measure = () => {
      const rect = root.getBoundingClientRect();
      const width = Math.max(1, Math.round(rect.width));
      const height = Math.max(1, Math.round(rect.height));
      dpr = Math.min(MAX_DPR, window.devicePixelRatio || 1);
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      copyRegion = measureCopyRegion(root);
      const nodeCount = nodeCountFor(width * height, isLowPower());
      if (engine) resize(engine, width, height, nodeCount);
      else engine = createEngine({ width, height, nodeCount, seed: Math.floor(Math.random() * 0xffffffff) });
    };

    const draw = () => {
      if (engine) drawFrame(ctx, engine, palette, { dpr, copyRegion });
    };

    const frame = (timestamp: number) => {
      rafId = window.requestAnimationFrame(frame);
      if (timestamp - lastFrame < FRAME_MS - 2) return;
      const dt = lastFrame === 0 ? 0 : (timestamp - lastFrame) / 1000;
      lastFrame = timestamp;
      if (engine) step(engine, dt);
      draw();
    };

    const stop = () => {
      if (rafId) window.cancelAnimationFrame(rafId);
      rafId = 0;
      lastFrame = 0;
    };

    /** Start or stop the loop to match the current choice, preference and visibility. */
    const sync = () => {
      const run = wantsMotion(currentChoice, reduceMotion) && inView && documentVisible;
      if (run) {
        if (!rafId) rafId = window.requestAnimationFrame(frame);
        setMotion('running');
      } else {
        stop();
        setMotion(currentChoice === null && reduceMotion ? 'static' : 'paused');
      }
    };

    applyChoiceRef.current = (next) => {
      currentChoice = next;
      sync();
    };

    measure();
    draw();
    sync();

    const onMediaChange = (event: MediaQueryListEvent) => {
      reduceMotion = event.matches;
      setReduce(reduceMotion);
      sync();
    };
    media?.addEventListener?.('change', onMediaChange);

    const onVisibility = () => {
      documentVisible = document.visibilityState !== 'hidden';
      sync();
    };
    document.addEventListener('visibilitychange', onVisibility);

    let intersection: IntersectionObserver | undefined;
    if (typeof IntersectionObserver === 'function') {
      intersection = new IntersectionObserver((entries) => {
        inView = entries.some((entry) => entry.isIntersecting);
        sync();
      });
      intersection.observe(root);
    }

    let resizeObserver: ResizeObserver | undefined;
    if (typeof ResizeObserver === 'function') {
      resizeObserver = new ResizeObserver(() => {
        window.clearTimeout(resizeTimer);
        resizeTimer = window.setTimeout(() => {
          measure();
          if (!rafId) draw();
        }, RESIZE_DEBOUNCE_MS);
      });
      resizeObserver.observe(root);
    }

    return () => {
      stop();
      window.clearTimeout(resizeTimer);
      media?.removeEventListener?.('change', onMediaChange);
      document.removeEventListener('visibilitychange', onVisibility);
      intersection?.disconnect();
      resizeObserver?.disconnect();
      applyChoiceRef.current = () => undefined;
    };
  }, []);

  const paused = !wantsMotion(choice, reduce);

  const toggle = () => {
    const next: Choice = paused ? 'play' : 'pause';
    sessionChoice = next;
    setChoice(next);
    applyChoiceRef.current(next);
  };

  return (
    <div ref={rootRef} className={styles.root} data-motion={motion}>
      <canvas ref={canvasRef} className={styles.canvas} aria-hidden="true" />
      <div className={styles.fade} />
      <button type="button" className={styles.toggle} aria-pressed={paused} onClick={toggle}>
        {paused ? <Play size={14} aria-hidden="true" /> : <Pause size={14} aria-hidden="true" />}
        {paused ? 'Play background' : 'Pause background'}
      </button>
    </div>
  );
}

export default Constellation;
