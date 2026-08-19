/**
 * @agenticai/platform-tool-catalogue — Platform Tool Catalogue SSOT.
 *
 * Single source of truth for every tool any workstream may subscribe to via
 * `D03TenantAllocation.allowedToolIds`. Authoritative at synth; unknown ids
 * fail the CDK synth with an actionable error.
 *
 * Same pattern as `packages/platform-baselines/src/allowed-models.ts`:
 *   - TypeScript constant is authoritative
 *   - Conformance + unit tests catch drift
 *   - Downstream consumers (per-workstream AgentCore Gateway synth) resolve
 *     their subscribed subset via `resolveSubscribedTools()`.
 *
 * Architectural decisions encoded here:
 *   - Q1 (hybrid owner placement): platform-account tools omit `targetAccountId`;
 *     workload-account tools set it explicitly (12-digit string).
 *   - Q3 (per-tool Cedar, union-ed at synth): every ToolSpec carries a Cedar
 *     snippet; subscribed subsets compose into the Gateway policy document.
 *   - Q5 (pin via Lambda alias 'PROD'): `targetArn` MUST end with an alias;
 *     synth-time validation asserts.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export type ToolId = string; // branded string; keep simple for v1

/**
 * A tool spec in the Platform Tool Catalogue. Immutable at synth.
 *
 * Q5 decision (pin via Lambda alias 'PROD'): targetArn MUST include an alias
 * suffix (e.g. `:PROD`). Synth-time validation asserts.
 *
 * Q1 decision (hybrid — platform-default, workload-targets explicit via
 * `targetAccountId`): if a tool lives outside the platform account, the
 * caller MUST set `targetAccountId` explicitly. Platform-account tools
 * omit it; synth emits the platform account id automatically.
 *
 * Q3 decision (per-tool Cedar, union-ed at synth): `cedarPolicy` is the
 * Cedar policy snippet that governs calls TO this tool. Each subscribed
 * workstream's Gateway policy document is built by union-ing the cedarPolicy
 * strings of every tool the workstream subscribed to.
 */
export type ToolType = 'lambda' | 'agent-a2a';

export interface ToolSpec {
  readonly toolId: ToolId;                     // unique within catalogue; kebab-case pattern
  /** Z7-K: kind of tool. Defaults to 'lambda'. 'agent-a2a' targets a peer-agent A2A endpoint. */
  readonly toolType?: ToolType;
  readonly targetArn: string;                  // Lambda ARN, MUST end with :<alias> — validate at synth
  readonly targetAccountId?: string;           // optional; when present the tool lives cross-account (platform-workload or workload-workload)
  readonly cedarPolicy: string;                // per-tool Cedar snippet, union-ed into Gateway policy at synth
  readonly ownerTeam: string;                  // e.g. 'platform-ai', 'retail', 'hr'
  readonly costCentre: string;                 // CUR attribution passthrough
  readonly description: string;                // human-readable; appears in Registry DDB
  readonly approvalStatus: 'approved' | 'experimental' | 'deprecated';
  readonly inputSchema?: Record<string, unknown>; // JSONSchema draft-07; passed into CreateGatewayTarget.targetConfiguration.mcp.lambda.toolSchema.inlinePayload
  /**
   * Z7-K: when toolType === 'agent-a2a' this is the peer agent's A2A
   * endpoint URL (must be HTTPS). When 'lambda', leave undefined.
   */
  readonly a2aEndpointUrl?: string;
  /**
   * Phase Q (v0.6.0) — second-layer entitlement: the set of Cognito group
   * names whose JWTs are permitted to invoke this tool. Empty/undefined ⇒
   * any authenticated principal allowed (the existing v0.5.0 behaviour).
   * When present, two things change:
   *   1. The composed Cedar bundle binds the permit to `principal in
   *      CognitoGroup::"<g>"` instead of an unconditional permit.
   *   2. The workstream Gateway stack throws at synth unless
   *      `cognitoDiscoveryUrl` is supplied (CUSTOM_JWT becomes mandatory —
   *      AWS_IAM mode has no JWT claims to evaluate against).
   */
  readonly allowedGroups?: readonly string[];
}

