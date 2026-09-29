import type { ReactNode } from 'react';
import styles from './SectionHeading.module.css';

export interface SectionHeadingProps {
  /** Id for the heading element, used by `aria-labelledby` on the enclosing section. */
  id?: string;
  /** Small uppercase label above the title. */
  eyebrow?: ReactNode;
  title: ReactNode;
  level?: 2 | 3;
  /** One or two sentences under the title, in secondary text. */
  lead?: ReactNode;
  align?: 'start' | 'center';
  /** Badge rendered before the title text, inside the heading (for project headings). */
  badge?: ReactNode;
  /** Links or buttons aligned to the end of the heading row. */
  actions?: ReactNode;
  className?: string;
}

/**
 * One heading rhythm for every section: optional eyebrow, the heading, an optional
 * lead and optional actions. Home centres its headings; everything else starts left.
 */
export function SectionHeading({
  id,
  eyebrow,
  title,
  level = 2,
  lead,
  align = 'start',
  badge,
  actions,
  className,
}: SectionHeadingProps) {
  const Heading = level === 3 ? 'h3' : 'h2';
  return (
    <div className={[styles.heading, className].filter(Boolean).join(' ')} data-align={align} data-section-heading>
      <div className={styles.text}>
        {eyebrow && <p className={styles.eyebrow}>{eyebrow}</p>}
        <Heading id={id} className={styles.title}>
          {badge && <span className={styles.badge}>{badge}</span>}
          <span>{title}</span>
        </Heading>
        {lead && <p className={styles.lead}>{lead}</p>}
      </div>
      {actions && <div className={styles.actions}>{actions}</div>}
    </div>
  );
}
