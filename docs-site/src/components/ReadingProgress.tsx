import styles from './ReadingProgress.module.css';

/**
 * Reading progress bar for long documents: a 3 px rule fixed under the site header that
 * fills from left to right as the page scrolls. CSS scroll-driven animation only, no
 * JavaScript and no layout impact (fixed, transform-only), hidden unless the browser
 * supports `animation-timeline: scroll()` and the reader has not asked for reduced motion.
 * Decorative, so it is hidden from assistive technology.
 */
export function ReadingProgress() {
  return <div className={styles.progress} aria-hidden="true" data-reading-progress />;
}
