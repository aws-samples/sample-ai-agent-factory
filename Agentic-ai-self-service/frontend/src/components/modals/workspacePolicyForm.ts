/**
 * The Admin → Workspaces form model (plan §3.B, step 5): pure conversions between the strict
 * `WorkspacePolicyDocument` the API speaks and the text an admin types.
 *
 * List fields are comma- or whitespace-separated text; budgets are text so an empty box means
 * "unset" (null on the wire) rather than 0; the two preset inputs (approver and security groups)
 * live on the form but are not part of the document. Everything here is a pure function so the
 * panel's rendering tests and these conversions can be pinned separately.
 */

import type {
  AllowedDataClass,
  AllowedResources,
  AllowedToolRiskClass,
  ApprovalRouting,
  ChannelType,
  GuardrailProfile,
  PlatformVersion,
  RiskTier,
  WorkspacePolicyDocument,
} from '../../services/api/workspacePolicies';
import type { WorkspacePresetInput } from './workspacePresets';

export interface WorkspacePolicyForm {
  workspaceId: string;
  name: string;
  memberGroups: string;
  adminGroups: string;
  /** Preset inputs, not persisted: the groups presets route medium and high risk to. */
  approverGroups: string;
  securityGroups: string;
  providers: string;
  models: string;
  connectors: string;
  regions: string;
  toolRiskClasses: AllowedToolRiskClass[];
  dataClasses: AllowedDataClass[];
  channels: ChannelType[];
  perAgentMonthlyUsd: string;
  maxAgentMonthlyUsd: string;
  perWorkspaceMonthlyUsd: string;
  approvalLow: string;
  approvalMedium: string;
  approvalHigh: string;
  platformVersion: '' | PlatformVersion;
  guardrailProfile: GuardrailProfile;
  memory: string;
  unclassifiedRiskTier: RiskTier;
  trustBuilderClassifications: boolean;
}

/** Split a typed list on commas, semicolons or whitespace; trimmed, de-duplicated, order kept. */
export function splitList(text: string): string[] {
  return [...new Set(text.split(/[,;\s]+/).map((item) => item.trim()).filter(Boolean))];
}

export function joinList(items: readonly string[]): string {
  return items.join(', ');
}

function budgetText(value: number | null): string {
  return value === null ? '' : String(value);
}

/**
 * The form for an existing document. The preset inputs are recovered from the approval routing
 * (medium's groups are the approvers; high's extra groups are security) unless given explicitly.
 */
export function formFromDocument(
  document: WorkspacePolicyDocument,
  presetGroups?: { approverGroups?: string; securityGroups?: string },
): WorkspacePolicyForm {
  const security = document.approval.high.filter((group) => !document.approval.medium.includes(group));
  return {
    workspaceId: document.workspaceId,
    name: document.name,
    memberGroups: joinList(document.memberGroups),
    adminGroups: joinList(document.adminGroups),
    approverGroups: presetGroups?.approverGroups ?? joinList(document.approval.medium),
    securityGroups: presetGroups?.securityGroups ?? joinList(security),
    providers: joinList(document.allowed.providers),
    models: joinList(document.allowed.models),
    connectors: joinList(document.allowed.connectors),
    regions: joinList(document.allowed.regions),
    toolRiskClasses: [...document.allowed.toolRiskClasses],
    dataClasses: [...document.allowed.dataClasses],
    channels: [...document.allowed.channels],
    perAgentMonthlyUsd: budgetText(document.budgets.perAgentMonthlyUsd),
    maxAgentMonthlyUsd: budgetText(document.budgets.maxAgentMonthlyUsd),
    perWorkspaceMonthlyUsd: budgetText(document.budgets.perWorkspaceMonthlyUsd),
    approvalLow: joinList(document.approval.low),
    approvalMedium: joinList(document.approval.medium),
    approvalHigh: joinList(document.approval.high),
    platformVersion: document.defaults.platformVersion ?? '',
    guardrailProfile: document.defaults.guardrailProfile,
    memory: document.defaults.memory,
    unclassifiedRiskTier: document.defaults.unclassifiedRiskTier,
    trustBuilderClassifications: document.defaults.trustBuilderClassifications,
  };
}

