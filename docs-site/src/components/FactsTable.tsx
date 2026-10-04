import { useId, useRef } from 'react';
import { NOT_DOCUMENTED_LABEL, type Fact } from '../content/facts';
import { SourceLink } from './SourceLink';
import { useIsScrollable } from './useIsScrollable';
import styles from './FactsTable.module.css';

export interface FactRow {
  label: string;
  fact: Fact;
}

export interface FactsTableProps {
  /** Visible table caption; it also names the scrollable region. Defaults to "Facts". */
  caption?: string;
  rows: FactRow[];
  className?: string;
}

/**
 * Three-column table: fact, value (or "not documented"), source link.
 *
 * The scroll wrapper is a labelled region (named by the caption) so keyboard users can
 * reach it and assistive technology announces what it holds.
 */
export function FactsTable({ caption = 'Facts', rows, className }: FactsTableProps) {
  const captionId = useId();
  const wrapperRef = useRef<HTMLDivElement>(null);
  const scrollable = useIsScrollable(wrapperRef);
  return (
    <div className={`${styles.wrapper} ${className ?? ''}`.trim()} ref={wrapperRef} role="region" aria-labelledby={captionId} tabIndex={scrollable ? 0 : undefined}>
      <table className={styles.table}>
        <caption id={captionId} className={styles.caption}>
          {caption}
        </caption>
        <thead>
          <tr>
            <th scope="col">Fact</th>
            <th scope="col">Value</th>
            <th scope="col">Source</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ label, fact }) => (
            <tr key={label}>
              <th scope="row">{label}</th>
              <td>
                {fact.notDocumented ? <em className={styles.notDocumented}>{NOT_DOCUMENTED_LABEL}</em> : fact.value}
                {fact.note && <span className={styles.note}> {fact.note}</span>}
              </td>
              <td>{fact.source ? <SourceLink source={fact.source} /> : <span className={styles.note}>none</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
