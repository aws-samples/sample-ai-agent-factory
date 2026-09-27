import type { ReactNode } from 'react';
import { SectionNav, type SectionNavItem } from '../../components/SectionNav';
import { SourceLink } from '../../components/SourceLink';
import { StageBadge } from '../../components/StageBadge';
import type { Project } from '../../content/data';
import { factText, type Fact } from '../../content/facts';
import { PATHS } from '../../paths';
import styles from './start.module.css';

/**
 * A fact value followed by a small "source" link and, when the fact carries one,
 * its note in muted text. The link's accessible name names the fact and, when
 * given, the project as written ("source for cost, Self-Service") so repeated
 * links stay distinguishable. Facts marked `notDocumented` render the shared label.
 */
export function FactValue({
  fact,
  label,
  project,
  compactNote = false,
}: {
  fact: Fact;
  label: string;
  project?: string;
  /** Show the note only from 640 px up (Home tiles, where the caveat repeats on the table and project pages). */
  compactNote?: boolean;
}) {
  return (
    <>
      {fact.notDocumented ? <em className={styles.notDocumented}>{factText(fact)}</em> : factText(fact)}
      {fact.source && (
        <>
          {' '}
          <SourceLink source={fact.source} className={styles.sourceSmall}>
            source
            <span className="visually-hidden">
              {' '}
              for {label.toLowerCase()}
              {project ? `, ${project}` : ''}
            </span>
          </SourceLink>
        </>
      )}
      {fact.note && (
        <span className={compactNote ? `${styles.factNote} ${styles.factNoteCompact}` : styles.factNote}>{fact.note}</span>
      )}
    </>
  );
}

/** One `dt`/`dd` pair for a compact facts list inside a tile. */
export function FactItem({
  label,
  fact,
  project,
  compactNote = false,
}: {
  label: string;
  fact: Fact;
  project?: string;
  compactNote?: boolean;
}) {
  return (
    <div className={styles.fact}>
      <dt className={styles.factLabel}>{label}</dt>
      <dd className={styles.factValue}>
        <FactValue fact={fact} label={label} project={project} compactNote={compactNote} />
      </dd>
    </div>
  );
}

/** Section heading for a project: stage badge plus the full project name. */
export function ProjectHeading({ project, id }: { project: Project; id: string }) {
  return (
    <h2 id={id} className={styles.projectHeading}>
      <StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />
      <span>{project.name}</span>
    </h2>
  );
}

/** Small "source" link for a step or paragraph, with hidden context. */
export function SmallSource({ source, context }: { source: Fact['source'] & object; context: string }) {
  return (
    <SourceLink source={source} className={styles.sourceSmall}>
      source<span className="visually-hidden"> for {context}</span>
    </SourceLink>
  );
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

/** In-page jump links to sections further down the same page. */
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
    <nav aria-label={label} className={styles.jump}>
      {lead && <span className={styles.jumpLabel}>{lead}</span>}
      <ul className={styles.chips}>
        {items.map(({ id, label: itemLabel }) => (
          <li key={id}>
            <a href={`#${id}`} className={styles.chip}>
              {itemLabel}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
