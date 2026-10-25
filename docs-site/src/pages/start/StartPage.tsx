import { Link } from 'react-router-dom';
import { Timer } from 'lucide-react';
import { CodeBlock } from '../../components/CodeBlock';
import { ExternalLink } from '../../components/ExternalLink';
import { NumberedSteps } from '../../components/NumberedSteps';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { Section } from '../../components/Section';
import { SourceLink } from '../../components/SourceLink';
import { StatTile } from '../../components/StatTile';
import { projects, type Project } from '../../content/data';
import { factText, getFacts } from '../../content/facts';
import { ISSUES_URL, REPO_URL, VULN_REPORT_URL } from '../../content/links';
import { getQuickstarts, type Quickstart, type QuickstartStep } from '../../content/quickstarts';
import { PATHS, projectPath } from '../../paths';
import { FactValue, JumpLinks, ProjectBadge, SmallSource, StartNav } from './shared';
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
  const hasBody = Boolean(step.command || step.note || (step.href && !linkIsTitle));
  return (
    <NumberedSteps.Item
      title={
        <span className={styles.stepTitle}>
          {linkIsTitle && step.href ? <ExternalLink href={step.href}>{step.title}</ExternalLink> : step.title}{' '}
          <SmallSource source={step.source} context={`step: ${step.title}`} />
        </span>
      }
    >
      {hasBody && (
        <>
          {step.command && <CodeBlock code={step.command} language="bash" />}
          {step.note && <p className={styles.stepNote}>{step.note}</p>}
          {step.href && !linkIsTitle && (
            <p className={styles.stepNote}>
              <ExternalLink href={step.href}>{step.hrefLabel ?? 'Open the README section'}</ExternalLink>
            </p>
          )}
        </>
      )}
    </NumberedSteps.Item>
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
      <NumberedSteps stage={project.stage} connector>
        {clone && <StepItem step={clone} />}
        {firstSteps.map((step) => (
          <StepItem key={step.title} step={step} />
        ))}
      </NumberedSteps>
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
    <Section
      id={project.id}
      title={project.name}
      badge={<ProjectBadge project={project} />}
      lead={
        <>
          {project.tagline}. <Link to={projectPath(project.id)}>{project.shortName} project page</Link>.
        </>
      }
    >
      {quickstarts.map((quickstart) => (
        <QuickstartBlock key={quickstart.id} project={project} quickstart={quickstart} />
      ))}
    </Section>
  );
}

/** First-deploy time per project, shown on the header band. The notes and caveats follow in each section. */
function FirstDeployRow() {
  return (
    <dl className={styles.heroFacts}>
      {projects.map((project) => {
        const fact = getFacts(project.id).firstDeploy;
        return (
          <StatTile
            key={project.id}
            label={project.shortName}
            value={fact.notDocumented ? <em>{factText(fact)}</em> : factText(fact)}
            muted={Boolean(fact.notDocumented)}
            source={fact.source}
            context={`first deploy, ${project.shortName}`}
            icon={<Timer size={14} aria-hidden="true" />}
          />
        );
      })}
    </dl>
  );
}

export function StartPage() {
  return (
    <>
      <PageMeta
        title="Getting started"
        description="The first ten minutes with each of the four Agentic AI Factory projects on Amazon Bedrock AgentCore: clone, first steps and expected time, copied from the READMEs, with links to the full quickstarts."
      />
      <PageHeader
        eyebrow="Start"
        title="Get started"
        lead="Pick one of the four samples for agentic AI on Amazon Bedrock and Amazon Bedrock AgentCore, clone the repository, and take its first steps here. The full quickstart and the teardown live on each project page; every path deploys real AWS resources."
        meta={<FirstDeployRow />}
      />
      <div className="container">
        <StartNav />

        <Section
          id="first"
          title="First ten minutes by project"
          flush
          lead={
            <>
              Each section gives the clone and folder lines, the expected time and the first two steps of one project,
              then hands over to the project page for the rest.{' '}
              <SmallSource source={ROOT_QUICK_START} context="the clone command" />
            </>
          }
        >
          <JumpLinks
            label="First ten minutes by project"
            lead="Jump to:"
            items={projects.map((project) => ({
              id: project.id,
              label: project.shortName,
            }))}
          />
        </Section>

        {projects.map((project) => (
          <ProjectSection key={project.id} project={project} />
        ))}

        <Section id="support" title="Support and feedback">
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
        </Section>
      </div>
    </>
  );
}
