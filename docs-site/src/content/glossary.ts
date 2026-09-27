/**
 * Glossary of terms used across the four projects.
 *
 * Several words (MCP Gateway, Registry, LiteLLM, PrivateLink, Fast Path) mean
 * different things in different projects. Each term carries a general
 * definition and, where the meaning differs, one sourced entry per project.
 */
import type { ProjectId } from './data';
import type { Source } from './facts';

/** How one project uses a term. */
export interface GlossaryMeaning {
  /** Project the meaning applies to. */
  projectId: ProjectId;
  /** Plain-language meaning in that project. */
  meaning: string;
  /** Where the meaning comes from. */
  source: Source;
}

/** A glossary entry. */
export interface GlossaryTerm {
  /** Stable id used for anchors. */
  id: string;
  /** Display term. */
  term: string;
  /** General definition. */
  definition: string;
  /** Per-project meanings when they differ. */
  perProjectMeaning?: GlossaryMeaning[];
  /** Ids of related terms. */
  seeAlso?: string[];
  /** Source for the general definition, when it is taken from the repository. */
  source?: Source;
}

const ROOT_README = 'README.md';
const WORKSHOP_README = 'workshop-building-agentic-ai-platform/README.md';
const WORKSHOP_CONTENTSPEC = 'workshop-building-agentic-ai-platform/contentspec.yaml';
const WORKSHOP_MODULE_3A = 'workshop-building-agentic-ai-platform/content/module-3a/index.en.md';
const WORKSHOP_MODULE_3B = 'workshop-building-agentic-ai-platform/content/module-3b/index.en.md';
const WORKSHOP_MODULE_3B_STEP_7 = 'workshop-building-agentic-ai-platform/content/module-3b/step-7/index.en.md';
const SELF_SERVICE_README = 'Agentic-ai-self-service/README.md';
const SELF_SERVICE_CAPABILITIES = 'Agentic-ai-self-service/docs/ENTERPRISE_CAPABILITIES.md';
const SELF_SERVICE_RBAC_ROLLOUT = 'Agentic-ai-self-service/docs/RBAC_ROLLOUT.md';
const GATEWAY_README = 'enterprise-mcp-governance-gateway/README.md';
const BLUEPRINT_README = 'enterprise-agentic-ai-platform-blueprint/README.md';
const BLUEPRINT_VPC_INDEX = 'enterprise-agentic-ai-platform-blueprint/packages/agentic-vpc/src/index.ts';

