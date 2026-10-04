/**
 * Sourced content for the workshop project page: the five modules, the note
 * about where notebooks run, and the alt text of the two figures. The three
 * workshop tracks live in ../tracks.ts (`workshopTracks`).
 *
 * Pure data. Every `quote` is a verbatim substring of the cited repository file
 * (checked by projects.test.ts).
 */
import type { Source } from '../facts';

const WORKSHOP = 'workshop-building-agentic-ai-platform';
export const WORKSHOP_README_PATH = `${WORKSHOP}/README.md`;
const MODULE_4_INDEX = `${WORKSHOP}/content/module-4/index.en.md`;

/** README section that lists the five modules. */
export const WHAT_YOULL_BUILD: Source = { file: WORKSHOP_README_PATH, heading: "What you'll build" };

/** README section that holds the track table. */
export const CHOOSE_YOUR_TRACK: Source = { file: WORKSHOP_README_PATH, heading: 'Choose your track' };

/** One workshop module. */
export interface WorkshopModule {
  id: string;
  /** "Module 1", "Module 3a", ... */
  name: string;
  /** Module title as printed in the README. */
  title: string;
  /** One line, copied from the README bullet. */
  summary: string;
  source: Source;
}

export const workshopModules: WorkshopModule[] = [
  {
    id: 'module-1',
    name: 'Module 1',
    title: 'The Vision',
    summary: 'Why enterprises need a platform approach to agentic AI, not just individual agents (all tracks).',
    source: { ...WHAT_YOULL_BUILD, quote: 'why enterprises need a platform approach to agentic AI, not just' },
  },
  {
    id: 'module-2',
    name: 'Module 2',
    title: 'LLM Gateway',
    summary: 'Deploy LiteLLM Proxy on ECS Fargate for governed, cost-attributed access to Amazon Bedrock models.',
    source: { ...WHAT_YOULL_BUILD, quote: 'deploy LiteLLM Proxy on ECS Fargate for governed, cost-attributed' },
  },
  {
    id: 'module-3a',
    name: 'Module 3a',
    title: 'MCP Registry + Tools Gateway',
    summary:
      'Register tools in the MCP Gateway & Registry, then layer an AgentCore Tools Gateway on top for JWT auth, audit, and guardrails.',
    source: { ...WHAT_YOULL_BUILD, quote: 'register tools in the MCP Gateway & Registry, then' },
  },
  {
    id: 'module-3b',
    name: 'Module 3b',
    title: 'AgentCore Registry & Gateway',
    summary:
      'AWS-native tool governance with Amazon Bedrock AgentCore, Cedar-based authorization, and EventBridge-driven approval workflows.',
    source: { ...WHAT_YOULL_BUILD, quote: 'AWS-native tool governance with Amazon Bedrock' },
  },
  {
    id: 'module-4',
    name: 'Module 4',
    title: 'Build Your Agent',
    summary:
      'Deploy a full-stack travel agent using FAST (Fullstack AgentCore Solution Template) on Amazon Bedrock AgentCore, wired to the platform via either the MCP path or the AgentCore path.',
    source: { ...WHAT_YOULL_BUILD, quote: 'deploy a full-stack travel agent using FAST (Fullstack' },
  },
];

/** Where the notebooks run. */
export const notebooksRunInIde = {
  text: 'The notebooks run inside the browser Code Editor IDE that the workshop provisions, not on your laptop. Open them from /workshop/source/<module>/notebooks/ in the IDE and select the workshop kernel (workshop-fast for Module 4b).',
  source: {
    file: WORKSHOP_README_PATH,
    quote: 'browser Code Editor IDE** (the URL the deploy printed), **not** on your laptop',
  } satisfies Source,
};

/** Alt text and provenance of the two workshop figures. */
export const workshopFigures = {
  architecture: {
    alt: 'Agentic AI Platform architecture',
    caption: 'Platform architecture, from the workshop README (Module 1).',
    source: {
      file: WORKSHOP_README_PATH,
      quote: '![Agentic AI Platform architecture](static/img/module-1/agentic-ai-platform-architecture.png)',
    } satisfies Source,
  },
  fast: {
    alt: 'FAST architecture: AgentCore Runtime with Amplify frontend, Cognito auth, Gateway tools, and Memory',
    caption: 'FAST (Fullstack AgentCore Solution Template) architecture, from the workshop Module 4 page.',
    source: {
      file: MODULE_4_INDEX,
      quote: 'AgentCore Runtime with Amplify frontend, Cognito auth, Gateway tools, and Memory',
    } satisfies Source,
  },
};

const MODULE_3B_STEP_7 = `${WORKSHOP}/content/module-3b/step-7/index.en.md`;
const GATEWAY_README_FOR_NOTE = 'enterprise-mcp-governance-gateway/README.md';

/** One-line note under Module 3b: it is not the MCP Gateway project on this site. */
export const module3bNote = {
  text: 'Not the same as the MCP Gateway project on this site: Module 3b attaches its Cedar policy in LOG_ONLY mode and teaches the Registry approval workflow, while the MCP Gateway project runs its policy engine in ENFORCE mode.',
  sources: [
    { file: MODULE_3B_STEP_7, quote: 'Attach with `"mode": "LOG_ONLY"`, not `"ENFORCE"`.' },
    { file: GATEWAY_README_FOR_NOTE, heading: 'Verified architecture', quote: '3. Cedar policy engine (ENFORCE)' },
  ] satisfies Source[],
};

/** Why the notebook folder names do not match the module numbers. */
export const notebookFolderNote = {
  text: 'Folder names under source/ predate the module renumbering: module-4a-tools-gateway holds the Module 3a Tools Gateway notebooks.',
  sources: [
    { file: WORKSHOP_README_PATH, heading: 'Repository structure', quote: 'module-4a-tools-gateway/  # Tools Gateway Lambdas + CDK' },
    { file: WORKSHOP_README_PATH, heading: 'Repository structure', quote: 'MCP Registry + Tools Gateway (tracks 2, 3)' },
  ] satisfies Source[],
};
