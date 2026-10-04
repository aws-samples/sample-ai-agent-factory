import { useEffect, useRef, type RefObject } from 'react';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'iframe',
  'object',
  'embed',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) =>
      !element.hidden &&
      element.getAttribute('aria-hidden') !== 'true' &&
      !element.closest('[inert]') &&
      element.tabIndex >= 0,
  );
}

function topmostFocusTrap(): HTMLElement | null {
  const active = document.querySelectorAll<HTMLElement>('[data-focus-trap-active="true"]');
  return active.item(active.length - 1);
}

function focusFirst(
  container: HTMLElement,
  initialFocusRef?: RefObject<HTMLElement | null>,
): void {
  const requested = initialFocusRef?.current;
  const target =
    (requested && container.contains(requested) && requested) ||
    container.querySelector<HTMLElement>('[data-autofocus="true"]') ||
    focusableElements(container)[0] ||
    container;
  target.focus();
}

/**
 * Keep keyboard and programmatic focus inside the topmost open modal.
 *
 * Each active trap marks its own dialog root. The last marked root in DOM order
 * wins, so a nested confirmation can take focus without the parent dialog
 * fighting it. On close, focus returns to the element that opened the dialog.
 */
export function useDialogFocusTrap(
  isOpen: boolean,
  containerRef: RefObject<HTMLElement | null>,
  initialFocusRef?: RefObject<HTMLElement | null>,
  onEscape?: () => void,
): void {
  const onEscapeRef = useRef(onEscape);
  useEffect(() => {
    onEscapeRef.current = onEscape;
  }, [onEscape]);

  useEffect(() => {
    if (!isOpen) return;

    const container = containerRef.current;
    if (!container) return;

    const previouslyFocused =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    container.dataset.focusTrapActive = 'true';

    const focusTimer = window.setTimeout(() => {
      if (topmostFocusTrap() === container && !container.contains(document.activeElement)) {
        focusFirst(container, initialFocusRef);
      }
    }, 0);

    const handleKeyDown = (event: KeyboardEvent) => {
      if (topmostFocusTrap() !== container) {
        return;
      }

      if (event.key === 'Escape' && onEscapeRef.current) {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        onEscapeRef.current();
        return;
      }

      if (event.key !== 'Tab' || event.defaultPrevented) {
        return;
      }

      const focusable = focusableElements(container);
      if (focusable.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;

      if (!container.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && (active === first || !focusable.includes(active as HTMLElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !focusable.includes(active as HTMLElement))) {
        event.preventDefault();
        first.focus();
      }
    };

    const handleFocusIn = (event: FocusEvent) => {
      if (
        topmostFocusTrap() === container &&
        event.target instanceof Node &&
        !container.contains(event.target)
      ) {
        focusFirst(container, initialFocusRef);
      }
    };

    document.addEventListener('keydown', handleKeyDown, true);
    document.addEventListener('focusin', handleFocusIn);

    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener('keydown', handleKeyDown, true);
      document.removeEventListener('focusin', handleFocusIn);
      delete container.dataset.focusTrapActive;
      if (previouslyFocused?.isConnected) {
        previouslyFocused.focus();
      }
    };
  }, [isOpen, containerRef, initialFocusRef]);
}