/** Cognito group names: lower-case kebab/snake, max 128 chars (AWS limit). */
const COGNITO_GROUP_REGEX = /^[A-Za-z0-9_+=,.@-]{1,128}$/;

/** The authoritative catalogue. Append-only in v1; version bumps via PR. */
export const PLATFORM_TOOL_CATALOGUE: Readonly<Record<ToolId, ToolSpec>> = {
  // Two demo tools to start — platform-owned, approved.
  'tool-echo': {
    toolId: 'tool-echo',
    targetArn: 'arn:aws:lambda:us-east-1:${PLATFORM_ACCOUNT_ID}:function:agenticai-d03-tool-echo:PROD',
    cedarPolicy: 'permit(principal, action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    ownerTeam: 'platform-ai',
    costCentre: 'platform',
    description: 'Echoes the input string. Canonical health-check tool.',
    approvalStatus: 'approved',
    inputSchema: { type: 'object', properties: { message: { type: 'string' } }, required: ['message'] },
  },
  'tool-ping': {
    toolId: 'tool-ping',
    targetArn: 'arn:aws:lambda:us-east-1:${PLATFORM_ACCOUNT_ID}:function:agenticai-d03-tool-ping:PROD',
    cedarPolicy: 'permit(principal, action == Action::"InvokeTool", resource == Tool::"tool-ping");',
    ownerTeam: 'platform-ai',
    costCentre: 'platform',
    description: 'Returns pong + timestamp + caller principal. Observability probe.',
    approvalStatus: 'approved',
    inputSchema: { type: 'object', properties: {} },
  },
};

export const PLATFORM_TOOL_CATALOGUE_VERSION = '1';

// Pattern matchers:
const LAMBDA_ARN_WITH_ALIAS = /^arn:aws:lambda:[a-z0-9-]+:(?:\$\{PLATFORM_ACCOUNT_ID\}|\d{12}):function:[a-zA-Z0-9-_]+:[a-zA-Z0-9-_$]+$/;

/** Validate a ToolSpec at synth; throws with actionable message. */
export function validateToolSpec(spec: ToolSpec): void {
  if (!/^[a-z0-9-]{3,50}$/.test(spec.toolId)) {
    throw new Error(`ToolId must be kebab-case 3-50 chars: ${spec.toolId}`);
  }
  const toolType: ToolType = spec.toolType ?? 'lambda';
  if (toolType === 'lambda') {
    if (!LAMBDA_ARN_WITH_ALIAS.test(spec.targetArn)) {
      throw new Error(
        `Tool ${spec.toolId}: targetArn MUST end with a Lambda alias (Q5 pin-via-alias). Got: ${spec.targetArn}`,
      );
    }
    if (spec.a2aEndpointUrl) {
      throw new Error(`Tool ${spec.toolId}: a2aEndpointUrl is only valid when toolType='agent-a2a'`);
    }
  } else if (toolType === 'agent-a2a') {
    if (!spec.a2aEndpointUrl || !/^https:\/\//.test(spec.a2aEndpointUrl)) {
      throw new Error(`Tool ${spec.toolId}: agent-a2a tools require a2aEndpointUrl starting with https://`);
    }
  } else {
    throw new Error(`Tool ${spec.toolId}: unsupported toolType ${toolType}`);
  }
  if (spec.targetAccountId && !/^\d{12}$/.test(spec.targetAccountId)) {
    throw new Error(`Tool ${spec.toolId}: targetAccountId must be 12 digits`);
  }
  if (!spec.cedarPolicy.includes('permit')) {
    throw new Error(`Tool ${spec.toolId}: cedarPolicy must contain permit()`);
  }
  if (!['approved', 'experimental', 'deprecated'].includes(spec.approvalStatus)) {
    throw new Error(`Tool ${spec.toolId}: approvalStatus invalid`);
  }
  if (spec.allowedGroups !== undefined) {
    if (!Array.isArray(spec.allowedGroups) || spec.allowedGroups.length === 0) {
      throw new Error(
        `Tool ${spec.toolId}: allowedGroups, when present, must be a non-empty array of Cognito group names`,
      );
    }
    for (const g of spec.allowedGroups) {
      if (typeof g !== 'string' || !COGNITO_GROUP_REGEX.test(g)) {
        throw new Error(
          `Tool ${spec.toolId}: allowedGroups entry '${g}' is not a valid Cognito group name`,
        );
      }
    }
  }
}

