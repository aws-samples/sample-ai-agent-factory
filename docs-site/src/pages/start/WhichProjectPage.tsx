import { Link } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { ResponsiveTable } from '../../components/ResponsiveTable';
import { StageBadge } from '../../components/StageBadge';
import { getProjectById, projects } from '../../content/data';
import { FACT_KEYS, FACT_LABELS, getFacts, type ProjectFacts } from '../../content/facts';
import { getSitePath, roleGuidance, sitePaths, timeGuidance } from '../../content/tracks';
import { PATHS, projectPath } from '../../paths';
import { FactItem, FactValue, SmallSource, StartNav } from './shared';
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

        <section className={styles.projectSection} aria-labelledby="compare-heading">
          <h2 id="compare-heading">Compare the four projects</h2>
          <p className={styles.lead}>
            Columns are the projects and rows are the facts. On a narrow screen, scroll the table sideways; the fact
            labels stay in view.
          </p>
          <ResponsiveTable className={styles.matrix}>
            <caption>Comparison of the four projects. Each fact links to its source in the repository.</caption>
            <thead>
              <tr>
                <th scope="col">Fact</th>
                {columns.map(({ project }) => (
                  <th scope="col" key={project.id}>
                    <span className={styles.matrixProject}>
                      <Link to={projectPath(project.id)}>{project.name}</Link>
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
              <tr>
                <th scope="row">Best for</th>
                {columns.map(({ project }) => (
                  <td key={project.id}>{project.bestFor}</td>
                ))}
              </tr>
              {FACT_ROWS.map((key) => (
                <tr key={key}>
                  <th scope="row">{FACT_LABELS[key]}</th>
                  {columns.map(({ project, facts }) => (
                    <td key={project.id}>
                      <FactValue fact={facts[key]} label={FACT_LABELS[key]} project={project.shortName} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </ResponsiveTable>
          <p className={`${styles.meta} ${styles.matrixHint}`}>
            Prerequisites per project are on the <Link to={PATHS.prerequisites}>Prerequisites</Link> page; cost notes
            and teardown procedures are under <Link to={PATHS.costsAndCleanup}>Costs and cleanup</Link>.
          </p>
        </section>

        <section className={styles.projectSection} aria-labelledby="paths-heading">
          <h2 id="paths-heading">Four paths through the repository</h2>
          <p className={styles.lead}>
            Each path starts on one project and says who it is for, what you have at the end, and how long the first
            result takes.
          </p>
          <ul className={`${styles.tileGrid} ${styles.tileGrid4}`}>
            {sitePaths.map((path) => {
              const first = getProjectById(path.projects[0]);
              if (!first) return null;
              return (
                <li key={path.id} className={styles.tile} data-stage={first.stage} data-reveal data-lift>
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
                  <dl className={styles.facts}>
                    <FactItem label="Time to first result" fact={path.timeToFirstResult} />
                  </dl>
                </li>
              );
            })}
          </ul>
        </section>

        <div className={`${styles.projectSection} ${styles.twoCol}`}>
          <section aria-labelledby="role-heading">
            <h2 id="role-heading">By role</h2>
            <ul className={`${styles.plainList} ${styles.subSection}`}>
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
            <h2 id="time-heading">By time available</h2>
            <ul className={`${styles.plainList} ${styles.subSection}`}>
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
