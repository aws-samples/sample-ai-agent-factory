import { Link } from 'react-router-dom';
import { ExternalLink } from '../../components/ExternalLink';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { StageBadge } from '../../components/StageBadge';
import { projects } from '../../content/data';
import { FACT_LABELS, getFacts, type ProjectFacts } from '../../content/facts';
import { tree } from '../../content/links';
import { PATHS, projectPath } from '../../paths';
import { FactItem } from '../start/shared';
import styles from './ProjectsPage.module.css';

/** Facts shown on each directory card, each with its source link. */
const CHIP_KEYS: ReadonlyArray<keyof ProjectFacts> = ['regions', 'firstDeploy', 'cost'];

export function ProjectsPage() {
  return (
    <div className={styles.page}>
      <PageMeta
        title="Projects"
        description="Directory of the four AI Agent Factory projects: Workshop, Self-Service, MCP Gateway and Blueprint, with validated regions, first-deploy time and cost for each."
      />
      <PageHeader
        eyebrow="Directory"
        title="Projects"
        lead="Four projects, one per stage. Each card links to the project page on this site and to its source folder on GitHub."
      />

      <div className="container page-section">
        <ul className={styles.grid}>
          {projects.map((project) => {
            const projectFacts = getFacts(project.id);
            return (
              <li key={project.id} className={styles.card} data-stage={project.stage}>
                <StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />
                <h2 className={styles.cardTitle}>{project.name}</h2>
                <p className={styles.tagline}>{project.tagline}</p>
                <p>
                  <strong>Best for:</strong> {project.bestFor}
                </p>
                <dl className={styles.facts}>
                  {CHIP_KEYS.map((key) => (
                    <FactItem key={key} label={FACT_LABELS[key]} fact={projectFacts[key]} />
                  ))}
                </dl>
                <div className={styles.actions}>
                  <Link to={projectPath(project.id)} className={styles.primary}>
                    {project.shortName} project page
                  </Link>
                  <ExternalLink href={tree(project.folder)} className={styles.secondary}>
                    Source on GitHub
                  </ExternalLink>
                </div>
              </li>
            );
          })}
        </ul>
        <p className={styles.compare}>
          Not sure where to start? <Link to={PATHS.whichProject}>Compare the four projects side by side</Link>.
        </p>
      </div>
    </div>
  );
}
