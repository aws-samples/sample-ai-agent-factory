import { Link } from 'react-router-dom';
import { Card } from '../../components/Card';
import { Figure } from '../../components/Figure';
import { FlowStrip, type FlowStripItem } from '../../components/FlowStrip';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { Section } from '../../components/Section';
import { SourceLink } from '../../components/SourceLink';
import { Sources } from '../../components/Sources';
import { StageBadge } from '../../components/StageBadge';
import { capabilities, projects, type JourneyStage } from '../../content/data';
import type { Source } from '../../content/facts';
import { blob } from '../../content/links';
import { projectPath } from '../../paths';
import { ConceptsNav } from './ConceptsNav';
import blueprintConcept from '../../../../enterprise-agentic-ai-platform-blueprint/assets/enterprise-agent-factory-concept.svg';
import blueprintServices from '../../../../enterprise-agentic-ai-platform-blueprint/assets/enterprise-agent-factory-aws-services.svg';
import workshopLandingZone from '../../../../workshop-building-agentic-ai-platform/static/img/module-1/agentic-ai-platform-architecture.png';
import workshopLlmGateway from '../../../../workshop-building-agentic-ai-platform/static/img/module-2/llm-gateway-architecture.png';
import selfServiceArchitecture from '../../../../Agentic-ai-self-service/docs/architecture.jpg';
import styles from './ArchitecturePage.module.css';

const BASE_URL = import.meta.env.BASE_URL || '/';

const BLUEPRINT_README = 'enterprise-agentic-ai-platform-blueprint/README.md';
const BLUEPRINT_ASSETS = 'enterprise-agentic-ai-platform-blueprint/assets';
const WORKSHOP_PLATFORM_PAGE = 'workshop-building-agentic-ai-platform/content/module-1/the-platform/index.en.md';
const WORKSHOP_LLM_GATEWAY_PAGE = 'workshop-building-agentic-ai-platform/content/module-2/step-1/index.en.md';
const SELF_SERVICE_README = 'Agentic-ai-self-service/README.md';

const BLUEPRINT_VIEWS_SOURCE: Source = { file: BLUEPRINT_README, heading: 'Two complementary architecture views' };
const WORKSHOP_FIGURE_SOURCE: Source = { file: WORKSHOP_PLATFORM_PAGE };
/** The Module 2 page that embeds the LLM Gateway diagram, under its "Why an LLM Gateway?" heading. */
const WORKSHOP_LLM_GATEWAY_SOURCE: Source = { file: WORKSHOP_LLM_GATEWAY_PAGE, heading: 'Why an LLM Gateway?' };
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

const WORKSHOP_LLM_GATEWAY_ALT =
  'LLM Gateway architecture in one AWS region. Tenant clients reach the gateway through distribution options (Amazon CloudFront or Route 53), AWS WAF and an Application Load Balancer with an ACM certificate. Inside a cluster VPC, an ECS cluster or EKS cluster runs API middleware tasks and the LiteLLM proxy, pulling images from Amazon ECR and reading credentials from AWS Secrets Manager. LiteLLM routes requests to AWS model providers (Amazon Bedrock, Amazon Nova, Amazon SageMaker AI) and to external model providers (OpenAI, Anthropic, Vertex AI, Cohere), stores state in Amazon RDS and Amazon ElastiCache for Redis OSS, and writes logs to Amazon S3. Numbered steps mark the request path from client to model provider.';

const SELF_SERVICE_ALT =
  'AgentCore Visual Workflow Platform architecture in one AWS region. A React single-page app served by CloudFront and S3 calls API Gateway. Lambda workflow and deployment APIs store workflows, flows and deployments in DynamoDB and start a Step Functions deployment pipeline. Cognito, IAM, CloudWatch and Systems Manager provide identity, permissions, logs and configuration; a single CDK stack deploys everything and a CloudFormation export is available. The pipeline runs steps such as validate, guardrails, MCP server, knowledge base, gateway, memory, policy, code generation, IAM role, runtime configure and launch, evaluation, JWT auth and status update. For each agent it creates an Agent Runtime, an MCP Gateway with JWT auth, an MCP Server Runtime, tool Lambdas, AgentCore Memory, Knowledge Base, Evaluation, Policy and Observability, and a per-agent Cognito user pool.';

