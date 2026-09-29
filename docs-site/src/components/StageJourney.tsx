import { Link } from 'react-router-dom';
import { projects } from '../content/data';
import { STAGE_ICONS } from '../stage';
import styles from './StageJourney.module.css';

export interface StageJourneyProps {
  /** Accessible name of the list. */
  label?: string;
  /** Show the project short name under the stage. */
  showProjects?: boolean;
  /** compact: smaller pills for narrow places (Projects index header). */
  size?: 'md' | 'sm';
  className?: string;
}

/**
 * The four-stage journey as a row of linked pills: icon, "1. Learn", project name,
 * joined by arrows drawn in CSS so the list still has exactly four items. Reads the
 * projects in stage order from the content model.
 */
export function StageJourney({ label = 'Journey stages', showProjects = true, size = 'md', className }: StageJourneyProps) {
  const ordered = [...projects].sort((a, b) => a.stageNumber - b.stageNumber);
  return (
    <ol className={[styles.journey, className].filter(Boolean).join(' ')} aria-label={label} data-stage-journey data-size={size}>
      {ordered.map((project) => {
        const Icon = STAGE_ICONS[project.stage];
        return (
          <li key={project.id} className={styles.item} data-stage={project.stage}>
            <Link to={project.route} className={styles.pill}>
              <span className={styles.icon} aria-hidden="true">
                <Icon size={size === 'sm' ? 14 : 18} />
              </span>
              <span className={styles.text}>
                <span className={styles.stage}>
                  {project.stageNumber}. {project.stageLabel}
                </span>
                {showProjects && <span className={styles.project}>{project.shortName}</span>}
              </span>
            </Link>
          </li>
        );
      })}
    </ol>
  );
}
