import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Figure } from './Figure';

/**
 * jsdom has no `showModal`/`close` on HTMLDialogElement. The polyfill mirrors the
 * browser contract the component relies on: `open` toggles and `close()` fires a
 * `close` event on the element.
 */
function polyfillDialog() {
  const proto = HTMLDialogElement.prototype as HTMLDialogElement & { showModal: () => void; close: () => void };
  const showModal = vi.fn(function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  });
  const close = vi.fn(function (this: HTMLDialogElement) {
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  });
  Object.defineProperty(proto, 'showModal', { value: showModal, configurable: true, writable: true });
  Object.defineProperty(proto, 'close', { value: close, configurable: true, writable: true });
  return { showModal, close };
}

describe('Figure lightbox', () => {
  let polyfill: ReturnType<typeof polyfillDialog>;

  beforeEach(() => {
    polyfill = polyfillDialog();
    document.body.style.overflow = '';
  });

  afterEach(() => {
    document.body.style.overflow = '';
  });

  function renderFigure() {
    return render(
      <Figure
        src="/diagram.png"
        alt="A diagram"
        width={800}
        height={600}
        caption={<strong>Figure 1.</strong>}
        download={{ href: 'https://example.com/diagram.drawio', label: 'Open the source' }}
      />,
    );
  }

  it('renders the dialog closed, without a second image, and keeps the download link', () => {
    renderFigure();
    const dialog = document.querySelector('dialog');
    expect(dialog).not.toBeNull();
    expect(dialog).not.toHaveAttribute('open');
    expect(screen.getAllByRole('img', { hidden: true })).toHaveLength(1);
    expect(screen.getByRole('link', { name: /open the source/i })).toHaveAttribute('href', 'https://example.com/diagram.drawio');
    expect(document.body.style.overflow).toBe('');
  });

  it('opens as a modal with the same image at natural size, locks scroll, and closes via the Close button', () => {
    renderFigure();
    const opener = screen.getByRole('button', { name: /open full size/i });
    fireEvent.click(opener);

    const dialog = document.querySelector('dialog') as HTMLDialogElement;
    expect(polyfill.showModal).toHaveBeenCalledTimes(1);
    expect(dialog).toHaveAttribute('open');
    expect(document.body.style.overflow).toBe('hidden');

    const images = dialog.querySelectorAll('img');
    expect(images).toHaveLength(1);
    expect(images[0]).toHaveAttribute('alt', 'A diagram');
    expect(images[0]).toHaveAttribute('width', '800');

    fireEvent.click(screen.getByRole('button', { name: /^close$/i }));
    expect(polyfill.close).toHaveBeenCalledTimes(1);
    expect(dialog).not.toHaveAttribute('open');
    expect(document.body.style.overflow).toBe('');
    expect(opener).toHaveFocus();
  });

  it('closes on a backdrop click but not on a click inside the content, and returns focus', () => {
    renderFigure();
    const opener = screen.getByRole('button', { name: /open full size/i });
    fireEvent.click(opener);
    const dialog = document.querySelector('dialog') as HTMLDialogElement;

    fireEvent.click(dialog.querySelector('img') as HTMLImageElement);
    expect(dialog).toHaveAttribute('open');

    fireEvent.click(dialog);
    expect(dialog).not.toHaveAttribute('open');
    expect(opener).toHaveFocus();
  });

  it('restores focus and scroll when the browser closes the dialog itself (Escape)', () => {
    renderFigure();
    const opener = screen.getByRole('button', { name: /open full size/i });
    fireEvent.click(opener);
    const dialog = document.querySelector('dialog') as HTMLDialogElement;
    (dialog.querySelector('button') as HTMLButtonElement).focus();

    // Escape makes the browser run close(): the element loses `open` and fires `close`.
    act(() => dialog.close());
    expect(document.body.style.overflow).toBe('');
    expect(opener).toHaveFocus();
  });
});

describe('Figure panel', () => {
  it('frames the image on a light panel by default and drops the frame for dark screenshots', () => {
    const { unmount } = render(<Figure src="/diagram.png" alt="A diagram" caption="Figure" />);
    expect(document.querySelector('figure')).toHaveAttribute('data-panel', 'light');
    unmount();
    render(<Figure src="/shot.png" alt="A dark screenshot" caption="Figure" panel={false} />);
    expect(document.querySelector('figure')).toHaveAttribute('data-panel', 'plain');
  });
});
