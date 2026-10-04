/**
 * Capability-by-project and security-by-project matrices.
 *
 * Each cell states a posture, a short text and a source. Postures are
 * deliberately coarse so the reader can compare projects at a glance; the
 * text carries the nuance.
 */
import type { ProjectId } from './data';
import type { Source } from './facts';

/** How strongly a project delivers a capability or control. */
export type Posture = 'enforced' | 'advisory' | 'illustrative' | 'outside-envelope' | 'not-applicable';

/** Display labels and one-line meanings for each posture. */
export const POSTURE_LABELS: Record<Posture, { label: string; meaning: string }> = {
  enforced: { label: 'Enforced', meaning: 'Active in the deployed reference implementation.' },
  advisory: { label: 'Advisory by default', meaning: 'Present, but logs or opt-in rather than blocking until you switch it on.' },
  illustrative: { label: 'Illustrative', meaning: 'Taught or demonstrated; not positioned as a production control.' },
  'outside-envelope': { label: 'Outside envelope', meaning: 'Documented by the project as outside its tested support envelope.' },
  'not-applicable': { label: 'Not applicable', meaning: 'Not part of this project.' },
};

/** One cell of a matrix. */
export interface MatrixCell {
  /** Posture for the cell. */
  posture: Posture;
  /** Short plain-language text. */
  text: string;
  /** Where the statement comes from. */
  source?: Source;
}

/** A capability row: one cell per project. */
export interface CapabilityRow {
  /** Matches `Capability.id` in data.ts. */
  capabilityId: string;
  /** Display name. */
  name: string;
  /** One cell per project. */
  cells: Record<ProjectId, MatrixCell>;
}

/** Security control rows. */
export type SecurityControlId = 'authn' | 'authz' | 'encryption' | 'network' | 'audit' | 'guardrails';

/** A security row: one cell per project. */
export interface SecurityRow {
  /** Control id. */
  id: SecurityControlId;
  /** Display name. */
  name: string;
  /** One cell per project. */
  cells: Record<ProjectId, MatrixCell>;
}

const ROOT_README = 'README.md';
const WORKSHOP_README = 'workshop-building-agentic-ai-platform/README.md';
const WORKSHOP_SELF_PACED_PAGE = 'workshop-building-agentic-ai-platform/content/introduction/getting-started/self-service.en.md';
const WORKSHOP_MODULE_3A = 'workshop-building-agentic-ai-platform/content/module-3a/index.en.md';
const WORKSHOP_MODULE_3B = 'workshop-building-agentic-ai-platform/content/module-3b/index.en.md';
const WORKSHOP_MODULE_3B_STEP_7 = 'workshop-building-agentic-ai-platform/content/module-3b/step-7/index.en.md';
const WORKSHOP_MODULE_4 = 'workshop-building-agentic-ai-platform/content/module-4/index.en.md';
const WORKSHOP_REGISTRY_DATA_STACK = 'workshop-building-agentic-ai-platform/static/cfn/registry/data-stack.yaml';
const SELF_SERVICE_README = 'Agentic-ai-self-service/README.md';
const SELF_SERVICE_COSTS = 'Agentic-ai-self-service/docs/COSTS.md';
const SELF_SERVICE_CAPABILITIES = 'Agentic-ai-self-service/docs/ENTERPRISE_CAPABILITIES.md';
const SELF_SERVICE_SECURITY = 'Agentic-ai-self-service/docs/SECURITY_HARDENING.md';
const SELF_SERVICE_RETENTION = 'Agentic-ai-self-service/docs/DATA_RETENTION.md';
const SELF_SERVICE_INTERNALS = 'Agentic-ai-self-service/docs/DEPLOYMENT_INTERNALS.md';
const SELF_SERVICE_RBAC_ROLLOUT = 'Agentic-ai-self-service/docs/RBAC_ROLLOUT.md';
const GATEWAY_README = 'enterprise-mcp-governance-gateway/README.md';
const GATEWAY_MANIFEST = 'enterprise-mcp-governance-gateway/policies/manifest.json';
const BLUEPRINT_README = 'enterprise-agentic-ai-platform-blueprint/README.md';
const BLUEPRINT_VPC_CONSTRUCT = 'enterprise-agentic-ai-platform-blueprint/packages/agentic-vpc/src/agentic-vpc-construct.ts';

const BLUEPRINT_CONTRACTS = 'Capability contracts and replaceable implementations';
const BLUEPRINT_CONTROLS = '10.1 Control summary';

