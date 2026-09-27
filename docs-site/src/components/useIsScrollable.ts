import { useEffect, useState, type RefObject } from 'react';

/**
 * True once the element's content is wider than its box (a horizontal scroll container
 * that actually scrolls). Starts false so prerendered HTML and the first client render
 * agree; the value updates after mount and on resize.
 */
export function useIsScrollable(ref: RefObject<HTMLElement | null>): boolean {
  const [scrollable, setScrollable] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const update = () => setScrollable(element.scrollWidth > element.clientWidth + 1);
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref]);
  return scrollable;
}
