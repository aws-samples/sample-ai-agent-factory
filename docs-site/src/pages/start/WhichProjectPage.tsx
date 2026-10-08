import { Link } from 'react-router-dom';
import { ArrowRight, Timer } from 'lucide-react';
import { Card } from '../../components/Card';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { ResponsiveTable } from '../../components/ResponsiveTable';
import { Section } from '../../components/Section';
import { SectionHeading } from '../../components/SectionHeading';
import { StageBadge } from '../../components/StageBadge';
import { FactStat } from '../../components/StatTile';
import { getProjectById, projects, type Project } from '../../content/data';
import { FACT_KEYS, FACT_LABELS, getFacts, type ProjectFacts } from '../../content/facts';
import { getSitePath, roleGuidance, sitePaths, timeGuidance } from '../../content/tracks';
import { PATHS, projectPath } from '../../paths';
import { STAGE_ICONS } from '../../stage';
import { factIcon } from './factIcons';
import { FactValue, SmallSource, StartNav } from './shared';
import styles from './start.module.css';

/**
 * Fact rows in display order, after the stage and "best for" rows from data.ts.
 * The list is driven by FACT_KEYS: a preferred order first (keys that facts.ts
 * does not define yet are skipped until they exist), then any other fact key
 * except the per-project details that belong on the Prerequisites and project
 * pages.
 */
const PREFERRED_ORDER: readonly string[] = [
  'regions',
  'accountTopology',
  'iac',
  'deploys',
  'firstDeploy',
  'handsOnTime',
  'cost',
  'teardown',
  'status',
];
const NOT_IN_TABLE: readonly string[] = ['defaultRegion', 'authAndPolicy', 'version'];
const isFactKey = (key: string): key is keyof ProjectFacts => (FACT_KEYS as readonly string[]).includes(key);
const FACT_ROWS: ReadonlyArray<keyof ProjectFacts> = [
  ...PREFERRED_ORDER.filter(isFactKey),
  ...FACT_KEYS.filter((key) => !PREFERRED_ORDER.includes(key) && !NOT_IN_TABLE.includes(key)),
];

/** One column per project, with its facts resolved once. */
const columns = projects.map((project) => ({ project, facts: getFacts(project.id) }));

function StageIcon({ project, size = 16 }: { project: Project; size?: number }) {
  const Icon = STAGE_ICONS[project.stage];
  return <Icon size={size} aria-hidden="true" className={styles.stageIcon} />;
}

