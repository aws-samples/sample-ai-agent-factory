import type { JourneyStage } from '../content/data';
import styles from './HeaderGlow.module.css';

export type HeaderHue = JourneyStage | 'slate';

/**
 * Static, CSS-only backdrop for page headers: two soft radial glows in a section
 * hue and the faint 40 px dotted grid used by the Atlas diagram. No JavaScript, no
 * motion, `pointer-events: none`; it only gives the dark band some depth.
 */
export function HeaderGlow({ hue = 'scale' }: { hue?: HeaderHue }) {
  return <div className={styles.glow} data-hue={hue} aria-hidden="true" />;
}
