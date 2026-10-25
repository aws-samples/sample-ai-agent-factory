import { useCallback, useEffect, useId, useRef, useState, type MouseEvent, type ReactNode } from 'react';
import { Maximize2, X } from 'lucide-react';
import { Button } from './Button';
import { DocImage, type ImageFetchPriority, type ImageLoading } from './DocImage';
import { ExternalLink } from './ExternalLink';
import styles from './Figure.module.css';

export interface FigureProps {
  /** Imported asset URL (never an external URL). */
  src: string;
  alt: string;
  caption: ReactNode;
  width?: number;
  height?: number;
  /** Optional download link, e.g. the .drawio source on GitHub. */
  download?: { href: string; label: string };
  /** Defaults to lazy; pass "eager" for the hero figure of a page. */
  loading?: ImageLoading;
  fetchPriority?: ImageFetchPriority;
  /** Default true: the image sits on a white panel, which keeps raster diagrams with white
   *  interiors framed in the dark theme. Pass false for screenshots that are dark themselves,
   *  so they sit directly on the surface instead of inside a bright frame. */
  panel?: boolean;
  className?: string;
}

/**
 * Image with a visible caption, an optional source download link and an "Open full
 * size" control. The control opens a native `<dialog>` (modal, so focus stays inside
 * and Escape closes it) that shows the same image at its natural width inside a
 * scrollable panel. The dialog is closed by default, so prerendered pages never
 * carry it open; body scroll is locked while it is open and focus returns to the
 * opener on close.
 */
export function Figure({
  src,
  alt,
  caption,
  width,
  height,
  download,
  loading,
  fetchPriority,
  panel = true,
  className,
}: FigureProps) {
  const captionId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  /* `Button` does not forward refs; the opener is found inside this wrapper when focus returns. */
  const actionsRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!open || !dialog) return;
    if (!dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    }
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  const close = useCallback(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (typeof dialog.close === 'function') dialog.close();
    else {
      dialog.removeAttribute('open');
      dialog.dispatchEvent(new Event('close'));
    }
  }, []);

  /** Fired by the browser after Escape, the Close button or a backdrop click. */
  const handleClose = useCallback(() => {
    setOpen(false);
    actionsRef.current?.querySelector('button')?.focus();
  }, []);

  /** The dialog box has no padding of its own, so a click on the element itself is a backdrop click. */
  const handleBackdropClick = useCallback(
    (event: MouseEvent<HTMLDialogElement>) => {
      if (event.target === event.currentTarget) close();
    },
    [close],
  );

  return (
    <figure className={[styles.figure, className].filter(Boolean).join(' ')} data-figure data-panel={panel ? 'light' : 'plain'}>
      <div className={styles.panel}>
        <DocImage src={src} alt={alt} width={width} height={height} loading={loading} fetchPriority={fetchPriority} />
      </div>
      <figcaption id={captionId} className={styles.caption}>
        {caption}
        {download && (
          <>
            {' '}
            <ExternalLink href={download.href}>{download.label}</ExternalLink>
          </>
        )}
      </figcaption>
      <div className={styles.actions} ref={actionsRef}>
        <Button
          variant="ghost"
          size="sm"
          iconStart={<Maximize2 size={16} />}
          aria-describedby={captionId}
          aria-haspopup="dialog"
          onClick={() => setOpen(true)}
          data-figure-open
        >
          Open full size
        </Button>
      </div>
      <dialog
        ref={dialogRef}
        className={styles.dialog}
        aria-labelledby={captionId}
        onClose={handleClose}
        onClick={handleBackdropClick}
        data-figure-dialog
      >
        <div className={styles.dialogBar}>
          <Button variant="secondary" size="sm" iconStart={<X size={16} />} onClick={close} data-figure-close>
            Close
          </Button>
        </div>
        {open && (
          <div className={styles.dialogBody}>
            <img src={src} alt={alt} width={width} height={height} decoding="async" className={styles.fullImage} />
          </div>
        )}
      </dialog>
    </figure>
  );
}
