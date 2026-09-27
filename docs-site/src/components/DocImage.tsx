import type { ImgHTMLAttributes } from 'react';
import { resolveAlt } from './docImageAlt';
import styles from './DocImage.module.css';

export type ImageLoading = 'lazy' | 'eager';
export type ImageFetchPriority = 'high' | 'low' | 'auto';

export interface DocImageProps extends Omit<ImgHTMLAttributes<HTMLImageElement>, 'alt' | 'loading' | 'fetchPriority'> {
  src: string;
  /** Required. Use an empty string only for purely decorative images. */
  alt: string;
  /** Defaults to lazy; hero figures above the fold should pass "eager". */
  loading?: ImageLoading;
  /** Resource hint for the browser; pair "high" with loading="eager" on the hero image. */
  fetchPriority?: ImageFetchPriority;
}

/** Responsive image used for MDX `img` elements and page figures. Lazy unless told otherwise. */
export function DocImage({ src, alt, className, loading = 'lazy', fetchPriority, ...rest }: DocImageProps) {
  if (typeof alt !== 'string') {
    throw new Error(`DocImage requires alt text (image: ${src})`);
  }
  // React 18 only forwards the lowercase attribute name; the camelCase prop would warn.
  const priority: Record<string, string> = fetchPriority ? { fetchpriority: fetchPriority } : {};
  return (
    <img
      src={src}
      alt={resolveAlt(src, alt)}
      loading={loading}
      decoding="async"
      className={className ? `${styles.image} ${className}` : styles.image}
      {...priority}
      {...rest}
    />
  );
}
