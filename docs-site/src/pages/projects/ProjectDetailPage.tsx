import { Link, useParams } from 'react-router-dom';
import { Callout } from '../../components/Callout';
import { CodeBlock } from '../../components/CodeBlock';
import { ExternalLink } from '../../components/ExternalLink';
import { FactsTable } from '../../components/FactsTable';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { SourceLink } from '../../components/SourceLink';
import { StageBadge } from '../../components/StageBadge';
import { getProjectById, projects, type ProjectId } from '../../content/data';
import { docsForProject } from '../../content/docs';
import {
  FACT_KEYS,
  FACT_LABELS,
  factText,
  getAdvisories,
  getFacts,
  type Fact,
  type ProjectFacts,
} from '../../content/facts';
import { limitations, validated } from '../../content/limitations';
import {
  DEFAULT_BRANCH,
  REPO_URL,
  tree,
  WORKSHOP_LINK_LABEL,
  WORKSHOP_URL,
  WORKSHOPS_DISCOVER_LABEL,
  WORKSHOPS_DISCOVER_URL,
} from '../../content/links';
import { getQuickstarts, type QuickstartStep } from '../../content/quickstarts';
import { docLabel, docTitle } from '../../docs/docModules';
import { PATHS, projectPath } from '../../paths';
import { NotFoundPage } from '../NotFoundPage';
import { EvidenceSection, WhatItIsSection } from './sections/CommonSections';
import { getProjectExtras } from './sections/projectExtras';
import { InlineMarkdown, Section, Sources } from './sections/shared';
import styles from './ProjectDetailPage.module.css';

/** Facts shown as rows in the page header, built from the same objects as the At a glance table. */
const HERO_FACT_KEYS: ReadonlyArray<keyof ProjectFacts> = ['firstDeploy', 'regions', 'iac'];

function FactValue({ fact }: { fact: Fact }) {
  return (
    <>
      {fact.notDocumented ? <em>{factText(fact)}</em> : fact.value}
      {fact.source && (
        <>
          {' '}
          <SourceLink source={fact.source} />
        </>
      )}
      {fact.note && <span className={styles.note}> {fact.note}</span>}
    </>
  );
}

