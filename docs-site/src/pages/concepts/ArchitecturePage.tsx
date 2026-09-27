import { Link } from 'react-router-dom';
import { Figure } from '../../components/Figure';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { ConceptsNav } from './ConceptsNav';
import { SourceLink } from '../../components/SourceLink';
import { StageBadge } from '../../components/StageBadge';
import { capabilities, projects } from '../../content/data';
import type { Source } from '../../content/facts';
import { blob } from '../../content/links';
import { projectPath } from '../../paths';
import blueprintConcept from '../../../../enterprise-agentic-ai-platform-blueprint/assets/enterprise-agent-factory-concept.svg';
import blueprintServices from '../../../../enterprise-agentic-ai-platform-blueprint/assets/enterprise-agent-factory-aws-services.svg';
import workshopLandingZone from '../../../../workshop-building-agentic-ai-platform/static/img/module-1/agentic-ai-platform-architecture.png';
import selfServiceArchitecture from '../../../../Agentic-ai-self-service/docs/architecture.jpg';
import styles from './ArchitecturePage.module.css';

const BASE_URL = import.meta.env.BASE_URL || '/';

const BLUEPRINT_README = 'enterprise-agentic-ai-platform-blueprint/README.md';
const BLUEPRINT_ASSETS = 'enterprise-agentic-ai-platform-blueprint/assets';
const WORKSHOP_PLATFORM_PAGE = 'workshop-building-agentic-ai-platform/content/module-1/the-platform/index.en.md';
const SELF_SERVICE_README = 'Agentic-ai-self-service/README.md';

const BLUEPRINT_VIEWS_SOURCE: Source = { file: BLUEPRINT_README, heading: 'Two complementary architecture views' };
const WORKSHOP_FIGURE_SOURCE: Source = { file: WORKSHOP_PLATFORM_PAGE };
const SELF_SERVICE_FIGURE_SOURCE: Source = { file: SELF_SERVICE_README, heading: 'Architecture' };
const FOUR_PLANES_SOURCE: Source = { file: BLUEPRINT_README, heading: '1. Overview' };
const CELL_SOURCE: Source = { file: BLUEPRINT_README, heading: '2.3 Repeatable Workstream cell' };
const FLOWS_SOURCE: Source = { file: BLUEPRINT_README, heading: '2.4 Four governed flows' };

const BLUEPRINT_NAME = projects.find((project) => project.id === 'blueprint')?.name ?? 'Blueprint';

/** Mirrors the text labels inside the SVG, including its own project names. */
const ATLAS_ALT =
  'AI Agent Factory Atlas: four complementary projects for enterprise agentic AI on AWS. In the centre sits a hub labelled Agent Factory Capabilities: LLM Gateway, Tool Gateway, Identity and Registry, Policy and Observability. Around it, four stages in order. Learn: Workshop, hands-on platform patterns. Build: Visual Workflow Platform, drag-and-drop agent builder. Govern: MCP Governance Gateway, per-tool-call authorization. Scale: Enterprise Blueprint, multi-account reference blueprint. A legend distinguishes the sequential journey path, a skip-ahead path, and the shared-capability links from each project to the hub. A footer reads: Each project is self-contained. Start anywhere based on your role and goals.';

const CONCEPT_ALT =
  'Enterprise Agent Factory operating model and governed flow: enterprise governance, people and ownership, an Agent Factory control plane, and repeatable isolated Workstream cells connected by software delivery, inference, tool, telemetry and governance flows.';

const SERVICES_ALT =
  'Enterprise Agent Factory AWS service-level reference architecture: Management, Security, Observability, Platform and repeatable Workstream accounts; the AWS services mapped to each capability; and numbered software delivery, identity, inference, tool and telemetry flows.';

