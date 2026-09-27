import { Link } from 'react-router-dom';
import { CodeBlock } from '../../components/CodeBlock';
import { ExternalLink } from '../../components/ExternalLink';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { SourceLink } from '../../components/SourceLink';
import { projects, type Project } from '../../content/data';
import { ISSUES_URL, REPO_URL, VULN_REPORT_URL } from '../../content/links';
import { getQuickstarts, type Quickstart, type QuickstartStep } from '../../content/quickstarts';
import { PATHS, projectPath } from '../../paths';
import { FactValue, JumpLinks, ProjectHeading, SmallSource, StartNav } from './shared';
import styles from './start.module.css';

/** Root README wording for the clone step; the folder line is per project. */
const ROOT_QUICK_START = { file: 'README.md', heading: 'Quick Start' };
const CASE_SENSITIVE_NOTE = 'Folder names are exact and case-sensitive.';

/** How many steps this page shows before handing over to the project page. */
const STEPS_SHOWN = 2;

/** True for the quickstart step that clones the repository and enters the folder. */
function isCloneStep(step: QuickstartStep): boolean {
  return step.title.startsWith('Clone the repository');
}

/** Clone and case-sensitive `cd` lines for one project folder, as spelled in the repository. */
function cloneBlock(project: Project): QuickstartStep {
  return {
    title: 'Clone the repository and enter the project folder',
    command: `git clone ${REPO_URL}.git\ncd sample-ai-agent-factory/${project.folder}`,
    note: CASE_SENSITIVE_NOTE,
    source: ROOT_QUICK_START,
  };
}

/**
 * The clone step for a quickstart: the README's own clone step when it has one,
 * otherwise a generated one for any path that runs shell commands. Link-only
 * paths (an AWS event with a provisioned account) have nothing to clone.
 */
function cloneStepFor(project: Project, quickstart: Quickstart): QuickstartStep | undefined {
  const own = quickstart.steps.find(isCloneStep);
  if (own) return own;
  return quickstart.steps.some((step) => step.command) ? cloneBlock(project) : undefined;
}

function StepItem({ step }: { step: QuickstartStep }) {
  const linkIsTitle = Boolean(step.href) && step.hrefLabel === step.title;
  return (
    <li className={styles.step}>
      <p className={styles.stepTitle}>
        {linkIsTitle && step.href ? <ExternalLink href={step.href}>{step.title}</ExternalLink> : step.title}
        <SmallSource source={step.source} context={`step: ${step.title}`} />
      </p>
      {step.command && <CodeBlock code={step.command} language="bash" />}
      {step.note && <p className={styles.stepNote}>{step.note}</p>}
      {step.href && !linkIsTitle && (
        <p className={styles.stepNote}>
          <ExternalLink href={step.href}>{step.hrefLabel ?? 'Open the README section'}</ExternalLink>
        </p>
      )}
    </li>
  );
}

/**
 * The first minutes of one quickstart: clone and cd lines, expected time, the
 * first two steps, then links to the full quickstart on the project page, the
 * prerequisites and the teardown. The project page is the canonical home of
 * every step and of the teardown.
 */
function QuickstartBlock({ project, quickstart }: { project: Project; quickstart: Quickstart }) {
  const clone = cloneStepFor(project, quickstart);
  const firstSteps = quickstart.steps.filter((step) => !isCloneStep(step)).slice(0, STEPS_SHOWN);
  const remaining = quickstart.steps.length - firstSteps.length - (clone && quickstart.steps.includes(clone) ? 1 : 0);
  return (
    <div className={styles.subSection}>
      <h3>{quickstart.name}</h3>
      <p>{quickstart.intro}</p>
      <p className={styles.meta}>
        <strong>Expected time:</strong>{' '}
        <FactValue fact={quickstart.expectedTime} label="expected time" project={quickstart.name} />
      </p>
      <ol className={styles.steps}>
        {clone && <StepItem step={clone} />}
        {firstSteps.map((step) => (
          <StepItem key={step.title} step={step} />
        ))}
      </ol>
      <ul className={styles.linkRow}>
        <li>
          <Link to={`${projectPath(project.id)}#quickstart`}>
            All steps on the {project.shortName} page
            {remaining > 0 && <span className="visually-hidden"> ({remaining} more)</span>}
          </Link>
        </li>
        <li>
          <Link to={`${PATHS.prerequisites}#${project.id}`}>Prerequisites for {project.shortName}</Link>
        </li>
        <li>
          <Link to={`${PATHS.costsAndCleanup}#${project.id}`}>Teardown and cost notes</Link>
        </li>
      </ul>
    </div>
  );
}

