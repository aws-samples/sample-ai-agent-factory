import { Link, useLocation } from 'react-router-dom';
import styles from './SectionNav.module.css';

export interface SectionNavItem {
  label: string;
  /** Canonical site path with a trailing slash. */
  to: string;
}

export interface SectionNavProps {
  /** Accessible name of the nav landmark, for example "Concepts section". Defaults to "In this section". */
  label?: string;
  items: SectionNavItem[];
  className?: string;
}

const normalise = (path: string) => path.replace(/\/+$/, '') || '/';

/**
 * "In this section" chip rail linking the pages of one hub section. The current
 * page is marked with aria-current. Below 640px the chips scroll horizontally
 * inside the list; from 640px they wrap. Styled like the Start section's rail.
 */
export function SectionNav({ label = 'In this section', items, className }: SectionNavProps) {
  const { pathname } = useLocation();
  const current = normalise(pathname);
  return (
    <nav aria-label={label} className={[styles.nav, className].filter(Boolean).join(' ')}>
      <span className={styles.label}>In this section:</span>
      <ul className={styles.chips}>
        {items.map(({ to, label: itemLabel }) => (
          <li key={to}>
            <Link to={to} className={styles.chip} aria-current={normalise(to) === current ? 'page' : undefined}>
              {itemLabel}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
