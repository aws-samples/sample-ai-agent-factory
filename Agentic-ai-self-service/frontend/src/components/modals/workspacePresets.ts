/**
 * Workspace policy presets (plan §3.B, step 5): Locked-down, Standard, Developer.
 *
 * A preset is a complete `WorkspacePolicyDocument` built from the few things only the admin knows
 * (the workspace's id and name, its member and admin groups, and which Cognito groups approve). The
 * form applies one and the admin edits from there; nothing here is sent until they save.
 *
 * The documents are a cross-language contract: `workspacePresets.test.ts` snapshots each one under
 * backend/tests/fixtures/workspace_policy/presets/, and backend/tests/test_workspace_presets.py
 * validates them against the real `WorkspacePolicy` schema and pins what they admit (every gallery
 * template deploys under Standard; a write connector is refused under Locked-down). A preset change
 * therefore shows up as a fixture diff the backend has to agree with.
 *
 * Why the approval groups are inputs rather than constants: a group nobody holds would gate a tier
 * forever (fail closed, but useless), and a preset cannot know an organisation's group names.
 */

import {
  DEFAULT_WORKSPACE_ID,
  type ApprovalRouting,
  type WorkspacePolicyDocument,
} from '../../services/api/workspacePolicies';

export type WorkspacePresetId = 'locked-down' | 'standard' | 'developer';

export interface WorkspacePresetInput {
  workspaceId: string;
  name: string;
  memberGroups: string[];
  adminGroups: string[];
  /** Groups that review medium-risk agents (and, with security, high-risk ones). */
  approverGroups: string[];
  /** Groups that must also clear high-risk agents. */
  securityGroups: string[];
}

export interface WorkspacePreset {
  id: WorkspacePresetId;
  label: string;
  /** One sentence an admin reads before choosing it. */
  summary: string;
  build: (input: WorkspacePresetInput) => WorkspacePolicyDocument;
}

function unique(groups: string[]): string[] {
  return [...new Set(groups.map((group) => group.trim()).filter(Boolean))];
}

/** Low auto-approves; medium needs the approvers; high needs security and the approvers. */
function reviewedApproval(input: WorkspacePresetInput): ApprovalRouting {
  return {
    low: [],
    medium: unique(input.approverGroups),
    high: unique([...input.securityGroups, ...input.approverGroups]),
  };
}

/** Everything unconstrained, everything auto-approved, conservative defaults: the schema's own defaults. */
export function emptyPolicy(workspaceId: string, name: string): WorkspacePolicyDocument {
  return {
    workspaceId,
    name,
    adminGroups: [],
    memberGroups: [],
    allowed: {
      providers: [],
      models: [],
      connectors: [],
      toolRiskClasses: [],
      dataClasses: [],
      channels: [],
      regions: [],
    },
    budgets: { perAgentMonthlyUsd: null, maxAgentMonthlyUsd: null, perWorkspaceMonthlyUsd: null },
    approval: { low: [], medium: [], high: [] },
    defaults: {
      platformVersion: null,
      guardrailProfile: 'standard',
      memory: 'shortTerm',
      unclassifiedRiskTier: 'medium',
      trustBuilderClassifications: false,
    },
  };
}

function base(input: WorkspacePresetInput): WorkspacePolicyDocument {
  const policy = emptyPolicy(input.workspaceId.trim(), input.name.trim());
  policy.memberGroups = input.workspaceId.trim() === DEFAULT_WORKSPACE_ID ? [] : unique(input.memberGroups);
  policy.adminGroups = unique(input.adminGroups);
  return policy;
}

/**
 * Locked-down: Bedrock only, verifiable READ connectors only (an unclassified Lambda or MCP tool is
 * refused, not floored), public/internal data, web and API channels, strict Gateway guardrails, and
 * unknown items floored to high. Meant for connector-only business agents; a canvas with a write
 * connector or a custom tool does not get in.
 */
export function lockedDown(input: WorkspacePresetInput): WorkspacePolicyDocument {
  const policy = base(input);
  policy.allowed.providers = ['bedrock'];
  policy.allowed.toolRiskClasses = ['read'];
  policy.allowed.dataClasses = ['public', 'internal'];
  policy.allowed.channels = ['web', 'api'];
  policy.budgets = { perAgentMonthlyUsd: 50, maxAgentMonthlyUsd: 100, perWorkspaceMonthlyUsd: 1000 };
  policy.approval = reviewedApproval(input);
  policy.defaults.guardrailProfile = 'strict';
  policy.defaults.unclassifiedRiskTier = 'high';
  policy.defaults.trustBuilderClassifications = false;
  return policy;
}

/**
 * Standard: Bedrock only; tools are not filtered by class (the TIER does the governing: an unclassified
 * or write tool makes the agent medium, so the approvers see it; pay tools and sensitive data make it
 * high, so security sees it too). Every gallery template deploys under it (pinned by the backend test).
 */
export function standard(input: WorkspacePresetInput): WorkspacePolicyDocument {
  const policy = base(input);
  policy.allowed.providers = ['bedrock'];
  policy.budgets = { perAgentMonthlyUsd: 100, maxAgentMonthlyUsd: 500, perWorkspaceMonthlyUsd: 5000 };
  policy.approval = reviewedApproval(input);
  policy.defaults.guardrailProfile = 'standard';
  policy.defaults.unclassifiedRiskTier = 'medium';
  policy.defaults.trustBuilderClassifications = false;
  return policy;
}

/**
 * Developer: nothing constrained, every tier auto-approved, builders' own classifications trusted and
 * unclassified prototypes floored to low. The builders are the reviewers.
 */
export function developer(input: WorkspacePresetInput): WorkspacePolicyDocument {
  const policy = base(input);
  policy.approval = { low: [], medium: [], high: [] };
  policy.defaults.guardrailProfile = 'standard';
  policy.defaults.unclassifiedRiskTier = 'low';
  policy.defaults.trustBuilderClassifications = true;
  return policy;
}

export const WORKSPACE_PRESETS: readonly WorkspacePreset[] = [
  {
    id: 'locked-down',
    label: 'Locked-down',
    summary:
      'Bedrock only, read-only verified connectors, public/internal data, strict guardrails; medium and high risk need approval.',
    build: lockedDown,
  },
  {
    id: 'standard',
    label: 'Standard',
    summary:
      'Bedrock only; any tool, but unclassified or write tools route to the approvers and payments or sensitive data to security.',
    build: standard,
  },
  {
    id: 'developer',
    label: 'Developer',
    summary: 'Nothing constrained, everything auto-approved, builder classifications trusted.',
    build: developer,
  },
];

export function applyPreset(id: WorkspacePresetId, input: WorkspacePresetInput): WorkspacePolicyDocument {
  const preset = WORKSPACE_PRESETS.find((candidate) => candidate.id === id);
  if (!preset) {
    throw new Error(`Unknown workspace preset: ${id}`);
  }
  return preset.build(input);
}
