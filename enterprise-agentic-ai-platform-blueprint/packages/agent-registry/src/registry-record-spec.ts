/**
 * @agenticai/agent-registry — typed record spec.
 *
 * Models an AWS Bedrock AgentCore Registry record at synth time so that the
 * platform CDK app can both:
 *   - emit `CreateRegistryRecord` calls with a validated descriptor payload,
 *   - and resolve a workstream's `subscribedRegistryRecords[]` into the
 *     concrete tool ARN list + Cedar policy that the per-workstream Gateway
 *     stack feeds into its three-layer enforcement model (synth → SCP → IAM).
 *
 * Authoritative on the AWS-side schema:
 *   - https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-supported-record-types.html
 *   - https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-record-lifecycle.html
 *
 * Design decisions encoded here:
 *   - Records are typed (`mcp` | `a2a` | `agent-skills` | `custom`); only `mcp`
 *     is wired into the v0.5.0 Gateway path. The other shapes round-trip
 *     through the validator + mapper but are inert at synth.
 *   - The MCP descriptor's `metadata` carries blueprint-specific fields that
 *     the AgentCore service treats as opaque pass-through: `gatewayTargetArn`,
 *     `cedarPolicy`, `ownerTeam`, `costCentre`, `targetAccountId`. These are
 *     the synth-time pin points; the service never inspects them.
 *   - `${PLATFORM_ACCOUNT_ID}` placeholder is preserved through the spec and
 *     resolved at synth via `resolveTargetArn()` (same convention as the
 *     legacy `ToolSpec`).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

import type { ToolSpec } from '@agenticai/platform-tool-catalogue';

/** Stable, kebab-case identifier for a registry record (record-id surface). */
export type RegistryRecordId = string;

/** Approval state at the AgentCore service. Subset we model at synth. */
export type RegistryRecordStatus =
  | 'CREATING'
  | 'DRAFT'
  | 'PENDING_APPROVAL'
  | 'APPROVED'
  | 'REJECTED'
  | 'DEPRECATED'
  | 'UPDATING'
  | 'CREATE_FAILED'
  | 'UPDATE_FAILED';

/** Inbound auth on the parent registry — immutable post-create at AWS side. */
export type RegistryInboundAuthType = 'AWS_IAM' | 'CUSTOM_JWT';

/**
 * MCP record. Two-descriptor pattern (1 server + N tools).
 *
 * The `metadata` map on the server descriptor is passed through unchanged by
 * AgentCore; we use it to carry the Lambda alias ARN and Cedar snippet. The
 * synth-time validator asserts shape; the per-workstream Gateway synth then
 * pulls the same fields back via `GetRegistryRecord` at deploy time.
 */
export interface McpRegistryRecordSpec {
  readonly recordId: RegistryRecordId;
  readonly descriptorType: 'MCP';
  readonly description: string;
  readonly ownerTeam: string;
  readonly costCentre: string;
  /** Lambda alias ARN — MUST end with `:<alias>` (Q5 pin-via-alias). */
  readonly gatewayTargetArn: string;
  /** When set, the tool lives in a different account (workload→workload, etc.). */
  readonly targetAccountId?: string;
  readonly cedarPolicy: string;
  /** JSONSchema draft-07 for the single MCP tool. */
  readonly inputSchema?: Record<string, unknown>;
  /** Schema versions per AWS docs (server: 2025-12-11, tools: 2025-06-18 or 2025-11-25). */
  readonly serverSchemaVersion?: string;
  readonly toolSchemaVersion?: string;
}

/** A2A (peer-agent) record. The agent-card URL is the primary discovery key. */
export interface A2aRegistryRecordSpec {
  readonly recordId: RegistryRecordId;
  readonly descriptorType: 'A2A';
  readonly description: string;
  readonly ownerTeam: string;
  readonly costCentre: string;
  readonly a2aEndpointUrl: string;
  readonly cedarPolicy: string;
  /** AgentCard schema version. AWS-current default: '0.3'. */
  readonly agentCardSchemaVersion?: string;
}

/** AGENT_SKILLS — markdown skill document + structured definition. Inert at v0.5.0. */
export interface AgentSkillsRegistryRecordSpec {
  readonly recordId: RegistryRecordId;
  readonly descriptorType: 'AGENT_SKILLS';
  readonly description: string;
  readonly ownerTeam: string;
  readonly costCentre: string;
  readonly skillName: string;
  readonly skillVersion: string;
  /** Markdown body. AgentCore validates the descriptor schema, not its content. */
  readonly markdown: string;
}

/** CUSTOM — opaque JSON. AgentCore performs no schema validation beyond JSON well-formedness. */
export interface CustomRegistryRecordSpec {
  readonly recordId: RegistryRecordId;
  readonly descriptorType: 'CUSTOM';
  readonly description: string;
  readonly ownerTeam: string;
  readonly costCentre: string;
  readonly payload: Record<string, unknown>;
}