export const glossary: GlossaryTerm[] = [
  {
    id: 'mcp',
    term: 'MCP (Model Context Protocol)',
    definition:
      'An open protocol that lets an agent discover and call tools exposed by MCP servers. All four projects use it for tool access; the workshop links the specification from its side navigation.',
    source: {
      file: WORKSHOP_CONTENTSPEC,
      quote: 'title: Model Context Protocol Specification',
    },
    seeAlso: ['mcp-gateway', 'tool-gateway'],
  },
  {
    id: 'mcp-gateway',
    term: 'MCP Gateway',
    definition:
      'A single endpoint that fronts many MCP tool servers so agents connect to one URL. The phrase names four different things in this repository.',
    perProjectMeaning: [
      {
        projectId: 'workshop',
        meaning:
          'The open-source MCP Gateway & Registry (Apache-2.0) that Module 3a uses, deployed on Amazon ECS Fargate with Cognito, DocumentDB and Grafana.',
        source: {
          file: WORKSHOP_MODULE_3A,
          quote: 'Use the pre-deployed **MCP Gateway & Registry**',
        },
      },
      {
        projectId: 'self-service',
        meaning:
          'Either an AgentCore Gateway the platform creates for the selected tools (the default, with Cognito OAuth2) or a customer-run LiteLLM MCP Gateway chosen per agent on the canvas.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Key Features',
          quote: 'Selected tools deploy as a single Lambda behind an MCP Gateway with Cognito OAuth2',
        },
      },
      {
        projectId: 'mcp-gateway',
        meaning:
          'The project itself: an Amazon Bedrock AgentCore Gateway placed in front of MCP tool servers with JWT authentication, Cedar policies and Lambda interceptors.',
        source: {
          file: GATEWAY_README,
          quote: 'A real, deployed governance layer that sits in front of MCP tool servers using',
        },
      },
      {
        projectId: 'blueprint',
        meaning:
          'The Tool / MCP Gateway capability, implemented as AgentCore Gateway with AWS_IAM authentication, MCP targets and PolicyEngine integration inside each Workstream cell.',
        source: {
          file: BLUEPRINT_README,
          heading: 'Capability contracts and replaceable implementations',
          quote: 'AgentCore Gateway with `AWS_IAM`, MCP targets, and PolicyEngine integration',
        },
      },
    ],
    seeAlso: ['tool-gateway', 'registry', 'litellm'],
  },
  {
    id: 'tool-gateway',
    term: 'Tool Gateway (Tools Gateway)',
    definition:
      'The capability that authenticates MCP discovery and invocation and applies policy per tool call. The root README names AgentCore Gateway as the reference implementation: AWS_IAM in the Blueprint, and a Cognito JWT authorizer in the Workshop, Self-Service and MCP Gateway.',
    source: {
      file: ROOT_README,
      heading: 'Capability Architecture',
      quote: 'Authenticated MCP discovery/invocation with policy',
    },
    perProjectMeaning: [
      {
        projectId: 'workshop',
        meaning:
          'The AgentCore Tools Gateway that Module 3a layers on top of the MCP Gateway & Registry for JWT auth, audit and guardrails.',
        source: {
          file: WORKSHOP_README,
          heading: "What you'll build",
          quote: 'layer an AgentCore Tools Gateway on top for JWT auth, audit, and guardrails',
        },
      },
      {
        projectId: 'blueprint',
        meaning: 'The AWS_IAM AgentCore Tool Gateway that every Workstream cell owns.',
        source: {
          file: BLUEPRINT_README,
          heading: '2.3 Repeatable Workstream cell',
          quote: 'an `AWS_IAM` AgentCore Tool Gateway;',
        },
      },
    ],
    seeAlso: ['mcp-gateway', 'agentcore-gateway'],
  },
  {
    id: 'llm-gateway',
    term: 'LLM Gateway',
    definition:
      'Governed access to foundation models with authentication, routing and usage attribution. The root README names AgentCore Gateway inference targets and LiteLLM as reference implementations.',
    source: {
      file: ROOT_README,
      heading: 'Capability Architecture',
      quote: 'Governed model access with auth, routing, and attribution',
    },
    perProjectMeaning: [
      {
        projectId: 'workshop',
        meaning: 'LiteLLM Proxy on ECS Fargate, deployed in Module 2 for governed, cost-attributed access to Amazon Bedrock models.',
        source: {
          file: WORKSHOP_README,
          heading: "What you'll build",
          quote: 'deploy LiteLLM Proxy on ECS Fargate for governed, cost-attributed',
        },
      },
      {
        projectId: 'self-service',
        meaning:
          'There is no separate LLM gateway layer. Generated agents call one of 13 model providers directly, with Bedrock as the default.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Key Features',
          quote: '13 model providers & multi-agent patterns',
        },
      },
      {
        projectId: 'blueprint',
        meaning:
          'AgentCore Gateway inference targets behind a mandatory Guardrail interceptor. LiteLLMModel is the agent-side client adapter, not the gateway.',
        source: {
          file: BLUEPRINT_README,
          heading: 'Inference flow',
          quote: '`LiteLLMModel` is a client adapter; it is not itself the deployed LLM Gateway.',
        },
      },
    ],
    seeAlso: ['litellm', 'agentcore-gateway'],
  },
  {
    id: 'registry',
    term: 'Registry',
    definition:
      'A governed catalog that records ownership, lifecycle state and approval for agents and tools. The root README names AWS Agent Registry as the reference implementation.',
    source: {
      file: ROOT_README,
      heading: 'Capability Architecture',
      quote: 'Ownership, lifecycle, and approval for agents/tools',
    },
    perProjectMeaning: [
      {
        projectId: 'workshop',
        meaning:
          'Two registries: the open-source MCP Registry in Module 3a, and the AgentCore Registry in Module 3b with a Publisher and Admin approval workflow.',
        source: {
          file: WORKSHOP_MODULE_3B,
          quote: 'Created an AgentCore Registry and registered 3 MCP tools with metadata',
        },
      },
      {
        projectId: 'self-service',
        meaning:
          'A built-in DynamoDB agent registry with an approval workflow, versioning and rollback. A LiteLLM proxy can optionally become the authoritative catalog.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Key Features',
          quote: 'agent registry with approval workflow, versioning & rollback',
        },
      },
      {
        projectId: 'mcp-gateway',
        meaning:
          'None. The gateway governs individual tool calls in the request path and positions itself a layer below platforms that manage which agents and servers exist.',
        source: {
          file: GATEWAY_README,
          heading: 'Related projects',
          quote: 'This sample is complementary and sits a layer lower',
        },
      },
      {
        projectId: 'blueprint',
        meaning: 'AWS Agent Registry with approved governance records that drive Gateway targets.',
        source: {
          file: BLUEPRINT_README,
          heading: 'Capability contracts and replaceable implementations',
          quote: 'AWS Agent Registry and approved governance records',
        },
      },
    ],
    seeAlso: ['agent-registry-naming'],
  },
  {
    id: 'agent-registry-naming',
    term: 'Agent Registry versus AgentCore Registry',
    definition:
      'The same AWS service under two names. The workshop calls it AgentCore Registry; the root README and the Blueprint call it AWS Agent Registry. The service-linked role and the GA service namespaces use agent-registry. Issue #29 tracks the Blueprint move from the preview bedrock-agentcore-control APIs, whose support ended on 2026-09-17, to the GA APIs.',
    source: {
      file: WORKSHOP_CONTENTSPEC,
      quote: "AWSServiceRoleForAgentRegistry itself on the account's first Agent Registry",
    },
    seeAlso: ['registry'],
  },
  {
    id: 'litellm',
    term: 'LiteLLM',
    definition:
      'An open-source proxy and SDK that gives one OpenAI-compatible interface to many model providers. The root README lists it as a reference choice that may be substituted if the contracts are preserved.',
    source: {
      file: ROOT_README,
      heading: 'Capability Architecture',
      quote: 'LiteLLM, AgentCore Gateway targets, and other components are reference choices.',
    },
    perProjectMeaning: [
      {
        projectId: 'workshop',
        meaning: 'The LLM Gateway of Module 2: LiteLLM Proxy on ECS Fargate with virtual keys and budgets.',
        source: {
          file: WORKSHOP_README,
          quote: 'proven open-source components (LiteLLM, MCP Gateway & Registry, Strands Agents, and FAST)',
        },
      },
      {
        projectId: 'self-service',
        meaning:
          'Bring your own LiteLLM proxy in two optional roles: as the MCP gateway for individual agents, and as the agent catalog behind the Registry. AgentCore Gateway and the built-in registry stay the defaults.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Bring your own LiteLLM',
          quote: 'it can serve two roles here: as an **MCP',
        },
      },
      {
        projectId: 'blueprint',
        meaning:
          'LiteLLMModel is the agent client for AgentCore Gateway inference targets. A self-managed LiteLLM gateway is a valid alternative pattern, and the older ECS LiteLLM path is outside the current envelope.',
        source: {
          file: BLUEPRINT_README,
          heading: 'Outside the current envelope',
          quote: 'Legacy direct-Bedrock evaluation, online-evaluation, ECS LiteLLM',
        },
      },
    ],
    seeAlso: ['llm-gateway', 'mcp-gateway'],
  },
  {
    id: 'cedar',
    term: 'Cedar',
    definition:
      'An open-source policy language for permit and forbid rules over principals, actions and resources. Three of the four projects express tool authorization in Cedar.',
    source: {
      file: ROOT_README,
      quote: '[Cedar](https://www.cedarpolicy.com/) policies and Bedrock Guardrails',
    },
    perProjectMeaning: [
      {
        projectId: 'workshop',
        meaning:
          'Module 3b Part C (optional) creates a Policy Engine and Cedar policies and attaches it in LOG_ONLY mode, because ENFORCE would empty tools/list in that setup.',
        source: {
          file: WORKSHOP_MODULE_3B_STEP_7,
          quote: 'Attach with `"mode": "LOG_ONLY"`, not `"ENFORCE"`.',
        },
      },
      {
        projectId: 'self-service',
        meaning:
          'A Policy node in ENFORCE mode builds a permit over the allowed tools against the gateway manifest; forbidden tools are denied by omission and hidden from tools/list.',
        source: {
          file: SELF_SERVICE_CAPABILITIES,
          heading: 'Agent lifecycle & quality',
          quote: 'When a Policy node runs in `ENFORCE` mode, the policy step builds a schema-correct Cedar policy set',
        },
      },
      {
        projectId: 'mcp-gateway',
        meaning:
          'One CfnPolicy per Cedar statement on an AgentCore policy engine in ENFORCE. The principal type is AgentCore::OAuthUser and JWT claims become principal tags.',
        source: {
          file: GATEWAY_README,
          heading: 'Cedar specifics',
          quote: 'Principal type is `AgentCore::OAuthUser`; JWT claims become **tags**',
        },
      },
      {
        projectId: 'blueprint',
        meaning: 'AgentCore PolicyEngine plus a retained Lambda Cedar wrapper that stays as a rollback control.',
        source: {
          file: BLUEPRINT_README,
          heading: 'Capability contracts and replaceable implementations',
          quote: 'AgentCore PolicyEngine, IAM/SCP controls, and the retained Lambda Cedar wrapper',
        },
      },
    ],
    seeAlso: ['policy-engine'],
  },
  {
    id: 'policy-engine',
    term: 'Policy Engine (AgentCore Policy Engine)',
    definition:
      'The AgentCore component that evaluates Cedar policies for each gateway tool call. Attached to a gateway in ENFORCE mode it denies by default; in LOG_ONLY mode it records decisions without blocking.',
    source: {
      file: ROOT_README,
      heading: 'Capability Architecture',
      quote: 'Fail-closed authorization with versioning',
    },
    seeAlso: ['cedar'],
  },
  {
    id: 'guardrails',
    term: 'Amazon Bedrock Guardrails',
    definition:
      'A managed Bedrock capability that filters prompts and responses for prompt attacks, harmful content and PII. Used here on tool traffic, not only on model calls.',
    perProjectMeaning: [
      {
        projectId: 'workshop',
        meaning: 'Module 3b step 7 wires a Bedrock guardrail to the response interceptor to screen tool output.',
        source: {
          file: WORKSHOP_MODULE_3B_STEP_7,
          heading: 'Part A: Bedrock Guardrails on tool output',
          quote: '## Part A: Bedrock Guardrails on tool output',
        },
      },
      {
        projectId: 'mcp-gateway',
        meaning:
          'A managed guardrail called through ApplyGuardrail from both interceptors: prompt-attack and content filters on the request, PII anonymization on the response, with regex rules as a backstop.',
        source: {
          file: GATEWAY_README,
          heading: 'Managed guardrail (Amazon Bedrock Guardrails)',
          quote: 'The interceptors enforce a **managed** Amazon Bedrock Guardrail in addition to the',
        },
      },
      {
        projectId: 'blueprint',
        meaning: 'A mandatory Bedrock Guardrail request interceptor on the Inference Gateway, protected by SCPs.',
        source: {
          file: BLUEPRINT_README,
          heading: '10.1 Control summary',
          quote: 'Mandatory Bedrock Guardrail request interceptor on the Inference Gateway.',
        },
      },
    ],
    seeAlso: ['interceptor'],
  },
  {
    id: 'interceptor',
    term: 'Interceptor (request and response)',
    definition:
      'A Lambda function that AgentCore Gateway invokes before a tool call (REQUEST) or after it (RESPONSE) to inspect, block or transform the payload.',
    source: {
      file: GATEWAY_README,
      heading: 'Verified architecture',
      quote: '`interceptorConfigurations` (a list of REQUEST + RESPONSE',
    },
    perProjectMeaning: [
      {
        projectId: 'workshop',
        meaning: 'Request and response interceptors on the Tools Gateway enforce audit trails and content safety.',
        source: {
          file: WORKSHOP_MODULE_3B,
          quote: 'How request and response interceptors enforce audit trails and content safety',
        },
      },
      {
        projectId: 'mcp-gateway',
        meaning:
          'SQL-injection blocking, business-hours gating, PII redaction, payload truncation and structured audit logging, backed by a managed Bedrock Guardrail.',
        source: {
          file: GATEWAY_README,
          quote: '**Inspects and transforms** requests and responses with **Lambda interceptors**',
        },
      },
    ],
    seeAlso: ['guardrails'],
  },
  {
    id: 'agentcore-runtime',
    term: 'AgentCore Runtime',
    definition:
      'The managed execution service for agents in Amazon Bedrock AgentCore. The Blueprint contract requires an immutable deployable revision, workload identity, isolation, health, scaling, logs, safe update and rollback.',
    source: {
      file: BLUEPRINT_README,
      heading: 'Capability contracts and replaceable implementations',
      quote: 'Immutable deployable revision, workload identity, isolation, health, scaling, logs, safe update, rollback',
    },
  },
  {
    id: 'agentcore-memory',
    term: 'AgentCore Memory',
    definition:
      'The managed memory service in AgentCore. The Blueprint uses actor-scoped events with customer-managed KMS keys; the Self-Service canvas exposes it as a Memory node.',
    source: {
      file: BLUEPRINT_README,
      heading: 'Capability contracts and replaceable implementations',
      quote: 'AgentCore Memory with actor-scoped events and customer-managed KMS keys',
    },
  },
  {
    id: 'agentcore-identity',
    term: 'AgentCore Identity',
    definition:
      'Workload identity and token brokerage for agents. Paired with Cognito M2M in the Blueprint to obtain short-lived, audience-bound credentials.',
    source: {
      file: BLUEPRINT_README,
      heading: 'Capability contracts and replaceable implementations',
      quote: 'Short-lived credentials, audience and issuer validation, tenant binding, rotation, revocation',
    },
  },
  {
    id: 'agentcore-gateway',
    term: 'AgentCore Gateway',
    definition:
      'The AgentCore service that exposes tools (Lambda, OpenAPI, Smithy, MCP servers) and, in the Blueprint, inference targets behind one authenticated endpoint. It appears as both the LLM Gateway and the Tool Gateway reference implementation.',
    source: {
      file: ROOT_README,
      heading: 'Capability Architecture',
      quote: 'AgentCore Gateway inference targets, LiteLLM',
    },
    seeAlso: ['tool-gateway', 'llm-gateway'],
  },
  {
    id: 'strands-agents',
    term: 'Strands Agents',
    definition: 'An open-source agent SDK used to write the agents themselves across the projects.',
    perProjectMeaning: [
      {
        projectId: 'workshop',
        meaning: 'One of the open-source components the workshop composes with AgentCore.',
        source: {
          file: WORKSHOP_README,
          heading: 'Why this workshop',
          quote: '**Strands Agents** (the agents themselves)',
        },
      },
      {
        projectId: 'self-service',
        meaning: 'Generated agent code uses the Strands Agents SDK, including Graph, Swarm and Workflow multi-agent orchestration.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Key Features',
          quote: 'Graph / Swarm / Workflow orchestration via Strands Agents SDK',
        },
      },
      {
        projectId: 'blueprint',
        meaning: 'The framework of the task, chatbot and multi-agent golden paths; LangGraph and CrewAI templates use adapters.',
        source: {
          file: BLUEPRINT_README,
          heading: '1.2 How an engineer ships an agent',
          quote: 'task, chatbot, supervisor/worker, LangGraph, or CrewAI template',
        },
      },
    ],
  },
  {
    id: 'fast',
    term: 'FAST (Fullstack AgentCore Solution Template)',
    definition:
      'An open-source starter template for full-stack agents on Amazon Bedrock AgentCore. Module 4 of the workshop deploys a travel agent with it.',
    source: {
      file: WORKSHOP_README,
      heading: "What you'll build",
      quote: 'AgentCore Solution Template) on Amazon Bedrock AgentCore, wired to the platform',
    },
  },
  {
    id: 'workshop-studio',
    term: 'Workshop Studio',
    definition:
      'The AWS platform that builds and publishes workshops and, at AWS events, provisions a pre-configured account per participant with the scoped WSParticipantRole.',
    source: {
      file: WORKSHOP_README,
      heading: 'Running the workshop',
      quote: 'Workshop Studio auto-provisions a pre-configured account; participants use',
    },
  },
  {
    id: 'self-service-vs-self-paced',
    term: 'Self-Service versus self-paced',
    definition:
      'Two different things. Self-Service is the project name for the AgentCore Visual Workflow Platform. Self-paced is the way of running the workshop in your own account instead of at an AWS event. The workshop\'s deploy script is nevertheless named self-service-deploy.sh and its setup page is titled Self-Paced Setup.',
    perProjectMeaning: [
      {
        projectId: 'workshop',
        meaning: 'Self-paced: run the workshop in your own AWS account with one deploy script.',
        source: {
          file: WORKSHOP_README,
          heading: 'Prerequisites (self-paced)',
          quote: '### Prerequisites (self-paced)',
        },
      },
      {
        projectId: 'self-service',
        meaning: 'The Build-stage project: a visual drag-and-drop canvas for designing and deploying agents.',
        source: {
          file: ROOT_README,
          quote: 'Visual drag-and-drop canvas for designing and deploying agents',
        },
      },
    ],
  },
  {
    id: 'privatelink',
    term: 'PrivateLink and VPC interface endpoints',
    definition:
      'AWS PrivateLink provides private connectivity through VPC interface endpoints. The two projects that mention it use it in opposite directions.',
    perProjectMeaning: [
      {
        projectId: 'self-service',
        meaning:
          'An optional ingress add-on (NLB, VPC endpoint service and security group) shipped as a downloadable CloudFormation template so callers inside a VPC can reach a deployed agent privately.',
        source: {
          file: SELF_SERVICE_CAPABILITIES,
          heading: 'Enterprise governance & operations (Loom-inspired)',
          quote: 'Optional PrivateLink ingress IaC (NLB + VPCEndpointService + SG) ships as a downloadable add-on',
        },
      },
      {
        projectId: 'blueprint',
        meaning:
          'Outbound: each workload VPC has private-isolated subnets only (no internet gateway, no NAT) and reaches AWS services through required VPC interface endpoints, enforced by SCP-03 and SCP-04. The live-validated Workstream Runtime stack (apps/workload-account/lib/d03-workstream-runtime-memory-stack.ts) sets networkMode PUBLIC; VPC network mode for the Runtime is documented as a follow-on.',
        source: {
          file: BLUEPRINT_VPC_INDEX,
          quote: '9 required VPC endpoints per §2.3.4',
        },
      },
    ],
  },
  {
    id: 'fast-path',
    term: 'Fast Path (workshop track only)',
    definition:
      'Track 1 of the workshop: AI/ML engineers jump straight to Module 4 on a pre-deployed platform, about 1.5 to 2 hours. The term belongs to the workshop; the site-level paths use different names.',
    source: {
      file: WORKSHOP_README,
      heading: 'Choose your track',
      quote: 'AI/ML engineers who want to build an agent',
    },
  },
  {
    id: 'workstream-cell',
    term: 'Workstream cell',
    definition:
      'The Blueprint unit of scale: an isolated, team-owned execution boundary with its own Runtime, Memory, Tool Gateway, tools, data and pipeline, created from a shared baseline.',
    source: {
      file: BLUEPRINT_README,
      heading: '1. Overview',
      quote: 'The unit of scale is a **workstream cell**, not a manually configured agent.',
    },
  },
  {
    id: 'capability-contract',
    term: 'Capability contract',
    definition:
      'The outcomes and controls that any chosen implementation of a capability must preserve, regardless of product. Alternatives are valid when the contract holds.',
    source: {
      file: ROOT_README,
      heading: 'Capability Architecture',
      quote: 'defines the outcomes and controls that any chosen implementation must preserve',
    },
    seeAlso: ['support-envelope'],
  },
  {
    id: 'support-envelope',
    term: 'Support envelope',
    definition:
      'The exact set of tested implementations, regions and configurations a project stands behind. Replacements and other regions need independent validation.',
    source: {
      file: ROOT_README,
      heading: 'Support Envelope',
      quote: 'The support envelope applies to the exact reference implementations tested',
    },
    seeAlso: ['capability-contract'],
  },
  {
    id: 'scp',
    term: 'SCP (service control policy)',
    definition:
      'An AWS Organizations policy that sets the maximum permissions for accounts in an organizational unit. The Blueprint ships 12; the workshop offers one optional region-fence SCP for self-hosted hardening.',
    perProjectMeaning: [
      {
        projectId: 'blueprint',
        meaning: 'Organizations SCPs for model, Region, Guardrail, Registry, Gateway and deployment boundaries.',
        source: {
          file: BLUEPRINT_README,
          heading: '10.1 Control summary',
          quote: 'Organizations SCPs for model, Region, Guardrail, Registry, Gateway, and deployment boundaries.',
        },
      },
      {
        projectId: 'workshop',
        meaning: 'An optional OU-level region-fence SCP in static/cfn/self-service-scp.json for self-hosted deployments.',
        source: {
          file: WORKSHOP_README,
          heading: 'Repository structure',
          quote: 'Optional OU-level region-fence SCP (self-hosted hardening)',
        },
      },
    ],
  },
  {
    id: 'application-inference-profile',
    term: 'Application Inference Profile',
    definition:
      'A Bedrock resource that tags model invocations so usage and cost can be attributed per application or tenant. The Blueprint creates them per tenant.',
    source: {
      file: BLUEPRINT_README,
      heading: '4. AWS services used',
      quote: 'Amazon Bedrock, Bedrock Guardrails, Bedrock application inference profiles',
    },
  },
  {
    id: 'golden-path',
    term: 'Golden path',
    definition:
      'A versioned, supported starting template for a class of agent (task, chatbot, multi-agent, LangGraph, CrewAI) that delivery teams instantiate instead of assembling infrastructure from scratch.',
    source: {
      file: BLUEPRINT_README,
      heading: '7. Golden paths for engineering teams',
      quote: 'starting points for **versioned enterprise golden paths**',
    },
  },
  {
    id: 'oam',
    term: 'OAM (CloudWatch Observability Access Manager)',
    definition:
      'CloudWatch cross-account observability. Each Platform and Workstream account creates one OAM source link to the Management sink so logs, metrics and traces can be queried centrally.',
    source: {
      file: BLUEPRINT_README,
      heading: '9.2 Observability',
      quote: 'create one OAM source link to the Management sink',
    },
  },
  {
    id: 'cognito-m2m-custom-jwt',
    term: 'Cognito M2M and CUSTOM_JWT',
    definition:
      'Machine-to-machine OAuth2 client credentials issued by Amazon Cognito, validated by AgentCore Gateway through its CUSTOM_JWT authorizer. The Workshop Tools Gateway, the Self-Service MCP Gateway and the MCP Gateway use CUSTOM_JWT for callers; the Blueprint uses it only between Runtime and the Inference Gateway and secures its Tool Gateway with AWS_IAM.',
    source: {
      file: BLUEPRINT_README,
      heading: '2.6 Trust, resilience, and blast-radius boundaries',
      quote: 'AgentCore Identity, Cognito M2M, CUSTOM_JWT',
    },
  },
  {
    id: 'advisory-rbac',
    term: 'Advisory mode (RBAC)',
    definition:
      'In the Self-Service platform, scope-based RBAC ships with RBAC_ENFORCE=false: every request is allowed and a would-be denial is logged and counted as a CloudWatch WouldDeny metric until you switch to enforce.',
    source: {
      file: SELF_SERVICE_RBAC_ROLLOUT,
      heading: 'RBAC Enforcement Rollout Runbook',
      quote: 'Scope-based RBAC (`services/rbac.py`) ships **advisory by default**',
    },
  },
];