function HeroFacts({ projectFacts }: { projectFacts: ProjectFacts }) {
  return (
    <dl className={styles.heroFacts}>
      {HERO_FACT_KEYS.map((key) => {
        const fact = projectFacts[key];
        const label = FACT_LABELS[key];
        return (
          <div key={key} className={styles.heroFact}>
            <dt className={styles.heroFactLabel}>{label}</dt>
            <dd className={styles.heroFactValue}>
              {fact.notDocumented ? <em>{factText(fact)}</em> : fact.value}
              {fact.source && (
                <>
                  {' '}
                  <SourceLink source={fact.source} className={styles.heroSource}>
                    source<span className="visually-hidden"> for {label.toLowerCase()}</span>
                  </SourceLink>
                </>
              )}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

function StepBody({ step }: { step: QuickstartStep }) {
  const linkIsTitle = step.href !== undefined && (step.hrefLabel ?? step.href) === step.title;
  return (
    <>
      <p className={styles.stepTitle}>
        {linkIsTitle && step.href ? <ExternalLink href={step.href}>{step.title}</ExternalLink> : step.title}
      </p>
      {step.command && <CodeBlock code={step.command} language="bash" />}
      {step.note && <p>{step.note}</p>}
      {step.href && !linkIsTitle && (
        <p>
          <ExternalLink href={step.href}>{step.hrefLabel ?? step.href}</ExternalLink>
        </p>
      )}
      <Sources sources={[step.source]} />
    </>
  );
}

export function ProjectDetailPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const project = projectId ? getProjectById(projectId) : undefined;

  if (!project) {
    return <NotFoundPage />;
  }

  const id = project.id as ProjectId;
  const projectFacts = getFacts(id);
  const projectAdvisories = getAdvisories(id);
  const projectQuickstarts = getQuickstarts(id);
  const projectLimitations = limitations[id];
  const projectValidated = validated[id];
  const siteDocs = docsForProject(id);
  const extras = getProjectExtras(id);
  const otherProjects = projects.filter((p) => p.id !== id);
  const isWorkshop = id === 'workshop';
  // GitHub commit history for the folder, on the same branch the source links use.
  const commitsUrl = `${REPO_URL}/commits/${DEFAULT_BRANCH}/${project.folder}`;

  const actions = (
    <>
      {isWorkshop && (
        <ExternalLink href={WORKSHOP_URL} className={styles.actionPrimary}>
          {WORKSHOP_LINK_LABEL}
        </ExternalLink>
      )}
      <a href="#quickstart" className={isWorkshop ? styles.actionSecondary : styles.actionPrimary}>
        Quickstart
      </a>
      {isWorkshop && (
        <ExternalLink href={WORKSHOPS_DISCOVER_URL} className={styles.actionSecondary}>
          {WORKSHOPS_DISCOVER_LABEL}
        </ExternalLink>
      )}
      <ExternalLink href={tree(project.folder)} className={styles.actionSecondary}>
        Source on GitHub
      </ExternalLink>
    </>
  );

  return (
    <div className={styles.page}>
      <PageMeta title={project.name} description={project.description} />
      <PageHeader
        eyebrow={project.tagline}
        stage={project.stage}
        stageLabel={project.stageLabel}
        title={project.name}
        lead={project.description}
        meta={<HeroFacts projectFacts={projectFacts} />}
        actions={actions}
        figure={extras.hero}
      />

      <div className="container">
        <div className={styles.body}>
          <WhatItIsSection projectId={id} />

          <Section id="status" title="Status">
            <dl className={styles.statusList}>
              <div className={styles.statusItem}>
                <dt>{FACT_LABELS.version}</dt>
                <dd>
                  <FactValue fact={projectFacts.version} />
                </dd>
              </div>
              <div className={styles.statusItem}>
                <dt>{FACT_LABELS.status}</dt>
                <dd>
                  <FactValue fact={projectFacts.status} />
                </dd>
              </div>
            </dl>
            <p>
              <ExternalLink href={commitsUrl}>Recent changes: commit history for {project.folder} on GitHub</ExternalLink>
            </p>
            {projectAdvisories.length === 0 ? (
              <p className={styles.note}>No open advisories are tracked for this project.</p>
            ) : (
              <div className={styles.advisories}>
                {projectAdvisories.map((advisory) => (
                  <Callout key={advisory.id} kind="warning" title={`Advisory: GitHub issue #${advisory.issue}`}>
                    <p>{advisory.summary}</p>
                    <p>
                      <ExternalLink href={advisory.url}>{advisory.title}</ExternalLink>
                    </p>
                    {advisory.source && <Sources label="Evidence" sources={[advisory.source]} />}
                  </Callout>
                ))}
              </div>
            )}
          </Section>

          <Section id="at-a-glance" title="At a glance">
            <FactsTable
              caption={`${project.shortName} facts with a source for every value`}
              rows={FACT_KEYS.map((key) => ({ label: FACT_LABELS[key], fact: projectFacts[key] }))}
            />
            <p>
              <Link to={PATHS.whichProject}>Compare all four projects</Link>
            </p>
          </Section>

          <Section id="quickstart" title="Quickstart">
            {projectQuickstarts.map((quickstart) => (
              <div key={quickstart.id} className={styles.quickstart}>
                <h3>{quickstart.name}</h3>
                <p>
                  <strong>Prerequisites:</strong>{' '}
                  <Link to={`${PATHS.prerequisites}#${id}`}>{project.shortName} prerequisites on the Start pages</Link>.
                </p>
                <p>{quickstart.intro}</p>
                <p>
                  <strong>Expected time:</strong> <FactValue fact={quickstart.expectedTime} />
                </p>
                <ol className={styles.steps}>
                  {quickstart.steps.map((step) => (
                    <li key={step.title}>
                      <StepBody step={step} />
                    </li>
                  ))}
                </ol>
              </div>
            ))}
          </Section>

          {extras.sections}

          <Section id="architecture" title="Architecture and figures">
            {extras.figures}
            <p>
              <Link to={PATHS.conceptsArchitecture}>Architecture across all four projects</Link>
            </p>
          </Section>

          <Section id="limitations" title="Known limitations and support envelope">
            <p className={styles.note}>Each item is copied from the project README or docs without paraphrase.</p>
            <ul className={styles.limitations}>
              {projectLimitations.map((limitation) => (
                <li key={limitation.id}>
                  {limitation.title && (
                    <strong>
                      <InlineMarkdown text={limitation.title} />
                      {limitation.title.endsWith('.') ? '' : '.'}{' '}
                    </strong>
                  )}
                  <InlineMarkdown text={limitation.text} /> <SourceLink source={limitation.source} />
                </li>
              ))}
            </ul>
            <p>
              <Link to={PATHS.referenceSupportEnvelope}>Support envelope for all projects</Link>
            </p>
          </Section>

          <EvidenceSection projectId={id}>
            {projectValidated.length > 0 && (
              <div className={styles.validated}>
                <h3>Live-validated reference envelope</h3>
                <p className={styles.note}>Each item is copied from the README without paraphrase and links its source.</p>
                <ul className={styles.limitations}>
                  {projectValidated.map((item) => (
                    <li key={item.id}>
                      <InlineMarkdown text={item.text} /> <SourceLink source={item.source} />
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </EvidenceSection>

          <Section id="documentation" title="Documentation on this site">
            <ul className={styles.docList}>
              {siteDocs.map((doc) => {
                const label = docLabel(doc);
                const title = docTitle(doc);
                return (
                  <li key={doc.id}>
                    <Link to={doc.route}>{label}</Link>
                    {label !== title && <span className={styles.note}>: {title}</span>}
                  </li>
                );
              })}
              {extras.extraDocs}
            </ul>
          </Section>

          <Section id="teardown" title="Teardown">
            {projectQuickstarts.length === 0 ? (
              <p>
                <FactValue fact={projectFacts.teardown} />
              </p>
            ) : (
              projectQuickstarts.map((quickstart) => (
                <div key={quickstart.id}>
                  <h3>{quickstart.name}</h3>
                  <p>{quickstart.teardown.text}</p>
                  {quickstart.teardown.command && <CodeBlock code={quickstart.teardown.command} language="bash" />}
                  <Sources sources={[quickstart.teardown.source]} />
                </div>
              ))
            )}
            <p>
              <Link to={PATHS.costsAndCleanup}>Costs and cleanup for all projects</Link>
            </p>
          </Section>

          <nav aria-labelledby="other-projects-heading" className={styles.others}>
            <h2 id="other-projects-heading">Other projects</h2>
            <ul className={styles.othersList}>
              {otherProjects.map((other) => (
                <li key={other.id}>
                  <Link to={projectPath(other.id)} className={styles.otherLink}>
                    <StageBadge stage={other.stage} label={other.stageLabel} variant="outline" />
                    <span className={styles.otherName}>{other.shortName}</span>
                    <span className={styles.otherTagline}>{other.tagline}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </div>
    </div>
  );
}