export type RegistryRecordSpec =
  | McpRegistryRecordSpec
  | A2aRegistryRecordSpec
  | AgentSkillsRegistryRecordSpec
  | CustomRegistryRecordSpec;

const RECORD_ID_PATTERN = /^[a-z0-9-]{3,80}$/;
const LAMBDA_ARN_WITH_ALIAS =
  /^arn:aws:lambda:[a-z0-9-]+:(?:\$\{PLATFORM_ACCOUNT_ID\}|\d{12}):function:[a-zA-Z0-9-_]+:[a-zA-Z0-9-_$]+$/;
const TWELVE_DIGITS = /^\d{12}$/;
const HTTPS_URL = /^https:\/\/[^\s]+$/;

/**
 * Synth-time validator. Throws with a record-id-prefixed actionable message
 * so a CDK synth failure points the developer straight at the offending file.
 */
export function validateRegistryRecordSpec(spec: RegistryRecordSpec): void {
  if (!RECORD_ID_PATTERN.test(spec.recordId)) {
    throw new Error(
      `RegistryRecord ${spec.recordId}: recordId must match ${RECORD_ID_PATTERN}`,
    );
  }
  if (typeof spec.description !== 'string' || spec.description.length === 0) {
    throw new Error(`RegistryRecord ${spec.recordId}: description is required`);
  }
  if (typeof spec.ownerTeam !== 'string' || spec.ownerTeam.length === 0) {
    throw new Error(`RegistryRecord ${spec.recordId}: ownerTeam is required`);
  }
  if (typeof spec.costCentre !== 'string' || spec.costCentre.length === 0) {
    throw new Error(`RegistryRecord ${spec.recordId}: costCentre is required`);
  }

  switch (spec.descriptorType) {
    case 'MCP': {
      if (!LAMBDA_ARN_WITH_ALIAS.test(spec.gatewayTargetArn)) {
        throw new Error(
          `RegistryRecord ${spec.recordId}: gatewayTargetArn MUST end with a Lambda alias (Q5 pin-via-alias). Got: ${spec.gatewayTargetArn}`,
        );
      }
      if (spec.targetAccountId && !TWELVE_DIGITS.test(spec.targetAccountId)) {
        throw new Error(
          `RegistryRecord ${spec.recordId}: targetAccountId must be 12 digits`,
        );
      }
      if (!spec.cedarPolicy.includes('permit')) {
        throw new Error(
          `RegistryRecord ${spec.recordId}: cedarPolicy must contain permit()`,
        );
      }
      return;
    }
    case 'A2A': {
      if (!HTTPS_URL.test(spec.a2aEndpointUrl)) {
        throw new Error(
          `RegistryRecord ${spec.recordId}: a2aEndpointUrl must start with https://`,
        );
      }
      if (!spec.cedarPolicy.includes('permit')) {
        throw new Error(
          `RegistryRecord ${spec.recordId}: cedarPolicy must contain permit()`,
        );
      }
      return;
    }
    case 'AGENT_SKILLS': {
      if (!spec.skillName || !spec.skillVersion || !spec.markdown) {
        throw new Error(
          `RegistryRecord ${spec.recordId}: skillName, skillVersion, markdown are all required`,
        );
      }
      return;
    }
    case 'CUSTOM': {
      if (typeof spec.payload !== 'object' || spec.payload === null) {
        throw new Error(`RegistryRecord ${spec.recordId}: payload must be a JSON object`);
      }
      return;
    }
    default: {
      const exhaustive: never = spec;
      throw new Error(
        `RegistryRecord (unknown): unsupported descriptorType ${(exhaustive as RegistryRecordSpec).descriptorType}`,
      );
    }
  }
}

/**
 * Substitute `${PLATFORM_ACCOUNT_ID}` in an MCP record's `gatewayTargetArn`
 * with the resolved platform-account id (or the explicit cross-account
 * `targetAccountId` if set). Mirrors the legacy `resolveTargetArn` helper.
 */
export function resolveGatewayTargetArn(
  spec: McpRegistryRecordSpec,
  platformAccountId: string,
): string {
  const acct = spec.targetAccountId ?? platformAccountId;
  return spec.gatewayTargetArn.replace('${PLATFORM_ACCOUNT_ID}', acct);
}

/**
 * Render the MCP record's two-descriptor payload as the AgentCore SDK shape
 * (`descriptors: [server, tool]`). Output is consumed by
 * `RegistryRecordConstruct` when calling `CreateRegistryRecord`.
 */