export const capabilityMatrix: CapabilityRow[] = [
  {
    capabilityId: 'llm-gateway',
    name: 'LLM Gateway',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'Module 2 deploys LiteLLM Proxy on ECS Fargate for governed, cost-attributed access to Amazon Bedrock models.',
        source: {
          file: WORKSHOP_README,
          heading: "What you'll build",
          quote: 'deploy LiteLLM Proxy on ECS Fargate for governed, cost-attributed',
        },
      },
      'self-service': {
        posture: 'not-applicable',
        text: 'No LLM gateway layer. Agents call one of 13 model providers directly (Bedrock by default). LiteLLM appears only as an optional MCP gateway or registry catalog.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Key Features',
          quote: 'Bedrock (default), OpenAI, Anthropic, Gemini, Mistral, Ollama, Groq',
        },
      },
      'mcp-gateway': {
        posture: 'not-applicable',
        text: 'Governs tool calls only; model access is out of scope.',
        source: {
          file: GATEWAY_README,
          quote: 'A real, deployed governance layer that sits in front of MCP tool servers using',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'AgentCore Gateway inference targets with a mandatory Guardrail interceptor and a model allow-list; LiteLLMModel is the agent client.',
        source: {
          file: BLUEPRINT_README,
          heading: BLUEPRINT_CONTRACTS,
          quote: 'AgentCore Gateway with inference targets; `LiteLLMModel` is the current agent client.',
        },
      },
    },
  },
  {
    capabilityId: 'tool-gateway',
    name: 'Tool Gateway',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'Module 3a layers an AgentCore Tools Gateway over the MCP Gateway & Registry for JWT auth, audit and guardrails; Module 3b uses AgentCore Gateway with Lambda targets.',
        source: {
          file: WORKSHOP_README,
          heading: "What you'll build",
          quote: 'layer an AgentCore Tools Gateway on top for JWT auth, audit, and guardrails',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'Selected tools deploy as a single Lambda behind an AgentCore Gateway with Cognito OAuth2; agents discover them at runtime via tools/list.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Key Features',
          quote: 'Selected tools deploy as a single Lambda behind an MCP Gateway with Cognito OAuth2',
        },
      },
      'mcp-gateway': {
        posture: 'enforced',
        text: 'AgentCore Gateway with a CUSTOM_JWT (Cognito) authorizer in front of two sample Lambda targets; one governed MCP endpoint.',
        source: {
          file: GATEWAY_README,
          quote: '**Authenticates** every caller with a JWT (Amazon Cognito OIDC, inbound auth).',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'An AWS_IAM AgentCore Tool Gateway per Workstream cell; targets derived from approved Registry records.',
        source: {
          file: BLUEPRINT_README,
          heading: '2.3 Repeatable Workstream cell',
          quote: 'an `AWS_IAM` AgentCore Tool Gateway;',
        },
      },
    },
  },
  {
    capabilityId: 'runtime',
    name: 'Agent Runtime',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'Module 4 deploys a FAST travel agent on AgentCore Runtime, wired to the platform through the MCP path or the AgentCore path.',
        source: {
          file: WORKSHOP_README,
          heading: "What you'll build",
          quote: 'deploy a full-stack travel agent using FAST (Fullstack',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'Canvas agents deploy as code-generated AgentCore Runtime or as a config-driven AgentCore Harness.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Key Features',
          quote: 'code-generated AgentCore **Runtime**) or as a config-driven **AgentCore Harness**',
        },
      },
      'mcp-gateway': {
        posture: 'not-applicable',
        text: 'No agent runtime. An external MCP client such as Kiro or Claude Code drives the gateway; only the optional Atlassian connector runs a server on AgentCore Runtime.',
        source: {
          file: GATEWAY_README,
          quote: 'behind one MCP URL that an MCP client (e.g. Kiro) connects to.',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'Nonproduction and production AgentCore Runtime per Workstream cell, deployed as immutable revisions with rollback.',
        source: {
          file: BLUEPRINT_README,
          heading: '2.3 Repeatable Workstream cell',
          quote: 'nonproduction and production AgentCore Runtime and Memory resources;',
        },
      },
    },
  },
  {
    capabilityId: 'memory',
    name: 'Agent Memory',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'The Module 4 FAST agent ships with conversation memory.',
        source: {
          file: WORKSHOP_MODULE_4,
          heading: 'The scenario',
          quote: 'AgentCore Runtime backend, and conversation memory.',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'A Memory node on the canvas; teardown removes the memories it created.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Key Features',
          quote: 'Drag-and-drop AgentCore components (Runtime, Gateway, Memory, Knowledge Base',
        },
      },
      'mcp-gateway': {
        posture: 'not-applicable',
        text: 'Not part of this project.',
      },
      blueprint: {
        posture: 'enforced',
        text: 'AgentCore Memory with actor-scoped events and customer-managed KMS keys.',
        source: {
          file: BLUEPRINT_README,
          heading: BLUEPRINT_CONTRACTS,
          quote: 'AgentCore Memory with actor-scoped events and customer-managed KMS keys',
        },
      },
    },
  },
  {
    capabilityId: 'identity',
    name: 'Identity',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'Cognito and WorkloadIdentity provide human and machine identity in Module 3b; three IAM persona roles separate duties.',
        source: {
          file: WORKSHOP_MODULE_3B,
          heading: 'What you will learn',
          quote: 'How Cognito and WorkloadIdentity provide human and machine identity',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'Cognito user pool for people, an Identity node for agents, and connector credentials that live only in Secrets Manager.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Key Features',
          quote: 'credentials live only in Secrets Manager',
        },
      },
      'mcp-gateway': {
        posture: 'enforced',
        text: 'Cognito OIDC JWT inbound authentication; per-user OAuth 3LO for the optional connectors.',
        source: {
          file: GATEWAY_README,
          quote: '**Authenticates** every caller with a JWT (Amazon Cognito OIDC, inbound auth).',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'AgentCore Identity plus Cognito M2M short-lived credentials.',
        source: {
          file: BLUEPRINT_README,
          heading: BLUEPRINT_CONTROLS,
          quote: 'Cognito M2M and AgentCore Identity short-lived credentials.',
        },
      },
    },
  },
  {
    capabilityId: 'registry',
    name: 'Registry',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'Module 3a uses the open-source MCP Gateway & Registry; Module 3b creates an AgentCore Registry with a Publisher and Admin approval workflow.',
        source: {
          file: WORKSHOP_MODULE_3B,
          quote: 'Created an AgentCore Registry and registered 3 MCP tools with metadata',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'Built-in agent registry with an approval workflow (registry-admin and registry-developer personas); a LiteLLM proxy can become the catalog.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Key Features',
          quote: 'agent registry with approval workflow, versioning & rollback',
        },
      },
      'mcp-gateway': {
        posture: 'not-applicable',
        text: 'No registry. The README positions the gateway a layer below platforms that manage which agents and servers exist.',
        source: {
          file: GATEWAY_README,
          heading: 'Related projects',
          quote: 'This sample is complementary and sits a layer lower',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'AWS Agent Registry governance records drive Gateway targets.',
        source: {
          file: BLUEPRINT_README,
          heading: BLUEPRINT_CONTRACTS,
          quote: 'AWS Agent Registry and approved governance records',
        },
      },
    },
  },
  {
    capabilityId: 'policy',
    name: 'Policy Engine',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'Module 3b Part C creates an AgentCore Policy Engine with Cedar policies and attaches it in LOG_ONLY mode; in this setup ENFORCE would empty tools/list.',
        source: {
          file: WORKSHOP_MODULE_3B_STEP_7,
          quote: 'Attach with `"mode": "LOG_ONLY"`, not `"ENFORCE"`.',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'Cedar ENFORCE per Policy node (fail-closed, converge-in-place); scope-based RBAC is advisory by default.',
        source: {
          file: SELF_SERVICE_CAPABILITIES,
          heading: 'Agent lifecycle & quality',
          quote: 'When a Policy node runs in `ENFORCE` mode, the policy step builds a schema-correct Cedar policy set',
        },
      },
      'mcp-gateway': {
        posture: 'enforced',
        text: 'Cedar ENFORCE at the gateway with one policy per statement; policies are created with validationMode IGNORE_ALL_FINDINGS.',
        source: {
          file: GATEWAY_MANIFEST,
          quote: '"validationMode": "IGNORE_ALL_FINDINGS"',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'AgentCore Policy Engine plus a retained Lambda Cedar wrapper; 12 SCPs for model, Region, Guardrail, Registry, Gateway and deployment boundaries.',
        source: {
          file: BLUEPRINT_README,
          heading: BLUEPRINT_CONTRACTS,
          quote: 'AgentCore PolicyEngine, IAM/SCP controls, and the retained Lambda Cedar wrapper',
        },
      },
    },
  },
  {
    capabilityId: 'delivery',
    name: 'Delivery',
    cells: {
      workshop: {
        posture: 'not-applicable',
        text: 'Not covered. The five stacks deploy through a shell wrapper with a preflight check.',
        source: {
          file: WORKSHOP_README,
          heading: 'Repository structure',
          quote: 'Self-paced deploy wrapper (preflight + delegate to deploy-cfn.sh)',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'A Step Functions deployment pipeline (validate, codegen, IAM, runtime, evaluation) with versioning and rollback; no source-review gate.',
        source: {
          file: SELF_SERVICE_COSTS,
          heading: 'AWS Resources Created',
          quote: 'Orchestrates multi-step deployments: validate ->',
        },
      },
      'mcp-gateway': {
        posture: 'not-applicable',
        text: 'Deployed with cdk deploy from a workstation; no pipeline.',
        source: {
          file: GATEWAY_README,
          heading: 'Quickstart',
          quote: 'cd cdk && cdk bootstrap && cdk deploy EnterpriseMcpGatewayStack --require-approval never && cd ..',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'GitHub, CodeConnections, CodePipeline, CodeBuild and ECR; digest-bound image scanning blocks Critical or High findings; human approval after deployed-runtime evaluation.',
        source: {
          file: BLUEPRINT_README,
          heading: BLUEPRINT_CONTROLS,
          quote: 'Digest-bound ECR image scanning that blocks Critical or High findings.',
        },
      },
    },
  },
  {
    capabilityId: 'observability',
    name: 'Observability',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'Grafana dashboards backed by Amazon Managed Service for Prometheus in Module 3a; Module 4 inspects traces, logs and memory.',
        source: {
          file: WORKSHOP_MODULE_3A,
          heading: "What's already deployed",
          quote: 'Grafana dashboards backed by Amazon Managed Service for Prometheus',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'Per-canvas and platform-level OTEL modes; OTLP traces from every platform Lambda and deployed agent can go to a backend such as Langfuse.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Quickstart',
          quote: 'export OTLP traces from every platform Lambda and deployed agent',
        },
      },
      'mcp-gateway': {
        posture: 'enforced',
        text: 'One structured request_audit record per call in the interceptor logs; refused calls are audited too.',
        source: {
          file: GATEWAY_README,
          heading: '3. See the audit trail',
          quote: 'one structured `request_audit` record per call (user, tool, args keys)',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'CloudWatch OAM links for centralized Logs, Metrics and Traces; Transaction Search is opt-in.',
        source: {
          file: BLUEPRINT_README,
          heading: BLUEPRINT_CONTROLS,
          quote: 'OAM links for centralized Logs, Metrics, and Traces.',
        },
      },
    },
  },
  {
    capabilityId: 'cost',
    name: 'Cost',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'The Module 2 LLM Gateway gives cost-attributed model access through LiteLLM virtual keys and budgets.',
        source: {
          file: WORKSHOP_MODULE_4,
          heading: 'The scenario',
          quote: 'with virtual keys, budgets, and guardrails',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'Cost budgets, usage events and audit analytics; a budget breach emits a CloudWatch metric.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Key Features',
          quote: 'cost budgets, audit analytics, HITL approvals',
        },
      },
      'mcp-gateway': {
        posture: 'not-applicable',
        text: 'No cost attribution controls in this sample.',
      },
      blueprint: {
        posture: 'enforced',
        text: 'Five allocation tags, per-application budgets, account-level Bedrock quotas and CUR reconciliation.',
        source: {
          file: BLUEPRINT_README,
          heading: BLUEPRINT_CONTROLS,
          quote: 'Five allocation tags: `application-id`, `agent-id`, `tenant-id`, `cost-centre`, and `environment`.',
        },
      },
    },
  },
];