/**
 * Resolve a subscription list into the catalogue subset, failing at synth
 * on any unknown id OR any deprecated tool being subscribed to fresh.
 * This is the synth-time gate from the three-layer governance model.
 */
export function resolveSubscribedTools(allowedToolIds: readonly ToolId[]): ToolSpec[] {
  const unknown = allowedToolIds.filter((id) => !(id in PLATFORM_TOOL_CATALOGUE));
  if (unknown.length > 0) {
    throw new Error(
      `Unknown tool id(s) in allowedToolIds: ${unknown.join(', ')}. ` +
        `Known: ${Object.keys(PLATFORM_TOOL_CATALOGUE).join(', ')}. ` +
        `Add the tool to PLATFORM_TOOL_CATALOGUE first.`,
    );
  }
  const subset = allowedToolIds.map((id) => PLATFORM_TOOL_CATALOGUE[id]);
  const deprecated = subset.filter((s) => s.approvalStatus === 'deprecated');
  if (deprecated.length > 0) {
    throw new Error(
      `Cannot subscribe to deprecated tool(s): ${deprecated.map((t) => t.toolId).join(', ')}. ` +
        `Remove from allowedToolIds or unmark as deprecated in PLATFORM_TOOL_CATALOGUE.`,
    );
  }
  return subset;
}

/**
 * Resolve a ToolSpec's targetArn into a concrete ARN by substituting
 * ${PLATFORM_ACCOUNT_ID} when the tool lives in the platform account
 * (targetAccountId is undefined). Cross-account tools use their explicit
 * account id literally.
 */
export function resolveTargetArn(spec: ToolSpec, platformAccountId: string): string {
  const acct = spec.targetAccountId ?? platformAccountId;
  return spec.targetArn.replace('${PLATFORM_ACCOUNT_ID}', acct);
}

/**
 * Compose the Cedar policy document for a subscription — union of
 * per-tool cedarPolicy strings plus a default deny. Per Q3.
 *
 * Phase Q (v0.6.0): when a ToolSpec carries `allowedGroups`, the composed
 * bundle replaces the tool's bare `permit` with one principal-bound permit
 * per Cognito group, so the Cedar evaluator can deny callers whose JWT
 * `cognito:groups` claim does not intersect the allow list. Tools without
 * allowedGroups keep their author-supplied cedarPolicy verbatim — this
 * preserves the v0.5.0 default of "any authenticated principal" for
 * back-compat.
 */
export function composeCedarPolicyDocument(subset: readonly ToolSpec[]): string {
  const parts = subset.map((s) => {
    if (s.allowedGroups && s.allowedGroups.length > 0) {
      const permits = s.allowedGroups
        .map(
          (g) =>
            `permit(principal in CognitoGroup::"${g}", action == Action::"InvokeTool", resource == Tool::"${s.toolId}");`,
        )
        .join('\n');
      return (
        `// Tool: ${s.toolId} (owner: ${s.ownerTeam})\n` +
        `// Q-entitlement: principal-bound; only members of [${s.allowedGroups.join(', ')}] may invoke.\n` +
        permits
      );
    }
    return `// Tool: ${s.toolId} (owner: ${s.ownerTeam})\n${s.cedarPolicy.trim()}`;
  });
  // Default forbid — belt-and-braces; Gateway's authorizer is permit-only in practice
  parts.push(
    '// Default forbid — everything not explicitly permitted above\nforbid(principal, action, resource) unless { principal has allowed && resource has allowed };',
  );
  return parts.join('\n\n');
}
