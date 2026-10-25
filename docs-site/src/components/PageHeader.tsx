import type { ReactNode } from 'react';
import type { JourneyStage } from '../content/data';
import { STAGE_LABELS } from '../stage';
import { HeaderGlow, type HeaderHue } from './HeaderGlow';
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
  /** Decorative layer behind the copy and figure, filling the band (for example the Home
   *  constellation). Rendered first, absolutely positioned and `pointer-events: none`, above the
   *  band's midnight background; the band looks identical when the slot is empty or JavaScript is
   *  off. Only interactive descendants that opt back into pointer events (the pause control) are
   *  reachable. */
  backdrop?: ReactNode;
  /** Hue of the default gradient backdrop when no `backdrop` is passed. Defaults to the stage, else blue. */
  hue?: HeaderHue;
  /** start: copy left-aligned (every hub page); center: copy centred (Home). */
  align?: 'start' | 'center';
  /** default: full band; compact: tighter band with a smaller title (doc pages). */
  variant?: 'default' | 'compact';
  /** Breadcrumb trail rendered above the eyebrow, on the band. */
  breadcrumbs?: ReactNode;
  className?: string;
}

/**
 * The dark page-header band: eyebrow, optional stage badge, h1, lead paragraph,
 * optional meta row (fact chips), optional actions row and optional figure slot. It is the only dark band a page
 * needs; sections below it sit on the light surface. Pages still render <PageMeta>.
 *
 * On load the eyebrow, badge, title, lead, meta, actions and figure rise 12px with a
 * short stagger (title first). The animation is CSS-only, applies inside
 * `@media (prefers-reduced-motion: no-preference)` and runs once, so reduced-motion visitors and
 * no-JavaScript renders see the final state immediately and nothing shifts layout.
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
  backdrop,
  hue,
  align = 'start',
  variant = 'default',
  breadcrumbs,
  className,
}: PageHeaderProps) {
  const innerClass = [styles.inner, figure ? styles.hasFigure : undefined].filter(Boolean).join(' ');
  const glowHue: HeaderHue = hue ?? stage ?? 'scale';
  return (
    <header
      className={[styles.header, 'on-dark', className].filter(Boolean).join(' ')}
      data-page-header
      data-align={align}
      data-variant={variant}
      data-stage={stage}
    >
      <div className={styles.backdrop}>{backdrop !== undefined ? backdrop : <HeaderGlow hue={glowHue} />}</div>
      <div className={`container ${innerClass}`}>
        <div className={styles.copy} data-copy>
          {breadcrumbs && <div className={styles.breadcrumbs}>{breadcrumbs}</div>}
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
