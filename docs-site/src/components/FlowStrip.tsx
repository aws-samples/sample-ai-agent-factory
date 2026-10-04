import type { ReactNode } from 'react';
import type { JourneyStage } from '../content/data';
import type { Source } from '../content/facts';
import { Card } from './Card';
import { SmallSource } from './Sources';
import styles from './FlowStrip.module.css';

export interface FlowStripItem {
  id: string;
  /** Step or flow name, shown in bold. */
  name: string;
  /** One or two sentences under the name. */
  detail: string;
  /** Optional source for this item, rendered as a small "source" link. */
  source?: Source;
}

export interface FlowStripProps {
  items: FlowStripItem[];
  /** Colours the index circles. */
  stage: JourneyStage;
  /** Accessible name of the list, for example "Four governed flows". */
  label: string;
  /** Optional caption under the strip (sources, a note). */
  caption?: ReactNode;
  className?: string;
}

/**
 * A numbered strip of small cards: an HTML rendering of a flow or request path.
 * Index circles take the stage colour; the number is decorative because the list
 * itself is ordered.
 */
export function FlowStrip({ items, stage, label, caption, className }: FlowStripProps) {
  return (
    <div className={[styles.strip, className].filter(Boolean).join(' ')} data-flow-strip data-stage={stage}>
      <ol className={styles.list} aria-label={label}>
        {items.map((item, index) => (
          <Card as="li" key={item.id} padding="sm" className={styles.step}>
            <span className={styles.index} aria-hidden="true">
              {index + 1}
            </span>
            <span className={styles.name}>{item.name}</span>
            <span className={styles.detail}>{item.detail}</span>
            {item.source && <SmallSource source={item.source} context={item.name} className={styles.source} />}
          </Card>
        ))}
      </ol>
      {caption && <p className={styles.caption}>{caption}</p>}
    </div>
  );
}
