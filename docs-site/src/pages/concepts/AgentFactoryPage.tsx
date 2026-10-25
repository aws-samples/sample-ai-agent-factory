import { Link } from 'react-router-dom';
import { Card } from '../../components/Card';
import { NumberedSteps } from '../../components/NumberedSteps';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { Section } from '../../components/Section';
import { SourceLink } from '../../components/SourceLink';
import { StageBadge } from '../../components/StageBadge';
import { FactStat } from '../../components/StatTile';
import { getProjectById, projects, type ProjectId } from '../../content/data';
import { facts, type Fact, type Source } from '../../content/facts';
import { PATHS, projectPath } from '../../paths';
import { STAGE_ICONS } from '../../stage';
import { ConceptsNav } from './ConceptsNav';
import styles from './AgentFactoryPage.module.css';

const ROOT_README = 'README.md';
const BLUEPRINT_README = 'enterprise-agentic-ai-platform-blueprint/README.md';

const SHIPPING_SOURCE: Source = {
  file: BLUEPRINT_README,
  heading: '1.2 How an engineer ships an agent',
};

const INDEPENDENCE_SOURCE: Source = {
  file: ROOT_README,
  heading: 'What This Is',
};

/** The one-line description under the root README's title. */
const README_INTRO_SOURCE: Source = {
  file: ROOT_README,
  heading: 'Agentic AI Factory',
};

const BLUEPRINT_NAME = projects.find((project) => project.id === 'blueprint')?.name ?? 'Blueprint';

interface StoryPart {
  title: string;
  text: string;
  projectIds: ProjectId[];
}

const STORY_PARTS: StoryPart[] = [
  {
    title: 'Platform foundation',
    text: 'The governed, enterprise-grade landing zone an organization stands up once so every team can build on shared, secured infrastructure.',
    projectIds: ['workshop', 'blueprint'],
  },
  {
    title: 'Builder experience',
    text: 'The visual workflow builder that lets engineers design, configure, and deploy agents through a drag-and-drop canvas on top of that foundation.',
    projectIds: ['self-service'],
  },
  {
    title: 'Governed front door',
    text: 'The enforcement layer that decides, for each individual tool call, whether an agent may reach a given internal tool or SaaS system.',
    projectIds: ['mcp-gateway'],
  },
];

interface ShippingStep {
  title: string;
  text: string;
}

/** The Blueprint's shipping steps (README section 1.2). Titles are verbatim; step 1 is quoted; the rest is lightly condensed. */
const BLUEPRINT_STEPS: ShippingStep[] = [
  {
    title: 'Discover a paved road.',
    text: 'Select a task, chatbot, supervisor/worker, LangGraph, or CrewAI template maintained by the platform team.',
  },
  {
    title: 'Create in a team-owned repository.',
    text: 'The template supplies approved Gateway clients, prompt and tool extension points, an evaluation corpus, and a metadata contract.',
  },
  {
    title: 'Select governed capabilities.',
    text: 'Models come from the Platform allow-list. Tools come from approved Registry records. Guardrail profiles come from the supported catalog.',
  },
  {
    title: 'Open a pull request.',
    text: 'Source review, static checks, manifest hashing, image scanning, policy validation, and evaluation run before deployment.',
  },
  {
    title: 'Deploy to the Workstream cell.',
    text: 'The pipeline creates stable roles, completes the Platform permission handoff, and deploys nonproduction resources.',
  },
  {
    title: 'Prove behavior.',
    text: 'The deployed Runtime is exercised with authorized and adversarial twins. Quality, tool success, refusal, latency, cost, rollback, and telemetry gates fail closed.',
  },
  {
    title: 'Approve production.',
    text: 'A human reviews evidence rather than a generic "pipeline succeeded" signal.',
  },
  {
    title: 'Operate with the fleet.',
    text: 'The team owns its application SLOs and data; the platform team owns shared service health and common controls.',
  },
];

interface ProjectShipping {
  projectId: ProjectId;
  text: string;
  /** The fact whose value and source are shown under the paragraph. */
  factLabel: string;
  fact: Fact;
}

/** How each project actually gets an agent running, in stage order. */
const PROJECT_SHIPPING: ProjectShipping[] = [
  {
    projectId: 'workshop',
    text: 'Nothing ships through a pipeline. You work in the browser-based Code Editor IDE that the workshop provisions and run each module section either as a CLI walkthrough or as a notebook. The final module deploys a FAST travel agent onto AgentCore Runtime with the AWS CDK from inside that IDE.',
    factLabel: 'Hands-on time',
    fact: facts.workshop.handsOnTime,
  },
  {
    projectId: 'self-service',
    text: 'You design the agent on the drag-and-drop canvas, optionally starting from a template, and press Deploy. A Step Functions pipeline validates the workflow, generates the code, creates the IAM role, deploys the AgentCore Runtime and runs an evaluation step. Versioning and rollback are built in.',
    factLabel: 'First deploy of the platform',
    fact: facts['self-service'].firstDeploy,
  },
  {
    projectId: 'mcp-gateway',
    text: 'There is no agent to ship. You run cdk deploy once for the gateway stack, seed the demo users, mint a JWT and connect an MCP client such as Kiro or Claude Code. From then on every tool call from that client passes through the gateway, its Cedar policies and its interceptors.',
    factLabel: 'First deploy',
    fact: facts['mcp-gateway'].firstDeploy,
  },
  {
    projectId: 'blueprint',
    text: 'Agents ship through the Blueprint flow above: a golden-path template in a team-owned repository, a pull request, and a Workload pipeline that deploys to the Workstream cell, proves behavior against authorized and adversarial twins, and waits for a human approval before production.',
    factLabel: 'First deploy',
    fact: facts.blueprint.firstDeploy,
  },
];