interface Plane {
  name: string;
  text: string;
  /** Stage whose tint colours the band's left rule (decorative, one per plane). */
  stage: JourneyStage;
}

const FOUR_PLANES: Plane[] = [
  {
    name: 'Developer experience plane',
    text: 'Versioned agent templates, CLI and repository workflows, approved extension points, and self-service onboarding.',
    stage: 'learn',
  },
  {
    name: 'Platform control plane',
    text: 'Registry governance, model and Guardrail policy, shared inference, release orchestration, and reusable account baselines.',
    stage: 'build',
  },
  {
    name: 'Workstream execution plane',
    text: 'Isolated cells containing team-owned Runtime, Memory, Tool Gateway, tools, and application data.',
    stage: 'govern',
  },
  {
    name: 'Assurance and operations plane',
    text: 'Organization policy, evidence gates, fleet telemetry, audit, incident response, quota management, and chargeback.',
    stage: 'scale',
  },
];

const FOUR_FLOWS: FlowStripItem[] = [
  {
    id: 'delivery',
    name: 'Software delivery flow',
    detail:
      'From pull request through source, build and synth, policy and image gates, stable roles, the permission handoff, nonproduction, deployed-runtime evaluation and human approval to production. No direct developer deployment path writes into a Workstream account.',
  },
  {
    id: 'inference',
    name: 'Inference flow',
    detail:
      'A governed LLM Gateway sits between agent workloads and model providers. The agent obtains a short-lived token through AgentCore Identity and Cognito M2M, calls the AgentCore Gateway inference endpoint, a request interceptor applies the stage Bedrock Guardrail, and the Gateway role invokes only an allow-listed Bedrock model.',
  },
  {
    id: 'tool',
    name: 'Tool flow',
    detail:
      'A governed Tool Gateway sits between agents and enterprise actions. AgentCore Gateway authenticates the Runtime role with AWS_IAM, targets come from approved Registry records, and the Gateway role invokes only subscribed Platform Lambda aliases. AgentCore PolicyEngine can enforce per-tool policy, with the Lambda Cedar wrapper as rollback and defense in depth.',
  },
  {
    id: 'telemetry',
    name: 'Telemetry and assurance flow',
    detail:
      'Every cell emits common logs, metrics, traces, deployment evidence and allocation dimensions. OAM links make Platform and Workstream telemetry queryable from Management, and CloudTrail plus retained audit data corroborate control-plane actions.',
  },
];

