import type { ReactNode } from 'react';
import { SectionHeading, type SectionHeadingProps } from './SectionHeading';
import styles from './Section.module.css';

export interface SectionProps {
  /** Fragment id; the heading gets `${id}-heading` and labels the region. */
  id: string;
  title: ReactNode;
  eyebrow?: SectionHeadingProps['eyebrow'];
  lead?: SectionHeadingProps['lead'];
  badge?: SectionHeadingProps['badge'];
  actions?: SectionHeadingProps['actions'];
  align?: SectionHeadingProps['align'];
  /** Remove the top rule (first section under a header or a chip nav). */
  flush?: boolean;
  className?: string;
  children: ReactNode;
}

/**
 * A page section with one heading rhythm: top rule, consistent padding, an h2
 * from `SectionHeading`, and `scroll-margin-top` so in-page links land below the
 * sticky site header.
 */
export function Section({ id, title, eyebrow, lead, badge, actions, align, flush = false, className, children }: SectionProps) {
  const headingId = `${id}-heading`;
  return (
    <section
      id={id}
      className={[styles.section, className].filter(Boolean).join(' ')}
      aria-labelledby={headingId}
      data-flush={flush ? '' : undefined}
    >
      <SectionHeading id={headingId} title={title} eyebrow={eyebrow} lead={lead} badge={badge} actions={actions} align={align} />
      {children}
    </section>
  );
}
