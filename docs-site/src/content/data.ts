/**
 * Core typed content model for the AI Agent Factory docs site.
 *
 * Pure data: no React, no imports from components or pages. Copy in this file
 * is kept at parity with the root README and each project's README. Numbers,
 * regions and durations that the site renders as facts live in `facts.ts`
 * with a source pointer; this file only carries descriptive copy.
 */

/** The four journey stages. Order matters: Learn, Build, Govern, Scale. */
export type JourneyStage = 'learn' | 'build' | 'govern' | 'scale';

/** Stable identifiers for the four sub-projects. Used as route segments. */
export type ProjectId = 'workshop' | 'self-service' | 'mcp-gateway' | 'blueprint';

/**
 * Pointer to a repository file that backs a statement.
 * Duplicated here as a structural type so `data.ts` stays dependency free;
 * `facts.ts` exports the canonical `Source` type with the same shape.
 */
export interface CopySource {
  /** Repository-relative path, for example `Agentic-ai-self-service/README.md`. */
  file: string;
  /** Exact Markdown heading text in that file, when the file has headings. */
  heading?: string;
  /** Verbatim substring of the file (20 to 120 characters). */
  quote?: string;
}

/** One of the four sub-projects. */
export interface Project {
  /** Route segment and lookup key. */
  id: ProjectId;
  /** Full project name as written in its README title. */
  name: string;
  /** Short label used in navigation and badges. */
  shortName: string;
  /** One sentence used for meta descriptions and directory cards. */
  description: string;
  /** Short phrase (a few words) used under the name in heroes and tiles. */
  tagline: string;
  /** Journey stage this project represents. */
  stage: JourneyStage;
  /** Display label for the stage. */
  stageLabel: string;
  /** 1-based stage number. */
  stageNumber: number;
  /** Exact, case-sensitive folder name in the repository. */
  folder: string;
  /** Site route for the project landing page (trailing slash). */
  route: string;
  /** Three or four short highlights shown on tiles. */
  highlights: string[];
  /** Who the project is best for, from the root README. */
  bestFor: string;
  /** Feature bullets at README parity. */
  features: string[];
  /** Prerequisite bullets at README parity. */
  prerequisites: string[];
  /** Where the feature and prerequisite copy was taken from. */
  sources: {
    features: CopySource;
    prerequisites: CopySource;
    /** Evidence for a prerequisite bullet that goes beyond the README section, with the text to show beside it. */
    prerequisitesNote?: { text: string; source: CopySource };
  };
}

/** One of the ten shared capabilities from the root README table. */
export interface Capability {
  /** Stable key used by the capability matrix. */
  id: string;
  /** Display name. */
  name: string;
  /** What the capability does (root README "Purpose" column, expanded). */
  description: string;
  /** Reference implementations named in the root README. */
  implementations: string[];
  /** What any replacement must preserve (the capability contract). */
  contractNote?: string;
}

/** A top-level navigation entry with optional children. */
export interface NavItem {
  /** Site path with a trailing slash. */
  path: string;
  /** Visible label. */
  label: string;
  /** Child entries rendered as a dropdown or nested list. */
  children?: NavItem[];
}

