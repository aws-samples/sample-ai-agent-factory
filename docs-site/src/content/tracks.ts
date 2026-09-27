/**
 * Site-level paths ("which project should I start with") and the workshop's
 * own three tracks.
 *
 * The four site paths have their own names. The workshop track names (Fast
 * Path, Build the Platform, Full Journey) belong to the workshop only and are
 * exported separately as `workshopTracks` for the workshop page.
 */
import type { ProjectId } from './data';
import { facts, type Fact, type Source } from './facts';

/** Identifiers for the four site-level paths. */
export type SitePathId = 'build-visually' | 'learn-platform' | 'govern-tools' | 'evaluate-blueprint';

/** A site-level path: who it is for, what you get, and how long to a first result. */
export interface SitePath {
  /** Stable id. */
  id: SitePathId;
  /** Display name. */
  name: string;
  /** Who should pick this path. */
  who: string;
  /** What the reader has at the end. */
  whatYouGet: string;
  /** README evidence for `whatYouGet`. */
  source: Source;
  /** Time to a first result, sourced. */
  timeToFirstResult: Fact;
  /** Projects this path uses, in order. */
  projects: ProjectId[];
  /** Site route to start on. */
  startRoute: string;
}

/** One of the workshop's own tracks. */
export interface WorkshopTrack {
  /** Track number as printed in the workshop README. */
  number: 1 | 2 | 3;
  /** Track name as printed in the workshop README. */
  name: string;
  /** Who the track is best for. */
  bestFor: string;
  /** What you do on the track. */
  youDo: string;
  /** Duration as documented by the workshop. */
  duration: string;
  /** Modules in order. */
  modules: string[];
  /** Where the row comes from. */
  source: Source;
}

/** Role-based recommendation. */
export interface RoleGuidance {
  /** Reader role. */
  role: string;
  /** Recommended path. */
  pathId: SitePathId;
  /** One sentence on why. */
  why: string;
}

/** Time-based recommendation. */
export interface TimeGuidance {
  /** How much time the reader has. */
  available: string;
  /** Recommended path. */
  pathId: SitePathId;
  /** The fact the recommendation rests on. */
  basis: Fact;
}

const WORKSHOP_README = 'workshop-building-agentic-ai-platform/README.md';
const SELF_SERVICE_README = 'Agentic-ai-self-service/README.md';
const GATEWAY_README = 'enterprise-mcp-governance-gateway/README.md';
const BLUEPRINT_README = 'enterprise-agentic-ai-platform-blueprint/README.md';

export const sitePaths: SitePath[] = [
  {
    id: 'build-visually',
    name: 'Build an agent visually',
    who: 'Engineers who want a working agent on Amazon Bedrock AgentCore without writing infrastructure.',
    whatYouGet:
      'A deployed visual workflow platform with six templates, real SaaS connectors, 13 model providers, and export to CloudFormation or a standalone Python project.',
    source: {
      file: SELF_SERVICE_README,
      heading: 'Key Features',
      quote: 'Selected tools deploy as a single Lambda behind an MCP Gateway with Cognito OAuth2',
    },
    timeToFirstResult: facts['self-service'].firstDeploy,
    projects: ['self-service'],
    startRoute: '/projects/self-service/',
  },
  {
    id: 'learn-platform',
    name: 'Learn and stand up the platform',
    who: 'Platform engineers, AI/ML engineers, and solutions architects who want to understand the foundation by building it.',
    whatYouGet:
      'Five CloudFormation stacks (LLM Gateway, MCP Registry, Tools Gateway, AgentCore, Code Editor IDE) and guided modules; two of the three tracks end with a deployed agent.',
    source: {
      file: WORKSHOP_README,
      heading: 'Quick start (self-paced)',
      quote: '# Deploy all 5 stacks (LLM Gateway, MCP Registry, Tools Gateway, AgentCore, Code Editor IDE)',
    },
    timeToFirstResult: {
      value: 'About 30 to 45 minutes to deploy self-paced, then 1.5 to 4 hours hands-on depending on the track',
      source: {
        file: WORKSHOP_README,
        heading: 'Quick start (self-paced)',
        quote: '~30-45 min; prints the IDE URL + password at the end',
      },
      note: 'Hands-on time is the estimatedDuration in contentspec.yaml (1.5-4 hours). At an AWS event there is no deploy step.',
    },
    projects: ['workshop'],
    startRoute: '/projects/workshop/',
  },
  {
    id: 'govern-tools',
    name: 'Govern every tool call',
    who: 'Platform and security engineers who need per-tool-call authorization, screening, and audit.',
    whatYouGet:
      'A single governed MCP endpoint with Cognito JWT authentication, Cedar policies in ENFORCE mode, request and response interceptors, a Bedrock Guardrail, and five live governance tests.',
    source: {
      file: GATEWAY_README,
      heading: 'Quickstart',
      quote: 'Deploy, then prove the governance works. Five steps, ~5 minutes.',
    },
    timeToFirstResult: facts['mcp-gateway'].firstDeploy,
    projects: ['mcp-gateway'],
    startRoute: '/projects/mcp-gateway/',
  },
  {
    id: 'evaluate-blueprint',
    name: 'Evaluate the enterprise blueprint',
    who: 'Platform teams and architects planning a multi-account rollout for many delivery teams.',
    whatYouGet:
      'A multi-account CDK reference with AWS Organizations, 12 SCPs, CDK Pipelines with an evaluation gate, OAM observability, and a documented support envelope.',
    source: {
      file: BLUEPRINT_README,
      heading: '15. Known limitations and support envelope',
      quote: 'is validated in `eu-west-1` (Ireland):',
    },
    timeToFirstResult: facts.blueprint.firstDeploy,
    projects: ['blueprint'],
    startRoute: '/projects/blueprint/',
  },
];