function ProjectSection({ project }: { project: Project }) {
  const quickstarts = getQuickstarts(project.id);
  return (
    <section id={project.id} className={styles.projectSection} aria-labelledby={`${project.id}-heading`}>
      <ProjectHeading project={project} id={`${project.id}-heading`} />
      <p className={styles.lead}>
        {project.tagline}. <Link to={projectPath(project.id)}>{project.shortName} project page</Link>.
      </p>
      {quickstarts.map((quickstart) => (
        <QuickstartBlock key={quickstart.id} project={project} quickstart={quickstart} />
      ))}
    </section>
  );
}

export function StartPage() {
  return (
    <>
      <PageMeta
        title="Getting started"
        description="The first ten minutes with each of the four AI Agent Factory projects on Amazon Bedrock AgentCore: clone, first steps and expected time, copied from the READMEs, with links to the full quickstarts."
      />
      <PageHeader
        eyebrow="Start"
        title="Get started"
        lead="Pick one of the four samples for agentic AI on Amazon Bedrock and Amazon Bedrock AgentCore, clone the repository, and take its first steps here. The full quickstart and the teardown live on each project page; every path deploys real AWS resources."
      />
      <div className="container">
        <StartNav />

        <section className={styles.projectSection} aria-labelledby="first-heading">
          <h2 id="first-heading">First ten minutes by project</h2>
          <p className={styles.prose}>
            Each section gives the clone and folder lines, the expected time and the first two steps of one project,
            then hands over to the project page for the rest.{' '}
            <SmallSource source={ROOT_QUICK_START} context="the clone command" />
          </p>
          <JumpLinks
            label="First ten minutes by project"
            lead="Jump to:"
            items={projects.map((project) => ({
              id: project.id,
              label: project.shortName,
            }))}
          />
        </section>

        {projects.map((project) => (
          <ProjectSection key={project.id} project={project} />
        ))}

        <section className={styles.projectSection} aria-labelledby="support-heading">
          <h2 id="support-heading">Support and feedback</h2>
          <ul className={styles.bulletList}>
            <li>
              <strong>Questions, bugs, and feature requests:</strong>{' '}
              <ExternalLink href={ISSUES_URL}>open a GitHub issue</ExternalLink> and name the project it concerns.
            </li>
            <li>
              <strong>Security issues:</strong> use the{' '}
              <ExternalLink href={VULN_REPORT_URL}>AWS vulnerability reporting page</ExternalLink>. Do not open a public
              GitHub issue for a potential security problem.{' '}
              <SourceLink
                source={{
                  file: 'CONTRIBUTING.md',
                  heading: 'Security issue notifications',
                }}
                className={styles.sourceSmall}
              >
                source
                <span className="visually-hidden"> for security reporting</span>
              </SourceLink>
            </li>
            <li>
              <strong>Contributing:</strong> read the <Link to={PATHS.contributing}>contribution guidelines</Link>{' '}
              before opening a pull request.
            </li>
            <li>
              <strong>Common questions:</strong> regions, costs, first sign-in, and the open issues are answered in the{' '}
              <Link to={PATHS.faq}>FAQ</Link>.
            </li>
          </ul>
        </section>
      </div>
    </>
  );
}
