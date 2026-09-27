export type JourneyStage = 'learn' | 'build' | 'govern' | 'scale';

export interface Project {
  id: string;
  name: string;
  shortName: string;
  description: string;
  stage: JourneyStage;
  stageLabel: string;
  stageNumber: number;
  color: string;
  folder: string;
  highlights: string[];
  bestFor: string;
  features: string[];
  prerequisites: string[];
}

export interface Capability {
  id: string;
  name: string;
  description: string;
  implementations: string[];
  contractNote?: string;
}

export interface NavItem {
  path: string;
  label: string;
  children?: NavItem[];
}

export const STAGE_COLORS: Record<JourneyStage, string> = {
  learn: 'var(--color-amber)',
  build: 'var(--color-green)',
  govern: 'var(--color-violet)',
  scale: 'var(--color-blue)',
};

export const projects: Project[] = [
  {
    id: 'workshop',
    name: 'Building an Enterprise Agentic AI Platform',
    shortName: 'Workshop',
    description: 'Hands-on AWS workshop for building enterprise landing-zone patterns for agentic AI on Amazon Bedrock AgentCore.',
    stage: 'learn',
    stageLabel: 'Learn',
    stageNumber: 1,
    color: 'var(--color-amber)',
    folder: 'workshop-building-agentic-ai-platform',
    highlights: [
      'LLM Gateway with LiteLLM',
      'MCP Gateway and Registry',
      'Strands Agents + Fullstack AgentCore Solution Template (FAST)',
      'Platform patterns',
    ],
    bestFor: 'Platform and ML engineers who want to understand and build the foundation.',
    features: [
      'Multi-module workshop (1.5-4 hours)',
      'Three learning tracks: Fast Path, Build the Platform, Full Journey',
      'Hands-on with real AWS resources',
      'Self-paced or event-based',
    ],
    prerequisites: [
      'AWS CLI v2',
      'Basic familiarity with IAM, Lambda, CloudFormation',
      'Understanding of Amazon Bedrock',
    ],
  },
  {
    id: 'self-service',
    name: 'AgentCore Visual Workflow Platform',
    shortName: 'Self-Service',
    description: 'Drag-and-drop canvas builder to design, configure, and deploy AgentCore agents with templates and enterprise features.',
    stage: 'build',
    stageLabel: 'Build',
    stageNumber: 2,
    color: 'var(--color-green)',
    folder: 'Agentic-ai-self-service',
    highlights: [
      'Visual canvas editor',
      'Template gallery + export',
      'Multiple model providers',
      'Versioning and rollback',
    ],
    bestFor: 'Engineers who want to build and ship agents fast on top of AgentCore.',
    features: [
      'Drag-and-drop AgentCore components',
      'Template gallery',
      'CloudFormation and Python export',
      'Example SaaS connector patterns (Jira, Slack, GitHub)',
      'Cedar policy enforcement',
      'Cost analytics and budgets',
    ],
    prerequisites: [
      'AWS CLI v2',
      'Node.js 20+',
      'Python 3.12+',
    ],
  },
  {
    id: 'mcp-gateway',
    name: 'Enterprise MCP Governance Gateway',
    shortName: 'MCP Gateway',
    description: 'Per-tool-call authorization layer with Cedar policies, JWT authentication, and Bedrock Guardrail screening.',
    stage: 'govern',
    stageLabel: 'Govern',
    stageNumber: 3,
    color: 'var(--color-violet)',
    folder: 'enterprise-mcp-governance-gateway',
    highlights: [
      'Cedar policy enforcement',
      'JWT authentication',
      'Bedrock Guardrail screening',
      'Request/response interceptors',
    ],
    bestFor: 'Platform and security engineers who need per-tool-call authorization and audit.',
    features: [
      'Single governed MCP endpoint',
      'Cognito OIDC authentication',
      'Cedar ENFORCE mode',
      'Lambda interceptors for inspection',
      'PII redaction and audit logging',
      'Optional OAuth 3LO connectors',
    ],
    prerequisites: [
      'Node.js + AWS CDK',
      'Python 3.12+',
      'AWS credentials',
    ],
  },
  {
    id: 'blueprint',
    name: 'Enterprise Agentic AI Platform Blueprint',
    shortName: 'Blueprint',
    description: 'Multi-account AWS CDK reference blueprint for enterprise-scale Agent Factory with Organizations, SCPs, and CDK Pipelines.',
    stage: 'scale',
    stageLabel: 'Scale',
    stageNumber: 4,
    color: 'var(--color-blue)',
    folder: 'enterprise-agentic-ai-platform-blueprint',
    highlights: [
      'AWS Organizations + SCPs',
      'AgentCore Runtime/Gateway/Memory',
      'CDK Pipelines + evaluation gates',
      'Cost attribution + observability',
    ],
    bestFor: 'Platform and security engineers building enterprise-scale infrastructure.',
    features: [
      'Multi-account architecture',
      'Service control policies',
      'Per-tenant Application Inference Profiles',
      'PrivateLink-only egress',
      'Mandatory evaluation gates',
      'Fleet observability with CloudWatch OAM',
    ],
    prerequisites: [
      'AWS Organizations',
      'Node.js 20+ and Python 3.12+',
      'AWS CDK experience',
    ],
  },
];

