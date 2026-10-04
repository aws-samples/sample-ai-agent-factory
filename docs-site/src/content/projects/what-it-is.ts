/**
 * "What it is" bullets for each project page: the feature copy from data.ts,
 * each bullet paired with the repository text that supports it.
 *
 * Three projects cite the single README section recorded in
 * `project.sources.features`. The Blueprint's bullets are spread over several
 * README sections and two CDK packages, so each bullet carries its own source.
 * projects.test.ts checks that every bullet text is one of `project.features`
 * and that kept plus dropped bullets cover the list exactly.
 */
import { projects, type ProjectId } from '../data';
import type { Source } from '../facts';

const BLUEPRINT = 'enterprise-agentic-ai-platform-blueprint';
const BLUEPRINT_README = `${BLUEPRINT}/README.md`;
const BLUEPRINT_VPC_CONSTRUCT = `${BLUEPRINT}/packages/agentic-vpc/src/agentic-vpc-construct.ts`;
const BLUEPRINT_AGENTIC_APP = `${BLUEPRINT}/packages/agentic-app/src/agentic-app.ts`;

/** One bullet with the text that supports it. */
export interface WhatItIsItem {
  text: string;
  sources: Source[];
}

function fromProjectFeatures(projectId: ProjectId): WhatItIsItem[] {
  const project = projects.find(p => p.id === projectId);
  if (!project) throw new Error(`Unknown project ${projectId}`);
  return project.features.map(text => ({ text, sources: [project.sources.features] }));
}

/**
 * Blueprint bullets with the section or file that actually contains each claim.
 * The README's "10.1 Control summary" (cited in data.ts) holds only the SCP and OAM bullets.
 */
const blueprintItems: WhatItIsItem[] = [
  {
    text: 'Multi-account architecture with Management, Platform, and Workstream account roles',
    sources: [
      {
        file: BLUEPRINT_README,
        heading: '2.1 Logical topology and cardinality',
        quote: 'The Management, Platform, and Workstream names describe **account roles**',
      },
    ],
  },
  {
    text: 'Service control policies for model, Region, Guardrail, Registry, Gateway, and deployment boundaries',
    sources: [
      {
        file: BLUEPRINT_README,
        heading: '10.1 Control summary',
        quote: 'Organizations SCPs for model, Region, Guardrail, Registry, Gateway, and deployment boundaries.',
      },
    ],
  },
  {
    text: 'Per-tenant Application Inference Profiles',
    sources: [
      { file: BLUEPRINT_AGENTIC_APP, quote: 'readonly inferenceProfile: CfnApplicationInferenceProfile;' },
      { file: BLUEPRINT_AGENTIC_APP, quote: 'readonly tenantId: string;' },
      { file: BLUEPRINT_README, heading: '4. AWS services used', quote: 'Bedrock application inference profiles' },
    ],
  },
  {
    text: 'Private VPC with interface endpoints (no NAT)',
    sources: [
      { file: BLUEPRINT_VPC_CONSTRUCT, quote: 'VPC (no IGW, no NAT; spec §2.3.2 L1034)' },
      { file: BLUEPRINT_VPC_CONSTRUCT, quote: 'subnetType: SubnetType.PRIVATE_ISOLATED,' },
      { file: BLUEPRINT_README, heading: '4. AWS services used', quote: 'Amazon VPC, VPC endpoints, security groups' },
    ],
  },
  {
    text: 'Evaluation gate in the Runtime/Memory pipeline shape',
    sources: [
      {
        file: BLUEPRINT_README,
        heading: '6.5 Onboard a Workstream cell',
        quote: 'invokes the deployed Runtime through the evaluation gate',
      },
    ],
  },
  {
    text: 'Fleet observability with CloudWatch OAM',
    sources: [
      { file: BLUEPRINT_README, heading: '10.1 Control summary', quote: 'OAM links for centralized Logs, Metrics, and Traces.' },
    ],
  },
  {
    text: 'Adversarial evidence model: every negative test needs an authorized positive twin',
    sources: [
      {
        file: BLUEPRINT_README,
        heading: '10.2 Threat and evidence model',
        quote: 'every negative test requires an authorized positive twin in the same run;',
      },
    ],
  },
];

/** Feature bullets from data.ts that no repository text supports; they are not rendered. */
export const droppedFeatures: Partial<Record<ProjectId, string[]>> = {};

export const whatItIs: Record<ProjectId, WhatItIsItem[]> = {
  workshop: fromProjectFeatures('workshop'),
  'self-service': fromProjectFeatures('self-service'),
  'mcp-gateway': fromProjectFeatures('mcp-gateway'),
  blueprint: blueprintItems,
};

/** "What it is" bullets for one project. */
export function getWhatItIs(projectId: ProjectId): WhatItIsItem[] {
  return whatItIs[projectId];
}