export const projects: Project[] = [
  {
    id: 'workshop',
    name: 'Building an Enterprise Agentic AI Platform',
    shortName: 'Workshop',
    description:
      'Hands-on AWS workshop for building an enterprise landing-zone pattern for agentic AI on Amazon Bedrock and Amazon Bedrock AgentCore.',
    tagline: 'Learn the platform patterns hands-on',
    stage: 'learn',
    stageLabel: 'Learn',
    stageNumber: 1,
    folder: 'workshop-building-agentic-ai-platform',
    route: '/projects/workshop/',
    highlights: [
      'LLM Gateway with LiteLLM',
      'MCP Gateway and Registry',
      'Strands Agents and FAST',
      'Published on AWS Builder Center',
    ],
    bestFor: 'Platform and ML engineers who want to understand and build the foundation.',
    features: [
      'Five modules (1, 2, 3a, 3b, 4) with a track selector at the end of Module 1',
      'Three workshop tracks: Fast Path, Build the Platform, Full Journey',
      'Hands-on with real AWS resources in a browser-based Code Editor IDE',
      'Run it at an AWS event or self-paced in your own account with one deploy script',
      'CLI walkthrough or notebook walkthrough for most module sections',
    ],
    prerequisites: [
      'AWS CLI v2',
      'yq (the deploy script reads contentspec.yaml with it)',
      'Git and a modern browser',
      'A dedicated, disposable AWS account with AdministratorAccess or the seven scoped deploy policies under static/cfn (the self-paced getting-started page lists seven files; the README prerequisites section still says four)',
      'A validated region: us-west-2 (default), us-east-1, or eu-west-1, with Bedrock model access granted there',
      'Familiarity with IAM, Lambda, CloudFormation, ECS Fargate, API Gateway, Cognito, and CloudWatch',
      'Familiarity with Amazon Bedrock and how LLMs use tools (function or tool calling)',
    ],
    sources: {
      features: {
        file: 'workshop-building-agentic-ai-platform/README.md',
        heading: "What you'll build",
        quote: 'The workshop follows two complementary personas',
      },
      prerequisites: {
        file: 'workshop-building-agentic-ai-platform/README.md',
        heading: 'Prerequisites (self-paced)',
        quote: 'the deploy script reads `contentspec.yaml` with it.',
      },
      prerequisitesNote: {
        text: 'The self-paced getting-started page lists seven scoped deploy policy files; the README prerequisites section still says four.',
        source: {
          file: 'workshop-building-agentic-ai-platform/content/introduction/getting-started/self-service.en.md',
          quote: '`static/cfn/self-service-deploy-policy-7.json`',
        },
      },
    },
  },
  {
    id: 'self-service',
    name: 'AgentCore Visual Workflow Platform',
    shortName: 'Self-Service',
    description:
      'Visual workflow builder for Amazon Bedrock AgentCore: design, configure, and deploy agents through a drag-and-drop canvas with templates and enterprise governance.',
    tagline: 'Build and ship agents visually',
    stage: 'build',
    stageLabel: 'Build',
    stageNumber: 2,
    folder: 'Agentic-ai-self-service',
    route: '/projects/self-service/',
    highlights: [
      'Visual canvas editor',
      'Template gallery and CloudFormation export',
      '13 model providers',
      'Versioning and rollback',
    ],
    bestFor: 'Engineers who want to build and ship agents fast on top of AgentCore.',
    features: [
      'Drag-and-drop AgentCore components: Runtime, Gateway, Memory, Knowledge Base, Browser, Identity, Observability, Policy, Connectors',
      'Template gallery with six one-click templates',
      'CloudFormation and Python export',
      'Real SaaS connectors: Jira, Asana, Slack, GitHub, Salesforce, or any OpenAPI spec',
      '13 model providers',
      'Scope-based RBAC (advisory by default) and Cedar policy enforcement per Policy node',
      'Agent registry with approval workflow, versioning and rollback, cost budgets, audit analytics',
      'Manifest-driven teardown with no orphans',
    ],
    prerequisites: [
      'AWS CLI v2 configured for the target account',
      'Node.js 20+ (CI runs on 22)',
      'Python 3.12+',
      'Any AWS region; us-east-1 is the default and the only region with a CloudFront-scoped WAF',
      'No Docker required; CDK runs through npx',
    ],
    sources: {
      features: {
        file: 'Agentic-ai-self-service/README.md',
        heading: 'Key Features',
        quote: 'Jira, Asana, Slack, GitHub, Salesforce, or any OpenAPI spec as Gateway targets',
      },
      prerequisites: {
        file: 'Agentic-ai-self-service/README.md',
        heading: 'Prerequisites',
        quote: 'No Docker installation required. CDK is invoked via `npx`',
      },
    },
  },
  {
    id: 'mcp-gateway',
    name: 'Enterprise MCP Governance Gateway',
    shortName: 'MCP Gateway',
    description:
      'Deployable governance layer that places Amazon Bedrock AgentCore Gateway in front of MCP tool servers with JWT authentication, Cedar policies in ENFORCE mode, Lambda interceptors and a Bedrock Guardrail.',
    tagline: 'Govern every tool call',
    stage: 'govern',
    stageLabel: 'Govern',
    stageNumber: 3,
    folder: 'enterprise-mcp-governance-gateway',
    route: '/projects/mcp-gateway/',
    highlights: [
      'Cedar policy enforcement',
      'Cognito JWT authentication',
      'Bedrock Guardrail screening',
      'Request and response interceptors',
    ],
    bestFor: 'Platform and security engineers who need per-tool-call authorization and audit.',
    features: [
      'Single governed MCP endpoint in front of MCP tool servers',
      'Cognito OIDC JWT authentication (CUSTOM_JWT authorizer)',
      'Cedar policy engine in ENFORCE mode, one policy per statement',
      'Request and response Lambda interceptors (SQL-injection blocking, business-hours gating, payload truncation)',
      'Managed Bedrock Guardrail with regex PII redaction as defense in depth, plus structured audit logging',
      'Optional per-user OAuth 3LO connectors (Atlassian)',
      'Five governance tests that run against the live gateway, never mocked',
    ],
    prerequisites: [
      'Node.js and the AWS CDK CLI pinned to 2.1129.0 (npm install -g aws-cdk@2.1129.0)',
      'Python 3.12+ with the CDK Python dependencies in a virtualenv',
      'AWS credentials for the target account; us-west-2 by default',
      'A running container runtime (Docker, Finch, or Podman) for the optional connector stacks only',
    ],
    sources: {
      features: {
        file: 'enterprise-mcp-governance-gateway/README.md',
        quote: 'A real, deployed governance layer that sits in front of MCP tool servers using',
      },
      prerequisites: {
        file: 'enterprise-mcp-governance-gateway/README.md',
        heading: 'Deploy',
        quote: 'pinned to the version this sample was',
      },
    },
  },
  {
    id: 'blueprint',
    name: 'Enterprise Agentic AI Platform Blueprint',
    shortName: 'Blueprint',
    description:
      'Multi-account AWS CDK reference blueprint for an enterprise Agent Factory with AWS Organizations, SCPs, CDK Pipelines, evaluation gates and a documented support envelope.',
    tagline: 'Evaluate the enterprise reference',
    stage: 'scale',
    stageLabel: 'Scale',
    stageNumber: 4,
    folder: 'enterprise-agentic-ai-platform-blueprint',
    route: '/projects/blueprint/',
    highlights: [
      'AWS Organizations and 12 SCPs',
      'AgentCore Runtime, Gateway, Memory, Identity',
      'CDK Pipelines with an evaluation gate',
      'Cost attribution and OAM observability',
    ],
    bestFor: 'Platform and security engineers building enterprise-scale infrastructure.',
    features: [
      'Multi-account architecture with Management, Platform, and Workstream account roles',
      'Service control policies for model, Region, Guardrail, Registry, Gateway, and deployment boundaries',
      'Per-tenant Application Inference Profiles',
      'Private VPC with interface endpoints (no NAT)',
      'Evaluation gate in the Runtime/Memory pipeline shape',
      'Fleet observability with CloudWatch OAM',
      'Adversarial evidence model: every negative test needs an authorized positive twin',
    ],
    prerequisites: [
      'Node.js 20 or later and Python 3.12 or later',
      'AWS CLI v2 and AWS CDK v2',
      'An AWS Organizations landing zone with at least Management, Platform, and Workstream account roles',
      'A GitHub organization, repository strategy, and an AWS CodeConnections connection',
      'Access to the selected Bedrock model in the target Region (eu-west-1 is the validated reference)',
      'Administrator access for the initial bootstrap only; pipelines use generated scoped execution policies',
    ],
    sources: {
      features: {
        file: 'enterprise-agentic-ai-platform-blueprint/README.md',
        heading: '10.1 Control summary',
        quote: 'Organizations SCPs for model, Region, Guardrail, Registry, Gateway, and deployment boundaries.',
      },
      prerequisites: {
        file: 'enterprise-agentic-ai-platform-blueprint/README.md',
        heading: '5. Prerequisites',
        quote: 'A GitHub organization, repository strategy, and AWS CodeConnections connection.',
      },
    },
  },
];

