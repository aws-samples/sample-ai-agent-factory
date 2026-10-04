import type { AnchorHTMLAttributes, ReactNode } from 'react';
import { ExternalLink as ExternalLinkIcon } from 'lucide-react';
import styles from './ExternalLink.module.css';

export interface ExternalLinkProps
  extends Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'href' | 'target' | 'rel' | 'aria-label' | 'children'> {
  href: string;
  children: ReactNode;
  /** Hide the decorative icon (the hidden "(opens in new tab)" text is always rendered). */
  hideIcon?: boolean;
  iconSize?: number;
}

/**
 * Anchor that opens in a new tab. Appends an aria-hidden icon and a visually
 * hidden "(opens in new tab)" so the accessible name says what happens.
 * Use this for every target="_blank" link. Do not pass aria-label; it would
 * override the hidden text.
 */
export function ExternalLink({ href, children, hideIcon = false, iconSize = 14, className, ...rest }: ExternalLinkProps) {
  return (
    <a {...rest} href={href} target="_blank" rel="noopener noreferrer" className={className}>
      {children}
      {!hideIcon && <ExternalLinkIcon size={iconSize} aria-hidden="true" className={styles.icon} />}
      <span className="visually-hidden"> (opens in new tab)</span>
    </a>
  );
}
