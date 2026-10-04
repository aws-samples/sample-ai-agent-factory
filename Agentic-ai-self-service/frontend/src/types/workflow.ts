/**
 * Core workflow type definitions for AgentCore Visual Workflow Platform.
 * These types define the structure of workflows, components, and connections.
 */

import type { ComponentConfiguration } from './components';

// ============================================================================
// Versioned Deployment Governance
// ============================================================================

export const DEPLOYMENT_GOVERNANCE_VERSION = 1 as const;

export interface CfnNamingProfile {
  prefix: string;
  resourceNames?: Record<string, string>;
}

export interface GovernanceTagProfileRef {
  name: string;
  updatedAt: string;
}

export interface DeploymentGovernanceTagsV1 {
  explicitValues: Record<string, string>;
  effectiveValues: Record<string, string>;
  profile: GovernanceTagProfileRef | null;
  policyRevision: string;
}

export interface DeploymentGovernanceV1 {
  version: typeof DEPLOYMENT_GOVERNANCE_VERSION;
  namingProfile: CfnNamingProfile | null;
  tags: DeploymentGovernanceTagsV1;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function assertOnlyKeys(
  value: Record<string, unknown>,
  allowed: readonly string[],
  fieldName: string,
): void {
  const allowedSet = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !allowedSet.has(key));
  if (unknown.length > 0) {
    throw new Error(`${fieldName} contains unknown field(s): ${unknown.join(', ')}`);
  }
}

function readAliasedValue(
  value: Record<string, unknown>,
  camelName: string,
  snakeName: string,
  fieldName: string,
): unknown {
  const hasCamel = Object.prototype.hasOwnProperty.call(value, camelName);
  const hasSnake = Object.prototype.hasOwnProperty.call(value, snakeName);
  if (hasCamel && hasSnake) {
    if (JSON.stringify(value[camelName]) !== JSON.stringify(value[snakeName])) {
      throw new Error(
        `${fieldName}.${camelName} and ${fieldName}.${snakeName} disagree`,
      );
    }
  }
  return hasCamel ? value[camelName] : value[snakeName];
}

function normalizeTagRecord(
  value: unknown,
  fieldName: string,
  allowEmptyValues: boolean,
): Record<string, string> {
  if (value === undefined) return {};
  if (!isRecord(value)) throw new Error(`${fieldName} must be an object`);
  const entries = Object.entries(value).sort(([left], [right]) => left.localeCompare(right));
  if (entries.length > 50) throw new Error(`${fieldName} accepts at most 50 tags`);

  const normalized: Record<string, string> = {};
  for (const [key, tagValue] of entries) {
    if (!key || key.length > 128) {
      throw new Error(`${fieldName} keys must be 1-128 characters`);
    }
    if (typeof tagValue !== 'string' || tagValue.length > 256) {
      throw new Error(`${fieldName}.${key} must be a string of at most 256 characters`);
    }
    if (!allowEmptyValues && !tagValue) {
      throw new Error(`${fieldName}.${key} must not be empty`);
    }
    normalized[key] = tagValue;
  }
  return normalized;
}

function normalizeNamingProfile(value: unknown): CfnNamingProfile | null {
  if (value === undefined || value === null) return null;
  if (!isRecord(value)) throw new Error('governance.namingProfile must be an object or null');
  assertOnlyKeys(
    value,
    ['prefix', 'resourceNames', 'resource_names'],
    'governance.namingProfile',
  );
  if (typeof value.prefix !== 'string' || !/^[a-z][a-z0-9]{0,11}$/.test(value.prefix)) {
    throw new Error(
      'governance.namingProfile.prefix must start with a lowercase letter and contain 1-12 lowercase letters or digits',
    );
  }

  const rawNames = readAliasedValue(
    value,
    'resourceNames',
    'resource_names',
    'governance.namingProfile',
  );
  if (rawNames === undefined) return { prefix: value.prefix };
  if (!isRecord(rawNames)) {
    throw new Error('governance.namingProfile.resourceNames must be an object');
  }
  const entries = Object.entries(rawNames)
    .sort(([left], [right]) => left.localeCompare(right));
  if (entries.length > 64) {
    throw new Error('governance.namingProfile.resourceNames accepts at most 64 overrides');
  }
  const resourceNames: Record<string, string> = {};
  for (const [family, template] of entries) {
    if (!family || family.length > 64) {
      throw new Error('governance.namingProfile.resourceNames keys must be 1-64 characters');
    }
    if (
      typeof template !== 'string'
      || !template
      || template.length > 160
      || [...template].some((character) => {
        const code = character.charCodeAt(0);
        return code < 32 || code > 126;
      })
    ) {
      throw new Error(
        `governance.namingProfile.resourceNames.${family} must be 1-160 printable ASCII characters`,
      );
    }
    resourceNames[family] = template;
  }
  return entries.length > 0 ? { prefix: value.prefix, resourceNames } : { prefix: value.prefix };
}

/**
 * Returns a fresh empty object. Never share a mutable singleton between flows.
 */
export function createEmptyDeploymentGovernance(): DeploymentGovernanceV1 {
  return {
    version: DEPLOYMENT_GOVERNANCE_VERSION,
    namingProfile: null,
    tags: {
      explicitValues: {},
      effectiveValues: {},
      profile: null,
      policyRevision: '',
    },
  };
}

/**
 * Migrates an absent legacy value and strictly normalizes a present V1 value.
 *
 * Both camelCase (wire format) and snake_case (older backend JSON exports) are
 * accepted at the boundary; the returned canonical object is always camelCase.
 */
