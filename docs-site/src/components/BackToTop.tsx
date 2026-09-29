import { ArrowUp } from 'lucide-react';
import styles from './BackToTop.module.css';

/**
 * "Back to top" link for long documents, fixed bottom-right. It targets the main
 * landmark (`#main-content` in Layout), so activating it both scrolls to the top and
 * moves focus to the start of the page. In browsers with scroll-driven animations it
 * slides in from below the viewport during the first 40vh of scrolling (translate only,
 * never opacity, so its contrast is constant); elsewhere it is simply visible.
 */
export function BackToTop() {
  return (
    <a href="#main-content" className={styles.top} data-back-to-top>
      <ArrowUp size={16} aria-hidden="true" />
      Back to top
    </a>
  );
}
