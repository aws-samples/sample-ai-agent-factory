import type { ReactNode } from 'react';
import { Link, useLocation } from 'react-router-dom';
import styles from './ChipNav.module.css';

export interface ChipNavItem {
  label: ReactNode;
  /** Site path (react-router); the current page is marked with aria-current. */
  to?: string;
  /** In-page fragment ("#quickstart") or other plain href. */
  href?: string;
  /** Optional decoration before the label, for example an outline stage badge. */
  badge?: ReactNode;
}

export interface ChipNavProps {
  /** Accessible name of the nav landmark. Required: several chip navs can share a page. */
  label: string;
  /** Visible lead text before the chips, for example "Jump to:". */
  lead?: ReactNode;
  items: ChipNavItem[];
  /** scroll: one row that scrolls below 640 px and wraps above; wrap: always wraps. */
  overflow?: 'scroll' | 'wrap';
  size?: 'sm' | 'md';
  /** Stick under the site header while the section scrolls (letter navs, long pages). */
  sticky?: boolean;
  className?: string;
}

const normalise = (path: string) => path.replace(/\/+$/, '') || '/';

/**
 * The site's one chip rail: section navigation, in-page jump links and letter
 * navigation all render through it. Items with `to` mark the current page.
 */
export function ChipNav({ label, lead, items, overflow = 'scroll', size = 'md', sticky = false, className }: ChipNavProps) {
  const { pathname } = useLocation();
  const current = normalise(pathname);
  return (
    <nav
      aria-label={label}
      className={[styles.nav, className].filter(Boolean).join(' ')}
      data-chip-nav
      data-overflow={overflow}
      data-size={size}
      data-sticky={sticky ? '' : undefined}
    >
      {lead && <span className={styles.lead}>{lead}</span>}
      <ul className={styles.chips}>
        {items.map((item, index) => {
          const key = item.to ?? item.href ?? String(index);
          const inner = (
            <>
              {item.badge && <span className={styles.badge}>{item.badge}</span>}
              <span>{item.label}</span>
            </>
          );
          return (
            <li key={key}>
              {item.to ? (
                <Link to={item.to} className={styles.chip} aria-current={normalise(item.to) === current ? 'page' : undefined}>
                  {inner}
                </Link>
              ) : (
                <a href={item.href} className={styles.chip}>
                  {inner}
                </a>
              )}
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