export function ArchitecturePage() {
  return (
    <div className={styles.page}>
      <PageMeta
        title="Architecture"
        description="Architecture diagrams for the AI Agent Factory: the repository atlas, the Blueprint's operating-model and AWS service-level figures, the workshop landing zone and LLM Gateway, the Self-Service platform architecture, and the Blueprint concepts of planes, cells and governed flows."
      />
      <PageHeader
        eyebrow="Concepts"
        title="Architecture"
        lead={`The repository map, the two reference diagrams of the ${BLUEPRINT_NAME} (the Blueprint), and the architecture figures the Workshop and Self-Service projects publish. Each project diagram shows where Amazon Bedrock AgentCore sits in that design, and every diagram is labelled with the project it belongs to.`}
        figure={
          <div className={styles.atlasFrame} data-atlas-figure>
            <img
              src={`${BASE_URL}repository-atlas-journey.svg`}
              alt={ATLAS_ALT}
              width={1200}
              height={700}
              loading="eager"
              decoding="async"
              className={styles.atlasImage}
            />
          </div>
        }
      />

      <div className="container">
        <ConceptsNav />

        <Section
          id="atlas"
          title="The repository at a glance"
          flush
          lead="The atlas in the page header shows the four projects in their Learn, Build, Govern, Scale order around the capabilities they share. The figure uses its own labels: it calls the Self-Service project the Visual Workflow Platform and the Blueprint the Enterprise Blueprint. The list below carries the same information as text, with the names this site uses."
        >
          <div>
            <p className={styles.atlasListIntro}>The same map as a list:</p>
            <ol className={styles.atlasProjects}>
              {projects.map((project) => (
                <Card as="li" key={project.id} padding="sm" className={styles.atlasProject}>
                  <StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />
                  <span>
                    <Link to={projectPath(project.id)} className={styles.atlasProjectLink}>
                      {project.shortName}
                    </Link>
                    <span className={styles.atlasTagline}> {project.tagline}</span>
                  </span>
                </Card>
              ))}
            </ol>
            <p className={styles.atlasHub}>
              <span className={styles.atlasHubLabel}>Shared capabilities hub: </span>
              {capabilities.map((cap) => cap.name).join(', ')}.
            </p>
          </div>
        </Section>

        <Section id="blueprint-figures" title="Blueprint: two reference diagrams">
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
                <strong>Figure 1 (Blueprint).</strong> Enterprise operating model and governed flow. AWS labels
                illustrate the repository&apos;s reference choices; the capability boundaries are the architecture.
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
                <strong>Figure 2 (Blueprint).</strong> AWS service-level reference implementation. Account IDs are
                documentation placeholders.
              </>
            }
            download={{
              href: blob(`${BLUEPRINT_ASSETS}/enterprise-agent-factory-aws-services.drawio`),
              label: 'Open the editable Draw.io source',
            }}
          />
        </Section>

        <Section id="workshop-figure" title="Workshop: the landing-zone pattern and the LLM Gateway">
          <p className={styles.prose}>
            The <Link to={projectPath('workshop')}>Workshop</Link> opens with the first diagram in Module 1. It is the
            multi-account pattern the modules teach; the self-paced deploy script stands up the Platform-account
            pieces in a single account. Module 2 adds the second diagram, the LLM Gateway that the workshop deploys as
            a LiteLLM proxy.
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
          <Figure
            src={workshopLlmGateway}
            alt={WORKSHOP_LLM_GATEWAY_ALT}
            width={1851}
            height={1111}
            caption={
              <>
                <strong>Workshop (Module 2).</strong> LLM Gateway architecture, shown under the heading &quot;Why an
                LLM Gateway?&quot;: a LiteLLM proxy on ECS or EKS behind WAF and a load balancer, routing to AWS and
                external model providers.{' '}
                <SourceLink source={WORKSHOP_LLM_GATEWAY_SOURCE}>Source page</SourceLink>.
              </>
            }
          />
        </Section>

        <Section id="self-service-figure" title="Self-Service: platform architecture">
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
        </Section>

        <Section
          id="blueprint-concepts"
          title="Concepts from the Blueprint"
          lead="The three ideas below come from the Blueprint's README and describe the Blueprint only. The Workshop, Self-Service and MCP Gateway projects do not use them."
        >
          <div className={styles.concepts}>
            <article className={styles.concept} aria-labelledby="four-planes">
              <h3 id="four-planes">Four planes</h3>
              <p>The Blueprint treats the Agent Factory as a product with four planes:</p>
              <ol className={styles.planes}>
                {FOUR_PLANES.map((plane, index) => (
                  <Card as="li" key={plane.name} padding="sm" reveal stage={plane.stage} className={styles.plane}>
                    <span className={styles.planeNumber} aria-hidden="true">
                      {index + 1}
                    </span>
                    <span className={styles.planeName}>{plane.name}</span>
                    <span className={styles.planeText}>{plane.text}</span>
                  </Card>
                ))}
              </ol>
              <Sources sources={[FOUR_PLANES_SOURCE]} />
            </article>

            <Card as="article" reveal className={styles.concept} aria-labelledby="workstream-cells">
              <h3 id="workstream-cells">Workstream cells</h3>
              <p>
                The unit of scale is a workstream cell, not a manually configured agent. A cell can represent a product,
                business domain, regulated boundary, or portfolio team. Each cell is an isolated execution boundary with
                a team-owned repository and Workload pipeline, stable IAM roles, and nonproduction and production
                AgentCore Runtime and Memory. It also holds an AWS_IAM Tool Gateway, Registry-approved targets,
                team-owned tools and data, local alarms, budgets, KMS keys, tags and retention policy, and an OAM link
                into the observability plane. A cell can deploy, roll back, or fail without another team coordinating
                its release.
              </p>
              <Sources sources={[CELL_SOURCE]} />
            </Card>

            <article className={styles.concept} aria-labelledby="four-flows">
              <h3 id="four-flows">Four governed flows</h3>
              <FlowStrip
                items={FOUR_FLOWS}
                stage="scale"
                label="Four governed flows"
                caption={<Sources sources={[FLOWS_SOURCE]} inline />}
              />
            </article>
          </div>
        </Section>
      </div>
    </div>
  );
}