export const capabilities: Capability[] = [
  {
    id: 'llm-gateway',
    name: 'LLM Gateway',
    description: 'Governed access to foundation models with authentication, routing, policy enforcement, and usage attribution.',
    implementations: ['AgentCore Gateway inference targets', 'LiteLLM Proxy'],
    contractNote: 'Central, non-bypassable authentication with tenant context, model allow-listing, and telemetry.',
  },
  {
    id: 'tool-gateway',
    name: 'Tool / MCP Gateway',
    description: 'Authenticated MCP discovery and invocation with least-privilege execution and policy enforcement.',
    implementations: ['AgentCore Gateway with AWS_IAM', 'MCP Registry'],
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
    description: 'Tenant and actor-scoped event storage with encryption and retention policies.',
    implementations: ['AgentCore Memory', 'Customer-managed KMS'],
    contractNote: 'Actor isolation, encryption, deletion lifecycle, and auditable access.',
  },
  {
    id: 'identity',
    name: 'Identity & Token Brokerage',
    description: 'Short-lived credentials with audience validation and traceable identity exchange.',
    implementations: ['AgentCore Identity', 'Cognito M2M'],
    contractNote: 'Tenant binding, rotation, revocation, and no secret exposure.',
  },
  {
    id: 'registry',
    name: 'Governance Registry',
    description: 'Ownership, lifecycle state, and approval separation for agents, tools, and models.',
    implementations: ['AWS Agent Registry'],
    contractNote: 'Immutable descriptor identity, versioning, and prevention of unapproved use.',
  },
  {
    id: 'policy',
    name: 'Policy Engine',
    description: 'Fail-closed authorization with explicit context, versioning, and decision telemetry.',
    implementations: ['AgentCore PolicyEngine', 'Cedar', 'IAM/SCP'],
    contractNote: 'Positive/negative tests and rollback capability.',
  },
  {
    id: 'delivery',
    name: 'Software Delivery',
    description: 'Reviewed immutable source, reproducible builds, and promotion gates.',
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
    name: 'Cost Governance',
    description: 'Application, agent, tenant, and environment attribution with budgets.',
    implementations: ['Allocation tags', 'Budgets', 'Cost Explorer', 'CUR'],
    contractNote: 'Shared-cost policy, anomaly response, and portfolio reporting.',
  },
];

export const navigation: NavItem[] = [
  { path: '/', label: 'Overview' },
  { path: '/choose-a-path', label: 'Choose Your Path' },
  { path: '/how-it-works', label: 'How It Works' },
  {
    path: '/projects',
    label: 'Projects',
    children: [
      { path: '/projects/workshop', label: 'Workshop' },
      { path: '/projects/self-service', label: 'Self-Service' },
      { path: '/projects/mcp-gateway', label: 'MCP Gateway' },
      { path: '/projects/blueprint', label: 'Blueprint' },
    ],
  },
  { path: '/capabilities', label: 'Capabilities' },
  { path: '/architecture', label: 'Architecture' },
  { path: '/security', label: 'Security' },
  { path: '/getting-started', label: 'Getting Started' },
];

export function getProjectByStage(stage: JourneyStage): Project | undefined {
  return projects.find(p => p.stage === stage);
}

export function getProjectById(id: string): Project | undefined {
  return projects.find(p => p.id === id);
}
