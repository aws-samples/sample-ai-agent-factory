import type { ReactNode } from 'react';
import { factText, type Fact, type Source } from '../content/facts';
import { SourceLink } from './SourceLink';
import styles from './StatTile.module.css';

export interface StatTileProps {
  label: ReactNode;
  value: ReactNode;
  /** Caveat under the value. `inline` shows it in muted text; `collapsed` puts it behind a native disclosure. */
  note?: ReactNode;
  noteMode?: 'inline' | 'collapsed';
  /** Repository source for the value; rendered as a small "source" link. */
  source?: Source;
  /** Hidden context for the source link and the disclosure ("cost, Self-Service"), so repeated links stay distinguishable. */
  context?: string;
  /** Decorative icon before the label (rendered aria-hidden). */
  icon?: ReactNode;
  /** stacked: label above value; inline: "Label: value" on one line. */
  layout?: 'stacked' | 'inline';
  /** Render the value in secondary text (for "not documented"). */
  muted?: boolean;
  className?: string;
}

/**
 * One fact as a `dt`/`dd` pair, to slot into a `<dl>`: label with optional icon,
 * value, a small source link and an optional caveat. Long caveats can collapse
 * behind a `<details>` so tiles stay scannable while the source stays visible.
 */
export function StatTile({
  label,
  value,
  note,
  noteMode = 'inline',
  source,
  context,
  icon,
  layout = 'stacked',
  muted = false,
  className,
}: StatTileProps) {
  return (
    <div className={[styles.tile, className].filter(Boolean).join(' ')} data-stat-tile data-layout={layout}>
      <dt className={styles.label}>
        {icon && (
          <span className={styles.icon} aria-hidden="true">
            {icon}
          </span>
        )}
        <span>{label}</span>
      </dt>
      <dd className={styles.value}>
        <span className={muted ? styles.muted : undefined}>{value}</span>
        {source && (
          <>
            {' '}
            <SourceLink source={source} className={styles.source}>
              source
              {context && <span className="visually-hidden"> for {context}</span>}
            </SourceLink>
          </>
        )}
        {note && noteMode === 'inline' && <span className={styles.note}>{note}</span>}
        {note && noteMode === 'collapsed' && (
          <details className={styles.details}>
            <summary className={styles.summary}>
              Why this figure{context && <span className="visually-hidden"> ({context})</span>}
            </summary>
            <p className={styles.note}>{note}</p>
          </details>
        )}
      </dd>
    </div>
  );
}

export interface FactStatProps extends Omit<StatTileProps, 'value' | 'note' | 'source' | 'muted'> {
  fact: Fact;
  /** Project name for the hidden source context. */
  project?: string;
  /** Plain label text used in the hidden context when `label` is a node. */
  labelText?: string;
}

/** A `StatTile` fed from a typed fact: value, source, note and the not-documented state. */
export function FactStat({ fact, project, label, labelText, context, ...rest }: FactStatProps) {
  const text = labelText ?? (typeof label === 'string' ? label : undefined);
  const ctx = context ?? (text ? `${text.toLowerCase()}${project ? `, ${project}` : ''}` : project);
  return (
    <StatTile
      {...rest}
      label={label}
      value={fact.notDocumented ? <em>{factText(fact)}</em> : factText(fact)}
      muted={Boolean(fact.notDocumented)}
      source={fact.source}
      note={fact.note}
      context={ctx}
    />
  );
}