export function AgentFactoryPage() {
  return (
    <div className={styles.page}>
      <PageMeta
        title="The Agent Factory"
        description="What an Agent Factory is, how this site groups the four projects, the Blueprint's step-by-step path from template to production, and how each of the four projects actually ships an agent."
      />
      <PageHeader
        eyebrow="Concepts"
        title="The Agent Factory"
        lead="What an Agent Factory is, how this site groups the four projects, and how each project in this repository actually gets an agent running."
      />

      <div className="container">
        <ConceptsNav />

        <Section id="concept" title="The Agent Factory concept" flush>
          <p className={styles.prose}>
            An <strong>Agentic AI Factory</strong> is the people, patterns, and platform that let an organization turn
            ideas into production agents reliably and at scale. The hard part is everything around the model:
            governed access, reusable tools, security and authorization, observability, and a repeatable way to ship
            agents to production.
          </p>
          <p className={styles.prose}>
            This repository&apos;s README describes its four projects as enterprise samples for building, governing,
            and operating agentic AI on AWS, centered on Amazon Bedrock and Amazon Bedrock AgentCore (
            <SourceLink source={README_INTRO_SOURCE}>README introduction</SourceLink>). Together, it says, they show
            how enterprises move from learning to agent operations at scale (
            <SourceLink source={INDEPENDENCE_SOURCE}>What This Is</SourceLink>).
          </p>
        </Section>

        <Section
          id="site-grouping"
          title="How this site groups the four projects"
          lead="This grouping is this site's reading of the repository, not a structure the project READMEs use. It exists to show which project answers which need."
        >
          <ol className={styles.partsGrid}>
            {STORY_PARTS.map((part, index) => (
              <Card as="li" key={part.title} reveal padding="lg" className={styles.partCard}>
                <span className={styles.partNumber} aria-hidden="true">
                  {index + 1}
                </span>
                <h3 className={styles.partTitle}>{part.title}</h3>
                <p>{part.text}</p>
                <p className={styles.partProjects}>
                  <span className={styles.partProjectsLabel}>Projects:</span>
                  {part.projectIds.map((id) => {
                    const project = getProjectById(id);
                    if (!project) return null;
                    return (
                      <Link key={id} to={projectPath(id)} className={styles.partProjectLink}>
                        <StageBadge stage={project.stage} label={project.stageLabel} variant="outline" />
                        {project.shortName}
                      </Link>
                    );
                  })}
                </p>
              </Card>
            ))}
          </ol>
        </Section>

        <Section id="ships" title="How an engineer ships an agent (Blueprint)">
          <p className={styles.prose}>
            The <Link to={projectPath('blueprint')}>{BLUEPRINT_NAME}</Link> (the Blueprint, in the rest of this page)
            describes this flow in its README. It is the Blueprint&apos;s pull-request-and-pipeline path; the other
            three projects ship an agent differently, as the next section shows. Step titles below are the
            README&apos;s own and the first step is quoted; the other descriptions are condensed. Source:{' '}
            <SourceLink source={SHIPPING_SOURCE} />.
          </p>
          <NumberedSteps stage="scale" connector className={styles.steps}>
            {BLUEPRINT_STEPS.map((step) => (
              <NumberedSteps.Item key={step.title} title={step.title}>
                <p>{step.text}</p>
              </NumberedSteps.Item>
            ))}
          </NumberedSteps>
          <p className={styles.prose}>
            In the Blueprint, the Platform team is not in the application deployment loop. It owns the contracts that
            make independent deployment safe.
          </p>
        </Section>

        <Section id="each-project" title="How each project actually ships an agent">
          <ul className={styles.shipList}>
            {PROJECT_SHIPPING.map((entry) => {
              const project = getProjectById(entry.projectId);
              if (!project) return null;
              const Icon = STAGE_ICONS[project.stage];
              return (
                <Card as="li" key={entry.projectId} variant="accent" stage={project.stage} reveal className={styles.shipItem}>
                  <div className={styles.shipHeading}>
                    <Icon size={20} aria-hidden="true" className={styles.shipIcon} />
                    <h3 className={styles.shipTitle}>
                      <Link to={projectPath(project.id)}>{project.shortName}</Link>
                    </h3>
                    <StageBadge stage={project.stage} label={project.stageLabel} />
                  </div>
                  <p>{entry.text}</p>
                  <dl className={styles.shipFacts}>
                    <FactStat fact={entry.fact} label={entry.factLabel} project={project.shortName} />
                  </dl>
                </Card>
              );
            })}
          </ul>
          <p className={styles.prose}>
            The projects are independent. Each is self-contained with its own deployment instructions and can be
            adopted in any order; none requires another to be deployed first (
            <SourceLink source={INDEPENDENCE_SOURCE}>root README</SourceLink>). To pick one, use{' '}
            <Link to={PATHS.whichProject}>Which project is for me</Link>.
          </p>
        </Section>
      </div>
    </div>
  );
}