const WORKSHOP_ALT =
  'Workshop platform architecture: a multi-account AWS landing zone. An AWS Organization holds governance accounts (Management with Organizations, Control Tower and IAM Identity Center; Log Archive with organization CloudTrail, WORM S3 and Bedrock invocation logs; Audit with Security Hub, GuardDuty, Config, Inspector and CloudWatch OAM). A central Platform account hosts the auth boundary (API Gateway, WAF, Cognito), Bedrock AgentCore (Gateway, Registry, Cedar), the inference gateway (LiteLLM on ECS Fargate), and security and cost controls (Guardrails, CUR, KMS). Per-application Workload accounts run AgentCore services, agent blueprints, RAG and knowledge stores, and PrivateLink egress. A CI/CD pipeline runs from source through build and test, non-production deploy, an evaluation gate, a canary stage, production, and a teardown test.';

const SELF_SERVICE_ALT =
  'AgentCore Visual Workflow Platform architecture in one AWS region. A React single-page app served by CloudFront and S3 calls API Gateway. Lambda workflow and deployment APIs store workflows, flows and deployments in DynamoDB and start a Step Functions deployment pipeline. Cognito, IAM, CloudWatch and Systems Manager provide identity, permissions, logs and configuration; a single CDK stack deploys everything and a CloudFormation export is available. The pipeline runs steps such as validate, guardrails, MCP server, knowledge base, gateway, memory, policy, code generation, IAM role, runtime configure and launch, evaluation, JWT auth and status update. For each agent it creates an Agent Runtime, an MCP Gateway with JWT auth, an MCP Server Runtime, tool Lambdas, AgentCore Memory, Knowledge Base, Evaluation, Policy and Observability, and a per-agent Cognito user pool.';

interface Plane {
  name: string;
  text: string;
}

const FOUR_PLANES: Plane[] = [
  {
    name: 'Developer experience plane',
    text: 'Versioned agent templates, CLI and repository workflows, approved extension points, and self-service onboarding.',
  },
  {
    name: 'Platform control plane',
    text: 'Registry governance, model and Guardrail policy, shared inference, release orchestration, and reusable account baselines.',
  },
  {
    name: 'Workstream execution plane',
    text: 'Isolated cells containing team-owned Runtime, Memory, Tool Gateway, tools, and application data.',
  },
  {
    name: 'Assurance and operations plane',
    text: 'Organization policy, evidence gates, fleet telemetry, audit, incident response, quota management, and chargeback.',
  },
];

interface Flow {
  name: string;
  text: string;
}

const FOUR_FLOWS: Flow[] = [
  {
    name: 'Software delivery flow',
    text: 'From pull request through source, build and synth, policy and image gates, stable roles, the permission handoff, nonproduction, deployed-runtime evaluation and human approval to production. No direct developer deployment path writes into a Workstream account.',
  },
  {
    name: 'Inference flow',
    text: 'A governed LLM Gateway sits between agent workloads and model providers. The agent obtains a short-lived token through AgentCore Identity and Cognito M2M, calls the AgentCore Gateway inference endpoint, a request interceptor applies the stage Bedrock Guardrail, and the Gateway role invokes only an allow-listed Bedrock model.',
  },
  {
    name: 'Tool flow',
    text: 'A governed Tool Gateway sits between agents and enterprise actions. AgentCore Gateway authenticates the Runtime role with AWS_IAM, targets come from approved Registry records, and the Gateway role invokes only subscribed Platform Lambda aliases. AgentCore PolicyEngine can enforce per-tool policy, with the Lambda Cedar wrapper as rollback and defense in depth.',
  },
  {
    name: 'Telemetry and assurance flow',
    text: 'Every cell emits common logs, metrics, traces, deployment evidence and allocation dimensions. OAM links make Platform and Workstream telemetry queryable from Management, and CloudTrail plus retained audit data corroborate control-plane actions.',
  },
];

