import type { CSSProperties, HTMLAttributes, ReactNode } from 'react';
import type { JourneyStage } from '../content/data';
import styles from './NumberedSteps.module.css';

export interface NumberedStepsProps extends Omit<HTMLAttributes<HTMLOListElement>, 'className' | 'children'> {
  /** Colours the counter circles; defaults to the text colour. */
  stage?: JourneyStage;
  /** First number (for a list that continues another). */
  start?: number;
  /** Draw a vertical line between the counters. */
  connector?: boolean;
  /** Tighter spacing for short lists (module lists, policy lists). */
  dense?: boolean;
  className?: string;
  children: ReactNode;
}

/**
 * Ordered list with stage-coloured counter circles. The number is drawn with a CSS
 * counter on `::before`, so screen readers announce the list position once, from
 * the `<ol>` itself. Use `NumberedSteps.Item` for each step.
 */
export function NumberedSteps({
  stage,
  start,
  connector = false,
  dense = false,
  className,
  children,
  ...rest
}: NumberedStepsProps) {
  const style = start && start > 1 ? ({ counterReset: `step ${start - 1}` } as CSSProperties) : undefined;
  return (
    <ol
      {...rest}
      className={[styles.steps, className].filter(Boolean).join(' ')}
      style={style}
      data-numbered-steps
      data-stage={stage}
      data-connector={connector ? '' : undefined}
      data-dense={dense ? '' : undefined}
    >
      {children}
    </ol>
  );
}

export interface NumberedStepItemProps extends Omit<HTMLAttributes<HTMLLIElement>, 'className' | 'children' | 'title'> {
  /** Step title, rendered in bold beside the counter. */
  title: ReactNode;
  /** Body: notes, code blocks, links. */
  children?: ReactNode;
  className?: string;
}

function Item({ title, children, className, ...rest }: NumberedStepItemProps) {
  return (
    <li {...rest} className={[styles.item, className].filter(Boolean).join(' ')}>
      <div className={styles.title}>{title}</div>
      {children && <div className={styles.body}>{children}</div>}
    </li>
  );
}

NumberedSteps.Item = Item;
