import { Link } from 'react-router-dom';
import { HomeLink } from './HomeLink';
import styles from './Breadcrumbs.module.css';

export interface Crumb {
  label: string;
  /** Omit for the current page (last item). */
  to?: string;
}

export function Breadcrumbs({ items }: { items: Crumb[] }) {
  return (
    <nav aria-label="Breadcrumb" className={styles.nav}>
      <ol className={styles.list}>
        {items.map((item, index) => {
          const last = index === items.length - 1;
          return (
            <li key={`${item.label}-${index}`} className={styles.item}>
              {item.to && !last ? (
                item.to === '/' ? (
                  <HomeLink className={styles.link}>{item.label}</HomeLink>
                ) : (
                  <Link to={item.to} className={styles.link}>
                    {item.label}
                  </Link>
                )
              ) : (
                <span aria-current={last ? 'page' : undefined} className={styles.current}>
                  {item.label}
                </span>
              )}
              {!last && (
                <span aria-hidden="true" className={styles.separator}>
                  /
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