export function ArchitecturePage() {
  return (
    <div className={styles.page}>
      <PageMeta
        title="Architecture"
        description="Architecture diagrams for the AI Agent Factory: the repository atlas, the Blueprint's operating-model and AWS service-level figures, the workshop landing zone, the Self-Service platform architecture, and the Blueprint concepts of planes, cells and governed flows."
      />
      <PageHeader
        eyebrow="Concepts"
        title="Architecture"
        lead={`The repository map, the two reference diagrams of the ${BLUEPRINT_NAME} (the Blueprint), and the architecture figures the Workshop and Self-Service projects publish. Each project diagram shows where Amazon Bedrock AgentCore sits in that design, and every diagram is labelled with the project it belongs to.`}
      />

      <div className="container">
        <ConceptsNav />

        <section className={styles.section} aria-labelledby="atlas">
          <h2 id="atlas">The repository at a glance</h2>
          <p className={styles.prose}>
            The atlas shows the four projects in their Learn, Build, Govern, Scale order around the capabilities they
            share. The figure uses its own labels: it calls the Self-Service project the Visual Workflow Platform and
            the Blueprint the Enterprise Blueprint. The list below carries the same information as text, with the
            names this site uses.
          </p>
          <div className={`${styles.atlasFrame} on-dark`}>
            <img
              src={`${BASE_URL}repository-atlas-journey.svg`}
              alt={ATLAS_ALT}
              width={1200}
              height={700}
              className={styles.atlasImage}
            />
          </div>
          <div className={styles.atlasList}>
            <p className={styles.atlasListIntro}>The same map as a list:</p>
            <ol className={styles.atlasProjects}>
              {projects.map((project) => (
                <li key={project.id} className={styles.atlasProject}>
                  <StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />
                  <span>
                    <Link to={projectPath(project.id)} className={styles.atlasProjectLink}>
                      {project.shortName}
                    </Link>
                    <span className={styles.atlasTagline}> {project.tagline}</span>
                  </span>
                </li>
              ))}
            </ol>
            <p className={styles.atlasHub}>
              <span className={styles.atlasHubLabel}>Shared capabilities hub: </span>
              {capabilities.map((cap) => cap.name).join(', ')}.
            </p>
          </div>
        </section>

        <section className={styles.section} aria-labelledby="blueprint-figures">
          <h2 id="blueprint-figures">Blueprint: two reference diagrams</h2>
          <p className={styles.prose}>
            Both figures belong to the <Link to={projectPath('blueprint')}>Blueprint</Link>. Its README explains that
            Figure 1 is the operating-model view: people, ownership, shared capability planes, repeatable cells, and
            governed flows. Figure 2 is the AWS reference-implementation view: the concrete services the repository
            deploys and the numbered release and run cycle used by its live evidence. Source:{' '}
            <SourceLink source={BLUEPRINT_VIEWS_SOURCE} />.
          </p>
          <Figure
            src={blueprintConcept}
            alt={CONCEPT_ALT}
            width={1840}
            height={1330}
            caption={
              <>
                <strong>Figure 1 (Blueprint).</strong> Enterprise operating model and governed flow. AWS
                labels illustrate the repository&apos;s reference choices; the capability boundaries are the
                architecture.
              </>
            }
            download={{
              href: blob(`${BLUEPRINT_ASSETS}/enterprise-agent-factory-concept.drawio`),
              label: 'Open the editable Draw.io source',
            }}
          />
          <Figure
            src={blueprintServices}
            alt={SERVICES_ALT}
            width={1920}
            height={1470}
            caption={
              <>
                <strong>Figure 2 (Blueprint).</strong> AWS service-level reference implementation. Account
                IDs are documentation placeholders.
              </>
            }
            download={{
              href: blob(`${BLUEPRINT_ASSETS}/enterprise-agent-factory-aws-services.drawio`),
              label: 'Open the editable Draw.io source',
            }}
          />
        </section>

        <section className={styles.section} aria-labelledby="workshop-figure">
          <h2 id="workshop-figure">Workshop: the landing-zone pattern</h2>
          <p className={styles.prose}>
            The <Link to={projectPath('workshop')}>Workshop</Link> opens with this diagram in Module 1. It is the
            multi-account pattern the modules teach; the self-paced deploy script stands up the Platform-account
            pieces in a single account.
          </p>
          <Figure
            src={workshopLandingZone}
            alt={WORKSHOP_ALT}
            width={2062}
            height={1094}
            caption={
              <>
                <strong>Workshop (Module 1).</strong> Agentic AI platform architecture: governance accounts, a central
                Platform account, per-application Workload accounts, and the CI/CD pipeline.{' '}
                <SourceLink source={WORKSHOP_FIGURE_SOURCE}>Source page</SourceLink>.
              </>
            }
          />
        </section>

        <section className={styles.section} aria-labelledby="self-service-figure">
          <h2 id="self-service-figure">Self-Service: platform architecture</h2>
          <p className={styles.prose}>
            The <Link to={projectPath('self-service')}>Self-Service</Link> project (AgentCore Visual Workflow Platform)
            publishes this diagram in its README: the serverless control plane, the Step Functions deployment pipeline,
            and the resources the pipeline creates for each agent.
          </p>
          <Figure
            src={selfServiceArchitecture}
            alt={SELF_SERVICE_ALT}
            width={2400}
            height={1700}
            caption={
              <>
                <strong>Self-Service (AgentCore Visual Workflow Platform).</strong> Architecture from the project README.{' '}
                <SourceLink source={SELF_SERVICE_FIGURE_SOURCE}>Source section</SourceLink>.
              </>
            }
            download={{
              href: blob('Agentic-ai-self-service/docs/architecture.drawio'),
              label: 'Open the editable Draw.io source',
            }}
          />
        </section>

        <section className={styles.section} aria-labelledby="blueprint-concepts">
          <h2 id="blueprint-concepts">Concepts from the Blueprint</h2>
          <p className={styles.prose}>
            The three ideas below come from the Blueprint&apos;s README and describe the Blueprint only. The Workshop,
            Self-Service and MCP Gateway projects do not use them.
          </p>

          <div className={styles.conceptGrid}>
            <article className={styles.conceptCard}>
              <h3>Four planes</h3>
              <p>The Blueprint treats the Agent Factory as a product with four planes:</p>
              <ol className={styles.conceptList}>
                {FOUR_PLANES.map((plane) => (
                  <li key={plane.name}>
                    <strong>{plane.name}.</strong> {plane.text}
                  </li>
                ))}
              </ol>
              <p className={styles.conceptSource}>
                Source: <SourceLink source={FOUR_PLANES_SOURCE} />
              </p>
            </article>

            <article className={styles.conceptCard}>
              <h3>Workstream cells</h3>
              <p>
                The unit of scale is a workstream cell, not a manually configured agent. A cell can represent a product,
                business domain, regulated boundary, or portfolio team. Each cell is an isolated execution boundary with
                a team-owned repository and Workload pipeline, stable IAM roles, and nonproduction and production
                AgentCore Runtime and Memory. It also holds an AWS_IAM Tool Gateway, Registry-approved targets,
                team-owned tools and data, local alarms, budgets, KMS keys, tags and retention policy, and an OAM link
                into the observability plane. A cell can deploy, roll back, or fail without another team coordinating
                its release.
              </p>
              <p className={styles.conceptSource}>
                Source: <SourceLink source={CELL_SOURCE} />
              </p>
            </article>

            <article className={`${styles.conceptCard} ${styles.conceptCardWide}`}>
              <h3>Four governed flows</h3>
              <dl className={styles.flowList}>
                {FOUR_FLOWS.map((flow) => (
                  <div key={flow.name} className={styles.flowItem}>
                    <dt>{flow.name}</dt>
                    <dd>{flow.text}</dd>
                  </div>
                ))}
              </dl>
              <p className={styles.conceptSource}>
                Source: <SourceLink source={FLOWS_SOURCE} />
              </p>
            </article>
          </div>
        </section>
      </div>
    </div>
  );
}