/** The comparison as a table: columns are the projects, rows the facts. Shown from 640 px up. */
function ComparisonTable() {
  return (
    <div className={styles.matrixDesktop}>
      <ResponsiveTable className={styles.matrix}>
        <caption>Comparison of the four projects. Each fact links to its source in the repository.</caption>
        <thead>
          <tr>
            <th scope="col">Fact</th>
            {columns.map(({ project }) => (
              <th scope="col" key={project.id} data-stage={project.stage}>
                <span className={styles.matrixProject}>
                  <Link to={projectPath(project.id)} className={styles.matrixName}>
                    <StageIcon project={project} />
                    <span>{project.name}</span>
                  </Link>
                  <StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">Stage</th>
            {columns.map(({ project }) => (
              <td key={project.id}>
                {project.stageNumber}. {project.stageLabel}
              </td>
            ))}
          </tr>
          <tr className={styles.bestForRow}>
            <th scope="row">Best for</th>
            {columns.map(({ project }) => (
              <td key={project.id}>{project.bestFor}</td>
            ))}
          </tr>
          {FACT_ROWS.map((key) => (
            <tr key={key}>
              <th scope="row">
                <span className={styles.rowLabel}>
                  {factIcon(key)}
                  <span>{FACT_LABELS[key]}</span>
                </span>
              </th>
              {columns.map(({ project, facts }) => (
                <td key={project.id}>
                  <FactValue fact={facts[key]} label={FACT_LABELS[key]} project={project.shortName} noteMode="collapsed" />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </ResponsiveTable>
    </div>
  );
}

/** The same comparison as one card per project, for screens below 640 px. */
function ComparisonCards() {
  return (
    <ul className={styles.matrixCards} aria-label="Comparison of the four projects, one card per project">
      {columns.map(({ project, facts }) => (
        <Card as="li" key={project.id} variant="accent" stage={project.stage} className={styles.compareCard}>
          <div className={styles.tileHead}>
            <StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />
          </div>
          <h3 className={styles.compareTitle}>
            <Link to={projectPath(project.id)} className={styles.matrixName}>
              <StageIcon project={project} />
              <span>{project.name}</span>
            </Link>
          </h3>
          <p className={styles.tileText}>
            <strong>Best for:</strong> {project.bestFor}
          </p>
          <dl className={styles.compareFacts}>
            {FACT_ROWS.map((key) => (
              <FactStat
                key={key}
                label={FACT_LABELS[key]}
                fact={facts[key]}
                project={project.shortName}
                icon={factIcon(key)}
                noteMode="collapsed"
              />
            ))}
          </dl>
        </Card>
      ))}
    </ul>
  );
}

export function WhichProjectPage() {
  return (
    <>
      <PageMeta
        title="Which project fits?"
        description="Compare the four AI Agent Factory projects by stage, audience, validated regions, account topology, tooling, first deploy, hands-on time, cost, teardown and status, every value linked to its source."
      />
      <PageHeader
        eyebrow="Start"
        title="Which project fits?"
        lead="One table for the whole decision across the four samples for agentic AI on Amazon Bedrock and Amazon Bedrock AgentCore: stage, audience, validated regions, topology, tooling, time, cost, teardown and status. Every value links to the repository line it comes from, and gaps say so."
      />
      <div className="container">
        <StartNav />

        <Section
          id="compare"
          title="Compare the four projects"
          flush
          lead="Columns are the projects and rows are the facts. On a narrow screen, scroll the table sideways; the fact labels stay in view."
        >
          <ComparisonTable />
          <ComparisonCards />
          <p className={`${styles.meta} ${styles.matrixHint}`}>
            Prerequisites per project are on the <Link to={PATHS.prerequisites}>Prerequisites</Link> page; cost notes
            and teardown procedures are under <Link to={PATHS.costsAndCleanup}>Costs and cleanup</Link>.
          </p>
        </Section>

        <Section
          id="paths"
          title="Four paths through the repository"
          lead="Each path starts on one project and says who it is for, what you have at the end, and how long the first result takes."
        >
          <ul className={styles.pathGrid}>
            {sitePaths.map((path) => {
              const first = getProjectById(path.projects[0]);
              if (!first) return null;
              return (
                <Card
                  as="li"
                  key={path.id}
                  variant="accent"
                  stage={first.stage}
                  interactive
                  reveal
                  className={styles.pathCard}
                >
                  <div className={styles.tileHead}>
                    <StageBadge stage={first.stage} label={`${first.stageNumber}. ${first.stageLabel}`} />
                    <Link to={path.startRoute} className={styles.tileLink}>
                      Start
                      <span className="visually-hidden">: {path.name}</span>
                      <ArrowRight size={16} aria-hidden="true" />
                    </Link>
                  </div>
                  <h3 className={styles.tileTitle}>{path.name}</h3>
                  <p className={styles.tileText}>
                    <strong>Who:</strong> {path.who}
                  </p>
                  <p className={styles.tileText}>
                    <strong>What you get:</strong> {path.whatYouGet}{' '}
                    <SmallSource source={path.source} context={`what you get: ${path.name}`} />
                  </p>
                  <dl className={styles.pathFacts}>
                    <FactStat
                      label="Time to first result"
                      fact={path.timeToFirstResult}
                      icon={<Timer size={14} aria-hidden="true" />}
                    />
                  </dl>
                </Card>
              );
            })}
          </ul>
        </Section>

        <div className={`${styles.guidance} ${styles.twoCol}`}>
          <section aria-labelledby="role-heading">
            <SectionHeading id="role-heading" title="By role" />
            <ul className={styles.plainList}>
              {roleGuidance.map((guidance) => {
                const path = getSitePath(guidance.pathId);
                if (!path) return null;
                return (
                  <li key={guidance.role}>
                    <strong>{guidance.role}:</strong> <Link to={path.startRoute}>{path.name}</Link>. {guidance.why}
                  </li>
                );
              })}
            </ul>
          </section>
          <section aria-labelledby="time-heading">
            <SectionHeading id="time-heading" title="By time available" />
            <ul className={styles.plainList}>
              {timeGuidance.map((guidance) => {
                const path = getSitePath(guidance.pathId);
                if (!path) return null;
                return (
                  <li key={guidance.available}>
                    <strong>{guidance.available}:</strong> <Link to={path.startRoute}>{path.name}</Link>.{' '}
                    <span className={styles.meta}>
                      Basis: <FactValue fact={guidance.basis} label="this recommendation" />
                    </span>
                  </li>
                );
              })}
            </ul>
          </section>
        </div>
      </div>
    </>
  );
}
