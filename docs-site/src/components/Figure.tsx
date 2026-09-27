import type { ReactNode } from 'react';
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
  className?: string;
}

/** Image with a visible caption and an optional source download link. */
export function Figure({ src, alt, caption, width, height, download, loading, fetchPriority, className }: FigureProps) {
  return (
    <figure className={[styles.figure, className].filter(Boolean).join(' ')}>
      <DocImage src={src} alt={alt} width={width} height={height} loading={loading} fetchPriority={fetchPriority} />
      <figcaption className={styles.caption}>
        {caption}
        {download && (
          <>
            {' '}
            <ExternalLink href={download.href}>{download.label}</ExternalLink>
          </>
        )}
      </figcaption>
    </figure>
  );
}