export const securityMatrix: SecurityRow[] = [
  {
    id: 'authn',
    name: 'Authentication',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'Cognito JWT on the Tools Gateway and the AgentCore Registry; Admin, Publisher and Consumer IAM persona roles; the scoped WSParticipantRole at events.',
        source: {
          file: WORKSHOP_MODULE_3B,
          heading: 'Steps',
          quote: 'Create an AgentCore Registry with Cognito JWT auth',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'Cognito user pool with SRP only (USER_PASSWORD_AUTH disabled) and user-existence errors prevented; OIDC federation available.',
        source: {
          file: SELF_SERVICE_SECURITY,
          heading: 'Infrastructure Hardening',
          quote: '`prevent_user_existence_errors=ENABLED`, `USER_PASSWORD_AUTH` disabled (SRP only)',
        },
      },
      'mcp-gateway': {
        posture: 'enforced',
        text: 'CUSTOM_JWT authorizer validates Cognito access tokens against the OIDC discovery URL; the demo app client allows only the IAM-gated ADMIN_USER_PASSWORD_AUTH flow.',
        source: {
          file: GATEWAY_README,
          heading: 'Identity & secrets',
          quote: 'The app client enables **only `ADMIN_USER_PASSWORD_AUTH`** (IAM-gated',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'AWS_IAM (SigV4) for the Tool Gateway; AgentCore Identity and Cognito M2M CUSTOM_JWT for inference.',
        source: {
          file: BLUEPRINT_README,
          heading: '14. Architecture decision record',
          quote: 'AgentCore Identity and Cognito M2M/CUSTOM_JWT for inference and AWS_IAM/SigV4 for tools',
        },
      },
    },
  },
  {
    id: 'authz',
    name: 'Authorization',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'A request interceptor enforces a TOOL_ACCESS_POLICY by Cognito group; Cedar policies on a Policy Engine run in LOG_ONLY mode (Module 3b).',
        source: {
          file: WORKSHOP_MODULE_3B_STEP_7,
          heading: 'Part B: Group-based tool access control',
          quote: 'The request interceptor Lambda (`ac-gateway-request-interceptor`) enforces group-based access policies.',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'Scope-based RBAC ships enforcing by default (RBAC_ENFORCE=true); owner-scoped tenant isolation is always enforced; Cedar ENFORCE applies per Policy node.',
        source: {
          file: SELF_SERVICE_RBAC_ROLLOUT,
          heading: 'RBAC Enforcement Rollout Runbook',
          quote: 'Scope-based RBAC (`services/rbac.py`) ships **enforcing by default**',
        },
      },
      'mcp-gateway': {
        posture: 'enforced',
        text: 'Cedar ENFORCE per tool call with default deny. Role-gated permits never fire for demo users because custom:role is only in the ID token.',
        source: {
          file: GATEWAY_README,
          quote: '`custom:role` never reaches the access token the gateway validates (documented limitation)',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'Organizations SCPs, AgentCore PolicyEngine, the retained Lambda Cedar wrapper, exact Lambda alias ARNs and exact role principals.',
        source: {
          file: BLUEPRINT_README,
          heading: BLUEPRINT_CONTROLS,
          quote: 'Organizations SCPs for model, Region, Guardrail, Registry, Gateway, and deployment boundaries.',
        },
      },
    },
  },
  {
    id: 'encryption',
    name: 'Encryption',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'The registry data stack creates a customer-managed KMS key for the DocumentDB store; see the CloudFormation templates under static/cfn for the other stores.',
        source: {
          file: WORKSHOP_REGISTRY_DATA_STACK,
          quote: '  DocumentDBKmsKey:\n    Type: AWS::KMS::Key',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'DynamoDB tables use AWS-managed SSE; S3 buckets block public access and enforce SSL; the SNS topic uses SSE with enforced TLS; CloudFront requires TLS 1.2.',
        source: {
          file: SELF_SERVICE_RETENTION,
          heading: 'Encryption',
          quote: 'All tables use AWS-managed SSE (DynamoDB default).',
        },
      },
      'mcp-gateway': {
        posture: 'enforced',
        text: 'One customer-managed KMS key with annual rotation for the gateway, the policy engine and its policies, and the demo credential secret.',
        source: {
          file: GATEWAY_README,
          heading: 'Identity & secrets',
          quote: '**Encryption at rest uses a customer-managed KMS key** (one CMK, annual rotation',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'Customer-managed KMS keys for Memory and per-cell resources; Secrets Manager and KMS for secrets.',
        source: {
          file: BLUEPRINT_README,
          heading: BLUEPRINT_CONTROLS,
          quote: 'Actor-scoped AgentCore Memory and customer-managed KMS keys.',
        },
      },
    },
  },
  {
    id: 'network',
    name: 'Network',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'Two VPCs (LLM gateway and registry), each with its own NAT gateway; public load balancers and CloudFront for the IDE.',
        source: {
          file: WORKSHOP_SELF_PACED_PAGE,
          heading: 'Service quota headroom',
          quote: 'each with its own NAT gateway',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'WAF web ACL (CLOUDFRONT scope in us-east-1, REGIONAL on the Cognito pool elsewhere); the control plane has no VPC egress; runtimes can use VPC egress through named profiles; optional PrivateLink ingress add-on.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Deploying to another region',
          quote: 'One `CLOUDFRONT`-scoped WebACL on the CloudFront distribution',
        },
      },
      'mcp-gateway': {
        posture: 'not-applicable',
        text: 'No network isolation is described. The gateway is a public AgentCore endpoint authenticated by JWT.',
        source: {
          file: GATEWAY_README,
          quote: 'https://<gateway-id>.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'Private VPC with three AZs and private-isolated subnets only (no internet gateway, no NAT) reaching AWS services through interface endpoints. The live-validated Workstream Runtime stack sets networkMode PUBLIC; VPC network mode is documented as a follow-on. VPC Lattice private endpoints are outside the envelope.',
        source: {
          file: BLUEPRINT_VPC_CONSTRUCT,
          quote: 'VPC with 3 AZs, private-isolated subnets only (no IGW, no NAT).',
        },
      },
    },
  },
  {
    id: 'audit',
    name: 'Audit',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'Request and response interceptors write audit trails for tool calls in Modules 3a and 3b.',
        source: {
          file: WORKSHOP_MODULE_3B,
          heading: 'What you will learn',
          quote: 'How request and response interceptors enforce audit trails and content safety',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'An audit table stores one row per control-plane write (actor sub, action, path, status) with a 90-day TTL; readable by super-admins only.',
        source: {
          file: SELF_SERVICE_RETENTION,
          quote: 'one row per auditable control-plane WRITE action',
        },
      },
      'mcp-gateway': {
        posture: 'enforced',
        text: 'Structured request_audit and request_blocked records per call; refused calls are audited; the user is the Cognito sub; argument values are never logged.',
        source: {
          file: GATEWAY_README,
          heading: '3. See the audit trail',
          quote: '**Refused calls are audited too**',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'CloudTrail and retained audit data corroborate control-plane actions; OAM links make telemetry queryable from Management.',
        source: {
          file: BLUEPRINT_README,
          heading: 'Telemetry and assurance flow',
          quote: 'CloudTrail and retained audit data provide independent control-plane corroboration.',
        },
      },
    },
  },
  {
    id: 'guardrails',
    name: 'Guardrails',
    cells: {
      workshop: {
        posture: 'illustrative',
        text: 'Module 3b step 7 wires a Bedrock guardrail to the response interceptor to screen tool output; the Module 2 LLM Gateway carries guardrails too.',
        source: {
          file: WORKSHOP_MODULE_3B_STEP_7,
          heading: 'Part A: Bedrock Guardrails on tool output',
          quote: '## Part A: Bedrock Guardrails on tool output',
        },
      },
      'self-service': {
        posture: 'enforced',
        text: 'A Guardrails node on the canvas with contextual grounding, regex and injection-defense configurations; guardrail creation is idempotent.',
        source: {
          file: SELF_SERVICE_INTERNALS,
          quote: 'Contextual grounding / regex / injection-defense configs',
        },
      },
      'mcp-gateway': {
        posture: 'enforced',
        text: 'Managed Bedrock Guardrail via ApplyGuardrail on requests (prompt attack, content filters) and responses (PII anonymized). It uses the unpinned DRAFT version, and a guardrail API error is logged while the local regex controls still apply.',
        source: {
          file: GATEWAY_README,
          heading: 'Managed guardrail (Amazon Bedrock Guardrails)',
          quote: 'A guardrail API error on the request path is',
        },
      },
      blueprint: {
        posture: 'enforced',
        text: 'Mandatory Bedrock Guardrail request interceptor on the Inference Gateway; SCP-02 enforces Guardrail use and SCP-05 denies Guardrail modification.',
        source: {
          file: BLUEPRINT_README,
          heading: BLUEPRINT_CONTROLS,
          quote: 'Mandatory Bedrock Guardrail request interceptor on the Inference Gateway.',
        },
      },
    },
  },
];

/** Root README source for the canonical capability contract statement. */
export const CAPABILITY_CONTRACT_SOURCE: Source = {
  file: ROOT_README,
  heading: 'Capability Architecture',
  quote: 'defines the outcomes and controls that any chosen implementation must preserve',
};