/** The workshop's own three tracks, from its README. For the workshop page only. */
export const workshopTracks: WorkshopTrack[] = [
  {
    number: 1,
    name: 'Fast Path',
    bestFor: 'AI/ML engineers who want to build an agent',
    youDo: 'Jump straight to Module 4; the platform is pre-deployed',
    duration: 'About 1.5 to 2 hours',
    modules: ['Module 1', 'Module 4'],
    source: {
      file: WORKSHOP_README,
      heading: 'Choose your track',
      quote: 'AI/ML engineers who want to build an agent',
    },
  },
  {
    number: 2,
    name: 'Build the Platform',
    bestFor: 'Platform engineers',
    youDo: 'Modules 1, 2, 3a, 3b (stops before the agent)',
    duration: 'About 2 to 3 hours',
    modules: ['Module 1', 'Module 2', 'Module 3a', 'Module 3b'],
    source: {
      file: WORKSHOP_README,
      heading: 'Choose your track',
      quote: '(stops before the agent)',
    },
  },
  {
    number: 3,
    name: 'Full Journey',
    bestFor: 'Solutions architects, tech leads',
    youDo: 'Modules 1, 2, 3a, 3b, 4 end-to-end',
    duration: 'About 3 to 4 hours',
    modules: ['Module 1', 'Module 2', 'Module 3a', 'Module 3b', 'Module 4'],
    source: {
      file: WORKSHOP_README,
      heading: 'Choose your track',
      quote: 'Solutions architects, tech leads',
    },
  },
];

/** "By role" guidance for the comparison page. */
export const roleGuidance: RoleGuidance[] = [
  {
    role: 'AI/ML engineer who wants a running agent today',
    pathId: 'build-visually',
    why: 'The Self-Service platform deploys in one command and ships six templates to start from.',
  },
  {
    role: 'Platform engineer learning the foundation',
    pathId: 'learn-platform',
    why: 'The workshop builds the LLM Gateway, registries, and Tools Gateway module by module.',
  },
  {
    role: 'Security engineer implementing tool governance',
    pathId: 'govern-tools',
    why: 'The MCP Gateway shows Cedar ENFORCE, interceptors, and a Bedrock Guardrail on a live endpoint with tests.',
  },
  {
    role: 'Solutions architect comparing the pieces',
    pathId: 'learn-platform',
    why: "The workshop's longest track covers every module end to end, including the agent.",
  },
  {
    role: 'Platform team planning a multi-account rollout',
    pathId: 'evaluate-blueprint',
    why: 'The Blueprint documents account roles, SCPs, pipelines, evidence gates, and a bounded support envelope.',
  },
];

/** "By time available" guidance for the comparison page. Each row rests on a sourced fact. */
export const timeGuidance: TimeGuidance[] = [
  {
    available: 'A few minutes',
    pathId: 'govern-tools',
    basis: facts['mcp-gateway'].firstDeploy,
  },
  {
    available: 'Under an hour',
    pathId: 'build-visually',
    basis: facts['self-service'].firstDeploy,
  },
  {
    available: 'Half a day',
    pathId: 'learn-platform',
    basis: facts.workshop.handsOnTime,
  },
  {
    available: 'A planning cycle with several accounts',
    pathId: 'evaluate-blueprint',
    basis: facts.blueprint.firstDeploy,
  },
];

/** Look up a site path by id. */
export function getSitePath(id: SitePathId): SitePath | undefined {
  return sitePaths.find(p => p.id === id);
}