export const capabilities: Capability[] = [
  {
    id: 'llm-gateway',
    name: 'LLM Gateway',
    description: 'Governed access to foundation models with authentication, routing, policy enforcement, and usage attribution.',
    implementations: ['AgentCore Gateway inference targets', 'LiteLLM'],
    contractNote: 'Central, non-bypassable authentication with tenant context, model allow-listing, and telemetry.',
  },
  {
    id: 'tool-gateway',
    name: 'Tool Gateway',
    description: 'Authenticated MCP discovery and invocation with least-privilege execution and policy enforcement.',
    implementations: ['AgentCore Gateway (AWS_IAM in the Blueprint; Cognito JWT authorizer in the Workshop, Self-Service and MCP Gateway)'],
    contractNote: 'Exact approved targets, tenant propagation, audit, and failure isolation.',
  },
  {
    id: 'runtime',
    name: 'Agent Runtime',
    description: 'Immutable deployable agent execution with workload identity, isolation, and invocation contracts.',
    implementations: ['AgentCore Runtime'],
    contractNote: 'Health, scaling, logs, safe update, and rollback capabilities.',
  },
  {
    id: 'memory',
    name: 'Agent Memory',
    description: 'Actor-scoped event storage with encryption and retention policies.',
    implementations: ['AgentCore Memory with CMK'],
    contractNote: 'Actor isolation, encryption, deletion lifecycle, and auditable access.',
  },
  {
    id: 'identity',
    name: 'Identity',
    description: 'Short-lived credentials with tenant binding and traceable identity exchange.',
    implementations: ['AgentCore Identity', 'Cognito M2M'],
    contractNote: 'Tenant binding, rotation, revocation, and no secret exposure.',
  },
  {
    id: 'registry',
    name: 'Registry',
    description: 'Ownership, lifecycle state, and approval separation for agents and tools.',
    implementations: ['AWS Agent Registry'],
    contractNote: 'Immutable descriptor identity, versioning, and prevention of unapproved use.',
  },
  {
    id: 'policy',
    name: 'Policy Engine',
    description: 'Fail-closed authorization with explicit context, versioning, and decision telemetry.',
    implementations: ['AgentCore PolicyEngine', 'Cedar'],
    contractNote: 'Positive and negative tests and rollback capability.',
  },
  {
    id: 'delivery',
    name: 'Delivery',
    description: 'Reviewed source, reproducible build, scanning, promotion gates, and rollback.',
    implementations: ['CodePipeline', 'CodeBuild', 'ECR'],
    contractNote: 'Image scanning, nonproduction proof, approval, and rollback.',
  },
  {
    id: 'observability',
    name: 'Observability',
    description: 'Correlated logs, metrics, and traces with cell and fleet views.',
    implementations: ['CloudWatch', 'X-Ray', 'OAM'],
    contractNote: 'Access separation, retention, alarms, and request attribution.',
  },
  {
    id: 'cost',
    name: 'Cost',
    description: 'Attribution by application, agent, tenant, and environment with budgets.',
    implementations: ['Allocation tags', 'Budgets', 'CUR'],
    contractNote: 'Shared-cost policy, anomaly response, and portfolio reporting.',
  },
];