export function renderMcpDescriptorPayload(spec: McpRegistryRecordSpec): {
  descriptorType: 'MCP';
  descriptors: Record<string, unknown>;
} {
  const serverSchema = spec.serverSchemaVersion ?? '2025-12-11';
  // LANDMINE (live-verified 2026-07-02 against a known-good workshop record):
  // CreateRegistryRecord expects
  //   descriptors = { mcp: { server: { schemaVersion, inlineContent },
  //                          tools:  { protocolVersion, inlineContent } } }
  // where each inlineContent is a JSON *string* that must VALIDATE against the
  // MCP protocol schema for the declared version. The server document is the
  // strict MCP server-definition shape { name, description, version } ONLY —
  // extra keys (transport, inboundAuth, metadata) fail schema validation
  // ("content is not in compliance with schema version '2025-12-11'"). The
  // tools protocolVersion that AWS accepts today is '2024-11-05'.
  //
  // Governance metadata (gatewayTargetArn, cedarPolicy, ownerTeam, costCentre)
  // deliberately does NOT live in the record content — it is enforced on the
  // Gateway target + Cedar policy layers. The registry record is the public
  // MCP/A2A protocol document only.
  const toolProtocol = spec.toolSchemaVersion ?? '2024-11-05';
  const serverInline = JSON.stringify({
    name: `agenticai/${spec.recordId}`,
    description: spec.description,
    version: '1.0.0',
  });
  const toolsInline = JSON.stringify({
    tools: [
      {
        name: spec.recordId,
        description: spec.description,
        inputSchema: spec.inputSchema ?? { type: 'object' },
      },
    ],
  });
  return {
    descriptorType: 'MCP',
    descriptors: {
      mcp: {
        server: { schemaVersion: serverSchema, inlineContent: serverInline },
        tools: { protocolVersion: toolProtocol, inlineContent: toolsInline },
      },
    },
  };
}

/**
 * Render the A2A record's agent-card descriptor.
 */
export function renderA2aDescriptorPayload(spec: A2aRegistryRecordSpec): {
  descriptorType: 'A2A';
  descriptors: Record<string, unknown>;
} {
  // LANDMINE (live-verified 2026-07-02 against a known-good workshop record):
  // descriptors = { a2a: { agentCard: { schemaVersion, inlineContent } } }
  // where inlineContent must be a full A2A agent-card document conforming to
  // the A2A protocol schema: protocolVersion, name, description, url, version,
  // capabilities, defaultInputModes/OutputModes, skills[]. A minimal
  // {name,url,metadata} document fails schema validation. Governance metadata
  // is enforced at the Cedar layer, not embedded in the card.
  const schemaVersion = spec.agentCardSchemaVersion ?? '0.3';
  const agentCardInline = JSON.stringify({
    protocolVersion: schemaVersion,
    name: spec.recordId,
    description: spec.description,
    url: spec.a2aEndpointUrl,
    version: '1.0.0',
    capabilities: { streaming: false, pushNotifications: false },
    defaultInputModes: ['text/plain'],
    defaultOutputModes: ['text/plain'],
    skills: [
      {
        id: spec.recordId,
        name: spec.recordId,
        description: spec.description,
        tags: [spec.ownerTeam],
      },
    ],
  });
  return {
    descriptorType: 'A2A',
    descriptors: {
      a2a: {
        agentCard: {
          schemaVersion,
          inlineContent: agentCardInline,
        },
      },
    },
  };
}

/**
 * Map a legacy `ToolSpec` (from `@agenticai/platform-tool-catalogue`) into a
 * `RegistryRecordSpec`. Used by the platform stack to seed the Registry from
 * the existing in-process catalogue during the v0.4.0 → v0.5.0 migration.
 *
 * The mapping is one-to-one for `lambda` tools (→ MCP record) and `agent-a2a`
 * tools (→ A2A record). The legacy `approvalStatus` field is irrelevant on the
 * record-spec side: the AWS Registry owns the lifecycle state machine and we
 * drive it through `SubmitForApproval` + `UpdateRegistryRecordStatus`.
 */
export function toolSpecToRegistryRecordSpec(spec: ToolSpec): RegistryRecordSpec {
  const toolType = spec.toolType ?? 'lambda';
  if (toolType === 'lambda') {
    return {
      recordId: spec.toolId,
      descriptorType: 'MCP',
      description: spec.description,
      ownerTeam: spec.ownerTeam,
      costCentre: spec.costCentre,
      gatewayTargetArn: spec.targetArn,
      targetAccountId: spec.targetAccountId,
      cedarPolicy: spec.cedarPolicy,
      inputSchema: spec.inputSchema,
    };
  }
  if (toolType === 'agent-a2a') {
    if (!spec.a2aEndpointUrl) {
      throw new Error(
        `ToolSpec ${spec.toolId}: agent-a2a tools require a2aEndpointUrl`,
      );
    }
    return {
      recordId: spec.toolId,
      descriptorType: 'A2A',
      description: spec.description,
      ownerTeam: spec.ownerTeam,
      costCentre: spec.costCentre,
      a2aEndpointUrl: spec.a2aEndpointUrl,
      cedarPolicy: spec.cedarPolicy,
    };
  }
  throw new Error(`ToolSpec ${spec.toolId}: unsupported toolType ${toolType}`);
}
