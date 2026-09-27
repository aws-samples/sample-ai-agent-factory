import type { ReactNode } from 'react';
import type { JourneyStage } from '../content/data';
import { STAGE_LABELS } from '../stage';
import { StageBadge } from './StageBadge';
import styles from './PageHeader.module.css';

export interface PageHeaderProps {
  /** Short section label above the title, for example "Start" or "Reference". */
  eyebrow?: ReactNode;
  /** Page heading, rendered as the page's single h1. */
  title: ReactNode;
  /** One or two sentences saying what the page answers. */
  lead?: ReactNode;
  /** Fact chips or other small metadata rendered under the lead, still on the dark band. */
  meta?: ReactNode;
  /** Journey stage; renders a filled StageBadge above the h1. */
  stage?: JourneyStage;
  /** Badge text when `stage` is set. Defaults to the stage name ("Learn"). */
  stageLabel?: string;
  /** Optional call-to-action links; they render on the dark band, so use on-dark aware styles. */
  actions?: ReactNode;
  /** Optional figure (diagram, illustration). Stacks under the copy on small screens and
   *  becomes a second column from 1024px up, all on the same dark band. */
  figure?: ReactNode;
  className?: string;
}

/**
 * The dark page-header band: eyebrow, optional stage badge, h1, lead paragraph,
 * optional meta row (fact chips), optional actions row and optional figure slot. It is the only dark band a page
 * needs; sections below it sit on the light surface. Pages still render <PageMeta>.
 */
export function PageHeader({
  eyebrow,
  title,
  lead,
  meta,
  stage,
  stageLabel,
  actions,
  figure,
  className,
}: PageHeaderProps) {
  const innerClass = [styles.inner, figure ? styles.hasFigure : undefined].filter(Boolean).join(' ');
  return (
    <header className={[styles.header, 'on-dark', className].filter(Boolean).join(' ')}>
      <div className={`container ${innerClass}`}>
        <div className={styles.copy}>
          {eyebrow && <p className={styles.eyebrow}>{eyebrow}</p>}
          {stage && (
            <StageBadge stage={stage} label={stageLabel ?? STAGE_LABELS[stage]} className={styles.badge} />
          )}
          <h1 className={styles.title}>{title}</h1>
          {lead && <p className={styles.lead}>{lead}</p>}
          {meta && <div className={styles.meta}>{meta}</div>}
          {actions && <div className={styles.actions}>{actions}</div>}
        </div>
        {figure && <div className={styles.figure}>{figure}</div>}
      </div>
    </header>
  );
}
