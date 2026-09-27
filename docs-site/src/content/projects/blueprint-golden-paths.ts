/**
 * Sourced content for the Blueprint project page: the five golden-path
 * templates under blueprints/, a grouping of the packages/ folders (names only,
 * no claims), and the two README figures with their .drawio sources.
 *
 * Pure data. Every `quote` is a verbatim substring of the cited file, every
 * `heading` is a real heading, and every folder named here exists in the
 * repository (checked by projects.test.ts).
 */
import type { Source } from '../facts';

export const BLUEPRINT = 'enterprise-agentic-ai-platform-blueprint';
export const BLUEPRINT_README_PATH = `${BLUEPRINT}/README.md`;
export const BLUEPRINT_PACKAGES_DIR = `${BLUEPRINT}/packages`;
export const BLUEPRINT_TEMPLATES_DIR = `${BLUEPRINT}/blueprints`;

/** README section the golden-path table is copied from. */
export const GOLDEN_PATHS_SOURCE: Source = { file: BLUEPRINT_README_PATH, heading: '7. Golden paths for engineering teams' };

/** One template under blueprints/. */
export interface GoldenPath {
  /** Folder name under blueprints/. */
  name: string;
  framework: string;
  bestFit: string;
  contract: string;
  source: Source;
}

export const goldenPaths: GoldenPath[] = [
  {
    name: 'agenticai-task-agent',
    framework: 'Strands',
    bestFit: 'Deterministic business task',
    contract: 'Max-iteration guard, baseline Guardrail, optional durable HITL',
    source: { ...GOLDEN_PATHS_SOURCE, quote: 'Max-iteration guard, baseline Guardrail, optional durable HITL' },
  },
  {
    name: 'agenticai-chatbot-agent',
    framework: 'Strands',
    bestFit: 'Customer or employee conversation',
    contract: 'Streaming, conversation memory, human handoff',
    source: { ...GOLDEN_PATHS_SOURCE, quote: 'Streaming, conversation memory, human handoff' },
  },
  {
    name: 'agenticai-multi-agent',
    framework: 'Strands',
    bestFit: 'Supervisor and bounded workers',
    contract: 'Separate identities, explicit delegation, bounded fan-out',
    source: { ...GOLDEN_PATHS_SOURCE, quote: 'Separate identities, explicit delegation, bounded fan-out' },
  },
  {
    name: 'agenticai-langgraph-agent',
    framework: 'LangGraph',
    bestFit: 'State-machine or graph orchestration',
    contract: 'Same Gateway and governance boundaries through an adapter',
    source: { ...GOLDEN_PATHS_SOURCE, quote: 'Same Gateway and governance boundaries through an adapter' },
  },
  {
    name: 'agenticai-crewai-agent',
    framework: 'CrewAI',
    bestFit: 'Role-oriented crew orchestration',
    contract: 'Same approved-tool and Guardrail contracts through an adapter',
    source: { ...GOLDEN_PATHS_SOURCE, quote: 'Same approved-tool and Guardrail contracts through an adapter' },
  },
];

/** How the README frames the templates. */
export const goldenPathsIntro = {
  text: 'The blueprints under blueprints/ are starting points for versioned enterprise golden paths, not disconnected demos.',
  source: {
    ...GOLDEN_PATHS_SOURCE,
    quote: 'starting points for **versioned enterprise golden paths**, not disconnected demos.',
  } satisfies Source,
};

/** A named group of packages/ folders. Names only; the grouping is editorial. */
export interface PackageGroup {
  id: string;
  name: string;
  packages: string[];
}

export const packageGroups: PackageGroup[] = [
  {
    id: 'organization',
    name: 'Organization, accounts and access',
    packages: ['landing-zone', 'organizations', 'platform-baselines', 'developer-access', 'federation', 'cost-allocation'],
  },
  {
    id: 'agentcore',
    name: 'AgentCore and application constructs',
    packages: [
      'agentcore-gateway',
      'agentcore-identity',
      'agentcore-memory',
      'agentcore-registry',
      'agentcore-runtime',
      'agent-registry',
      'agentic-app',
      'agentic-vpc',
    ],
  },
  {
    id: 'models',
    name: 'Model access, quotas and safety',
    packages: [
      'platform-inference-gateway',
      'litellm-gateway',
      'bedrock-guardrails',
      'bedrock-invocation-logging',
      'bedrock-quotas',
      'tenant-quota-guard',
      'pii-redaction',
    ],
  },
  {
    id: 'tools',
    name: 'Tools, catalogue and policy',
    packages: ['platform-tool-catalogue', 'tool-cedar-wrapper', 'catalogue-drift-detector', 'agent-protocols', 'rag'],
  },
  {
    id: 'lifecycle',
    name: 'Delivery, evaluation and lifecycle',
    packages: ['agent-lifecycle', 'agent-resilience', 'evaluation-gates', 'online-evaluation', 'hitl', 'developer-cli'],
  },
  {
    id: 'observability',
    name: 'Observability and compliance',
    packages: ['observability', 'otel-genai-semconv', 'eu-ai-act-compliance'],
  },
];

/** The two README figures, with their editable sources. */
export const blueprintFigures = {
  concept: {
    alt: 'Enterprise Agent Factory operating model and governed flow',
    caption:
      'Figure 1 from the Blueprint README: enterprise operating model and governed flow. AWS labels illustrate the reference choices; the capability boundaries are the architecture.',
    drawio: `${BLUEPRINT}/assets/enterprise-agent-factory-concept.drawio`,
    sources: [
      { file: BLUEPRINT_README_PATH, quote: 'Enterprise Agent Factory operating model and governed flow' },
      {
        file: BLUEPRINT_README_PATH,
        quote: "AWS labels illustrate this repository's reference choices; the capability boundaries are the architecture.",
      },
    ] satisfies Source[],
  },
  services: {
    alt: 'Enterprise Agent Factory AWS service-level reference architecture',
    caption:
      'Figure 2 from the Blueprint README: AWS service-level reference implementation. Account IDs are documentation placeholders.',
    drawio: `${BLUEPRINT}/assets/enterprise-agent-factory-aws-services.drawio`,
    sources: [
      { file: BLUEPRINT_README_PATH, quote: 'Enterprise Agent Factory AWS service-level reference architecture' },
      { file: BLUEPRINT_README_PATH, quote: 'Account IDs are documentation placeholders.' },
    ] satisfies Source[],
  },
};
