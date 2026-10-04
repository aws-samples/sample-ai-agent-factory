import { useId, type ReactNode } from 'react';
import { AlertTriangle, Info, ShieldAlert } from 'lucide-react';
import styles from './Callout.module.css';

export type CalloutKind = 'note' | 'warning' | 'important';

export interface CalloutProps {
  kind?: CalloutKind;
  title: string;
  children: ReactNode;
  className?: string;
}

const ICONS = { note: Info, warning: AlertTriangle, important: ShieldAlert } as const;

/**
 * Tinted callout. The title is a paragraph, not a heading, so callouts never
 * disturb the page heading outline. Colour is never the only cue: each kind
 * has an icon and a visible title.
 *
 * Rendered as `role="note"` (not `<aside>`): a page often carries several callouts
 * of the same kind, and repeated complementary landmarks fail axe `landmark-unique`.
 */
export function Callout({ kind = 'note', title, children, className }: CalloutProps) {
  const Icon = ICONS[kind];
  const titleId = useId();
  return (
    <div role="note" aria-labelledby={titleId} className={[styles.callout, styles[kind], className].filter(Boolean).join(' ')}>
      <p className={styles.title} id={titleId}>
        <Icon size={18} aria-hidden="true" className={styles.icon} />
        <strong>{title}</strong>
      </p>
      <div className={styles.body}>{children}</div>
    </div>
  );
}