/** A budget box: empty is unset (null); anything else must be a non-negative number. */
export function parseBudget(text: string, label: string): number | null {
  const trimmed = text.trim();
  if (trimmed === '') {
    return null;
  }
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value < 0) {
    throw new Error(`${label} must be a non-negative number (got "${text}").`);
  }
  return value;
}

export function presetInputFromForm(form: WorkspacePolicyForm): WorkspacePresetInput {
  return {
    workspaceId: form.workspaceId.trim(),
    name: form.name.trim(),
    memberGroups: splitList(form.memberGroups),
    adminGroups: splitList(form.adminGroups),
    approverGroups: splitList(form.approverGroups),
    securityGroups: splitList(form.securityGroups),
  };
}

/** The document to PUT. Throws a plain-language error for what the backend would refuse anyway. */
export function documentFromForm(form: WorkspacePolicyForm): WorkspacePolicyDocument {
  const workspaceId = form.workspaceId.trim();
  const name = form.name.trim();
  if (!workspaceId) {
    throw new Error('A workspace id is required.');
  }
  if (!name) {
    throw new Error('A workspace name is required.');
  }
  return {
    workspaceId,
    name,
    adminGroups: splitList(form.adminGroups),
    memberGroups: splitList(form.memberGroups),
    allowed: {
      providers: splitList(form.providers),
      models: splitList(form.models),
      connectors: splitList(form.connectors),
      toolRiskClasses: [...form.toolRiskClasses],
      dataClasses: [...form.dataClasses],
      channels: [...form.channels],
      regions: splitList(form.regions),
    },
    budgets: {
      perAgentMonthlyUsd: parseBudget(form.perAgentMonthlyUsd, 'The per-agent monthly budget'),
      maxAgentMonthlyUsd: parseBudget(form.maxAgentMonthlyUsd, 'The per-agent cap'),
      perWorkspaceMonthlyUsd: parseBudget(form.perWorkspaceMonthlyUsd, 'The workspace monthly budget'),
    },
    approval: {
      low: splitList(form.approvalLow),
      medium: splitList(form.approvalMedium),
      high: splitList(form.approvalHigh),
    },
    defaults: {
      platformVersion: form.platformVersion === '' ? null : form.platformVersion,
      guardrailProfile: form.guardrailProfile,
      memory: form.memory.trim() || 'shortTerm',
      unclassifiedRiskTier: form.unclassifiedRiskTier,
      trustBuilderClassifications: form.trustBuilderClassifications,
    },
  };
}

/** One line per constrained axis; "unconstrained" when every allow-list is empty. */
export function summarizeAllowed(allowed: AllowedResources): string {
  const parts: string[] = [];
  if (allowed.providers.length) parts.push(`providers ${allowed.providers.join('/')}`);
  if (allowed.models.length) parts.push(`${allowed.models.length} model${allowed.models.length === 1 ? '' : 's'}`);
  if (allowed.connectors.length) parts.push(`connectors ${allowed.connectors.join('/')}`);
  if (allowed.toolRiskClasses.length) parts.push(`tool classes ${allowed.toolRiskClasses.join('/')}`);
  if (allowed.dataClasses.length) parts.push(`data ${allowed.dataClasses.join('/')}`);
  if (allowed.channels.length) parts.push(`channels ${allowed.channels.join('/')}`);
  if (allowed.regions.length) parts.push(`Regions ${allowed.regions.join('/')}`);
  return parts.length ? parts.join(' · ') : 'unconstrained';
}

/** "low auto · medium g-approvers · high g-security+g-approvers". */
export function summarizeApproval(approval: ApprovalRouting): string {
  const tier = (label: string, groups: string[]) => `${label} ${groups.length ? groups.join('+') : 'auto'}`;
  return [tier('low', approval.low), tier('medium', approval.medium), tier('high', approval.high)].join(' · ');
}