export function normalizeDeploymentGovernance(value: unknown): DeploymentGovernanceV1 {
  if (value === undefined) return createEmptyDeploymentGovernance();
  if (!isRecord(value)) throw new Error('governance must be an object');
  assertOnlyKeys(
    value,
    ['version', 'namingProfile', 'naming_profile', 'tags'],
    'governance',
  );

  const version = value.version ?? DEPLOYMENT_GOVERNANCE_VERSION;
  if (version !== DEPLOYMENT_GOVERNANCE_VERSION) {
    throw new Error(`Unsupported governance version: ${String(version)}`);
  }

  const rawTags = value.tags ?? {};
  if (!isRecord(rawTags)) throw new Error('governance.tags must be an object');
  assertOnlyKeys(
    rawTags,
    [
      'explicitValues',
      'explicit_values',
      'effectiveValues',
      'effective_values',
      'profile',
      'policyRevision',
      'policy_revision',
    ],
    'governance.tags',
  );
  const explicitValues = normalizeTagRecord(
    readAliasedValue(
      rawTags,
      'explicitValues',
      'explicit_values',
      'governance.tags',
    ),
    'governance.tags.explicitValues',
    true,
  );
  const effectiveValues = normalizeTagRecord(
    readAliasedValue(
      rawTags,
      'effectiveValues',
      'effective_values',
      'governance.tags',
    ),
    'governance.tags.effectiveValues',
    false,
  );

  const rawProfile = rawTags.profile;
  let profile: GovernanceTagProfileRef | null = null;
  if (rawProfile !== undefined && rawProfile !== null) {
    if (!isRecord(rawProfile)) throw new Error('governance.tags.profile must be an object or null');
    assertOnlyKeys(
      rawProfile,
      ['name', 'updatedAt', 'updated_at'],
      'governance.tags.profile',
    );
    const updatedAt = readAliasedValue(
      rawProfile,
      'updatedAt',
      'updated_at',
      'governance.tags.profile',
    );
    if (
      typeof rawProfile.name !== 'string'
      || !rawProfile.name
      || rawProfile.name.length > 128
    ) {
      throw new Error('governance.tags.profile.name must be a non-empty string of at most 128 characters');
    }
    if (typeof updatedAt !== 'string' || !updatedAt || Number.isNaN(Date.parse(updatedAt))) {
      throw new Error('governance.tags.profile.updatedAt must be an ISO timestamp');
    }
    profile = { name: rawProfile.name, updatedAt };
  }

  const rawRevision = readAliasedValue(
    rawTags,
    'policyRevision',
    'policy_revision',
    'governance.tags',
  ) ?? '';
  if (typeof rawRevision !== 'string' || rawRevision.length > 128) {
    throw new Error('governance.tags.policyRevision must be a string of at most 128 characters');
  }
  if (
    !rawRevision
    && (Object.keys(explicitValues).length > 0
      || Object.keys(effectiveValues).length > 0
      || profile !== null)
  ) {
    throw new Error(
      'governance.tags.policyRevision is required when tag values or a profile are captured',
    );
  }

  return {
    version: DEPLOYMENT_GOVERNANCE_VERSION,
    namingProfile: normalizeNamingProfile(readAliasedValue(
      value,
      'namingProfile',
      'naming_profile',
      'governance',
    )),
    tags: {
      explicitValues,
      effectiveValues,
      profile,
      policyRevision: rawRevision,
    },
  };
}

// ============================================================================
// Core Workflow Types
// ============================================================================

export interface WorkflowDefinition {
  id: string;
  name: string;
  description: string;
  version: string;
  nodes: ComponentNode[];
  edges: ConnectionEdge[];
  viewport: Viewport;
  metadata: WorkflowMetadata;
  /**
   * Optional only for legacy API/import payloads. Every current serializer and
   * store hydration path materializes an explicit V1 object.
   */
  governance?: DeploymentGovernanceV1;
  createdAt: string;
  updatedAt: string;
}

export interface ComponentNode {
  id: string;
  type: AgentCoreComponentType;
  position: Position;
  data: ComponentConfiguration;
  selected: boolean;
  validationStatus: ValidationStatus;
}

export interface Position {
  x: number;
  y: number;
}

export interface ConnectionEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle: string;
  targetHandle: string;
  type: ConnectionType;
  animated: boolean;
  data: EdgeData;
}

export interface EdgeData {
  label?: string;
  validationStatus: ValidationStatus;
}

export interface Viewport {
  x: number;
  y: number;
  zoom: number;
}

export interface WorkflowMetadata {
  author: string;
  tags: string[];
  awsRegion: string;
  deploymentStatus: DeploymentStatus;
  lastDeployedAt?: string;
  endpointUrl?: string;
}

// ============================================================================
// Enums and Union Types
// ============================================================================

export type AgentCoreComponentType =
  | 'runtime'
  | 'gateway'
  | 'memory'
  | 'code_interpreter'
  | 'browser'
  | 'observability'
  | 'identity'
  | 'evaluation'
  | 'policy'
  | 'guardrails'
  | 'a2a'
  | 'tool';

export type ConnectionType = 'data' | 'tool' | 'identity';

export type ValidationStatus = 'valid' | 'warning' | 'error' | 'pending';

export type DeploymentStatus = 'not_deployed' | 'deploying' | 'deployed' | 'failed';

export type SaveStatus = 'saved' | 'saving' | 'pending' | 'error';

export type AgentServerProtocol = 'HTTP' | 'MCP' | 'A2A';

export type PythonRuntime = 'PYTHON_3_10' | 'PYTHON_3_11' | 'PYTHON_3_12' | 'PYTHON_3_13';

export type DeploymentType = 'direct_code_deploy' | 'container';
