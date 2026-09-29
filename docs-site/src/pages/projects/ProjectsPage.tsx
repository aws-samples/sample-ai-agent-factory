import { Link } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { StageBadge } from '../../components/StageBadge';
import { StageJourney } from '../../components/StageJourney';
import { FactStat } from '../../components/StatTile';
import { projects } from '../../content/data';
import { FACT_LABELS, getFacts, type ProjectFacts } from '../../content/facts';
import { tree } from '../../content/links';
import { PATHS, projectPath } from '../../paths';
import { STAGE_ICONS } from '../../stage';
import { factIcon } from '../start/factIcons';
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
        meta={<StageJourney label="Journey stages" size="sm" />}
      />

      <div className={`container ${styles.body}`}>
        <ul className={styles.grid}>
          {projects.map((project) => {
            const projectFacts = getFacts(project.id);
            const Icon = STAGE_ICONS[project.stage];
            return (
              <Card
                as="li"
                key={project.id}
                variant="accent"
                stage={project.stage}
                interactive
                reveal
                padding="lg"
                className={styles.projectCard}
              >
                <div className={styles.head}>
                  <span className={styles.stageIcon} aria-hidden="true">
                    <Icon size={20} />
                  </span>
                  <StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />
                </div>
                <h2 className={styles.cardTitle}>{project.name}</h2>
                <p className={styles.tagline}>{project.tagline}</p>
                <p>
                  <strong>Best for:</strong> {project.bestFor}
                </p>
                <dl className={styles.factList}>
                  {CHIP_KEYS.map((key) => (
                    <FactStat
                      key={key}
                      label={FACT_LABELS[key]}
                      fact={projectFacts[key]}
                      project={project.shortName}
                      icon={factIcon(key)}
                      noteMode="collapsed"
                    />
                  ))}
                </dl>
                <div className={styles.actions}>
                  <Button to={projectPath(project.id)} iconEnd={<ArrowRight size={16} />}>
                    {project.shortName} project page
                  </Button>
                  <Button href={tree(project.folder)} external variant="secondary">
                    Source on GitHub
                  </Button>
                </div>
              </Card>
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
