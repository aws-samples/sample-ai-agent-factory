/**
 * Shared motion primitives (redesign).
 *
 * One source of truth for spring physics + transition presets so every surface
 * (nodes, panels, modals, buttons) feels like the same product. Mirrors the CSS
 * duration/easing tokens in index.css.
 *
 * Bundle note: import { m, LazyMotion, domAnimation } from 'motion/react' and
 * wrap the app once in <LazyMotion features={domAnimation}> (see AppMotionProvider),
 * then use <m.div> instead of <motion.div> to ship the lean (~5KB) feature set.
 */
import type { Transition, Variants } from 'motion/react';

/** Springs — tuned for a crisp, non-bouncy "tool" feel (Linear-like). */
export const spring = {
  /** Snappy UI feedback: buttons, hovers, small state changes. */
  snappy: { type: 'spring', stiffness: 520, damping: 34, mass: 0.9 } as Transition,
  /** Default for panels/cards entering. */
  smooth: { type: 'spring', stiffness: 320, damping: 32, mass: 1 } as Transition,
  /** Gentle, for large surfaces (drawers, modals). */
  gentle: { type: 'spring', stiffness: 240, damping: 30, mass: 1 } as Transition,
  /** A touch of overshoot for playful accents (node drop, badges). */
  bouncy: { type: 'spring', stiffness: 420, damping: 22, mass: 0.8 } as Transition,
} as const;

/** Tween presets keyed to the CSS duration tokens. */
export const tween = {
  fast: { duration: 0.12, ease: [0.4, 0, 0.2, 1] } as Transition,
  base: { duration: 0.2, ease: [0.22, 1, 0.36, 1] } as Transition,
  slow: { duration: 0.32, ease: [0.22, 1, 0.36, 1] } as Transition,
} as const;

/* ── Reusable variants ─────────────────────────────────────────────────── */

/** Fade + rise. Good default for cards, list items, empty states. */
export const fadeRise: Variants = {
  hidden: { opacity: 0, y: 8 },
  visible: { opacity: 1, y: 0, transition: spring.smooth },
  exit: { opacity: 0, y: 6, transition: tween.fast },
};

/** Scale + fade — modals/dialogs. */
export const popIn: Variants = {
  hidden: { opacity: 0, scale: 0.96, y: 6 },
  visible: { opacity: 1, scale: 1, y: 0, transition: spring.smooth },
  exit: { opacity: 0, scale: 0.98, y: 4, transition: tween.fast },
};

/** Right-side drawer slide (deploy panel, AI panels). */
export const drawerRight: Variants = {
  hidden: { x: '100%', opacity: 0.6 },
  visible: { x: 0, opacity: 1, transition: spring.gentle },
  exit: { x: '100%', opacity: 0.4, transition: tween.slow },
};

/** Backdrop scrim fade. */
export const scrim: Variants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1, transition: tween.base },
  exit: { opacity: 0, transition: tween.fast },
};

/** Node entering the canvas — small overshoot "drop in". */
export const nodeEnter: Variants = {
  hidden: { opacity: 0, scale: 0.9 },
  visible: { opacity: 1, scale: 1, transition: spring.bouncy },
  exit: { opacity: 0, scale: 0.92, transition: tween.fast },
};

/** Stagger container: children animate in sequence. */
export const staggerContainer = (stagger = 0.06, delayChildren = 0): Variants => ({
  hidden: {},
  visible: {
    transition: { staggerChildren: stagger, delayChildren },
  },
});

/** Standard hover/tap feedback props for interactive elements. */
export const pressable = {
  whileHover: { scale: 1.02 },
  whileTap: { scale: 0.97 },
  transition: spring.snappy,
} as const;
