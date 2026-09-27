import type { JourneyStage } from '../content/data';
import styles from './StageBadge.module.css';

export interface StageBadgeProps {
  stage: JourneyStage;
  /** Visible text, usually the stage label ("Learn") or "1. Learn". */
  label: string;
  /** filled: ink background with white text; outline: tinted border with ink text. */
  variant?: 'filled' | 'outline';
  className?: string;
}

/**
 * Stage badge that resolves its colour from the stage tokens. Inside an
 * `.on-dark` container the colours switch to the on-dark set automatically.
 */
export function StageBadge({ stage, label, variant = 'filled', className }: StageBadgeProps) {
  const classes = [styles.badge, styles[variant], className].filter(Boolean).join(' ');
  return (
    <span className={classes} data-stage={stage}>
      {label}
    </span>
  );
}
