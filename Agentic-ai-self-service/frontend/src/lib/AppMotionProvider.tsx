/**
 * Wraps the app in LazyMotion so we can use the lightweight <m.*> components
 * (framer-motion "motion") and ship ~5KB of features instead of the full ~34KB
 * bundle. domAnimation covers everything the redesign needs (transforms,
 * opacity, layout, gestures, exit animations).
 */
import { LazyMotion, domAnimation, MotionConfig } from 'motion/react';
import type { ReactNode } from 'react';

export function AppMotionProvider({ children }: { children: ReactNode }) {
  return (
    <LazyMotion features={domAnimation} strict>
      {/* reducedMotion="user" makes every animation honor the OS setting. */}
      <MotionConfig reducedMotion="user">{children}</MotionConfig>
    </LazyMotion>
  );
}