/** Top navigation for the new information architecture. All paths end with a slash. */
export const navigation: NavItem[] = [
  { path: '/start/', label: 'Get started' },
  {
    path: '/concepts/agent-factory/',
    label: 'Concepts',
    children: [
      { path: '/concepts/agent-factory/', label: 'Agent Factory' },
      { path: '/concepts/capability-contracts/', label: 'Capability contracts' },
      { path: '/concepts/architecture/', label: 'Architecture' },
      { path: '/concepts/glossary/', label: 'Glossary' },
    ],
  },
  {
    path: '/projects/',
    label: 'Projects',
    children: [
      { path: '/projects/workshop/', label: 'Workshop' },
      { path: '/projects/self-service/', label: 'Self-Service' },
      { path: '/projects/mcp-gateway/', label: 'MCP Gateway' },
      { path: '/projects/blueprint/', label: 'Blueprint' },
    ],
  },
  {
    path: '/reference/security/',
    label: 'Reference',
    children: [
      { path: '/reference/security/', label: 'Security' },
      { path: '/reference/support-envelope/', label: 'Support envelope' },
    ],
  },
];

/** Find the project that represents a journey stage. */
export function getProjectByStage(stage: JourneyStage): Project | undefined {
  return projects.find(p => p.stage === stage);
}

/** Find a project by its route segment. Accepts any string so route params can be passed directly. */
export function getProjectById(id: string): Project | undefined {
  return projects.find(p => p.id === id);
}
