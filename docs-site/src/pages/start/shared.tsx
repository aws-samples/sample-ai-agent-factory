import type { ReactNode } from 'react';
import { ChipNav } from '../../components/ChipNav';
import { SectionHeading } from '../../components/SectionHeading';
import { SectionNav, type SectionNavItem } from '../../components/SectionNav';
import { SmallSource as SharedSmallSource } from '../../components/Sources';
import { StageBadge } from '../../components/StageBadge';
import { FactStat } from '../../components/StatTile';
import type { Project } from '../../content/data';
import { factText, type Fact } from '../../content/facts';
import { PATHS } from '../../paths';
import styles from './start.module.css';

/**
 * A fact value in running text or a table cell: the value, a small "source" link
 * and, when the fact carries one, its note in muted text. The link's accessible
 * name names the fact and, when given, the project as written ("source for cost,
 * Self-Service") so repeated links stay distinguishable. Facts marked
 * `notDocumented` render the shared label as a muted pill. `noteMode="collapsed"` puts the
 * note behind a native disclosure so dense table cells show the value and its source first.
 */
export function FactValue({
  fact,
  label,
  project,
  noteMode = 'inline',
}: {
  fact: Fact;
  label: string;
  project?: string;
  noteMode?: 'inline' | 'collapsed';
}) {
  const context = `${label.toLowerCase()}${project ? `, ${project}` : ''}`;
  return (
    <>
      {fact.notDocumented ? <em className={styles.notDocumented}>{factText(fact)}</em> : factText(fact)}
      {fact.source && (
        <>
          {' '}
          <SharedSmallSource source={fact.source} context={context} />
        </>
      )}
      {fact.note && noteMode === 'inline' && <span className={styles.factNote}>{fact.note}</span>}
      {fact.note && noteMode === 'collapsed' && (
        <details className={styles.factDetails}>
          <summary className={styles.factSummary}>
            Why this figure<span className="visually-hidden"> ({context})</span>
          </summary>
          <p className={styles.factNote}>{fact.note}</p>
        </details>
      )}
    </>
  );
}

/**
 * One `dt`/`dd` pair for a compact facts list inside a tile. Delegates to the
 * shared `FactStat`; `compactNote` (Home tiles) collapses the caveat behind the
 * "Why this figure" disclosure.
 */
export function FactItem({
  label,
  fact,
  project,
  compactNote = false,
  icon,
}: {
  label: string;
  fact: Fact;
  project?: string;
  compactNote?: boolean;
  icon?: ReactNode;
}) {
  return <FactStat label={label} fact={fact} project={project} icon={icon} noteMode={compactNote ? 'collapsed' : 'inline'} />;
}

/** Section heading for a project: stage badge plus the full project name. */
export function ProjectHeading({ project, id }: { project: Project; id: string }) {
  return (
    <SectionHeading
      id={id}
      title={project.name}
      badge={<StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />}
    />
  );
}

/** Stage badge used beside a project heading ("1. Learn"). */
export function ProjectBadge({ project }: { project: Project }) {
  return <StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />;
}

/** Small "source" link for a step or paragraph, with hidden context. */
export function SmallSource({ source, context }: { source: Fact['source'] & object; context: string }) {
  return <SharedSmallSource source={source} context={context} />;
}

const START_PAGES: SectionNavItem[] = [
  { to: PATHS.start, label: 'Get started' },
  { to: PATHS.whichProject, label: 'Which project fits?' },
  { to: PATHS.prerequisites, label: 'Prerequisites' },
  { to: PATHS.costsAndCleanup, label: 'Costs and cleanup' },
  { to: PATHS.faq, label: 'FAQ' },
];

/** Chip navigation between the five pages of the Start section (the shared SectionNav marks the current page). */
export function StartNav() {
  return <SectionNav label="Start section" items={START_PAGES} />;
}

/** In-page jump links to sections further down the same page, as a chip rail. */
export function JumpLinks({
  label,
  items,
  lead,
}: {
  label: string;
  items: { id: string; label: ReactNode }[];
  lead?: string;
}) {
  return (
    <ChipNav
      label={label}
      lead={lead}
      overflow="wrap"
      items={items.map(({ id, label: itemLabel }) => ({ href: `#${id}`, label: itemLabel }))}
    />
  );
}
