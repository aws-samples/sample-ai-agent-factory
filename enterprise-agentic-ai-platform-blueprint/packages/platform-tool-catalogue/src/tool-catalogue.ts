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

import { createHash } from "node:crypto";

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
export type ToolType = "lambda" | "agent-a2a";

export interface ToolSpec {
  readonly toolId: ToolId; // unique within catalogue; kebab-case pattern
  /** Z7-K: kind of tool. Defaults to 'lambda'. 'agent-a2a' targets a peer-agent A2A endpoint. */
  readonly toolType?: ToolType;
  /** Lambda alias ARN. Platform-owned targets use both `${PLATFORM_REGION}` and `${PLATFORM_ACCOUNT_ID}` placeholders. */
  readonly targetArn: string;
  readonly targetAccountId?: string; // optional; when present the tool lives cross-account (platform-workload or workload-workload)
  readonly cedarPolicy: string; // per-tool Cedar snippet, union-ed into Gateway policy at synth
  readonly ownerTeam: string; // e.g. 'platform-ai', 'retail', 'hr'
  readonly costCentre: string; // CUR attribution passthrough
  readonly description: string; // human-readable; appears in Registry DDB
  readonly approvalStatus: "approved" | "experimental" | "deprecated";
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
  /**
   * Round 3 — per-developer entitlement: the set of stable JWT `sub` values
   * permitted to invoke this tool. Empty/undefined ⇒ no subject-scoping.
   * When present, the composed Cedar bundle adds one
   * `permit(principal == Developer::"<sub>", ...)` per subject, which the
   * wrapper evaluates by exact `sub` equality. May be combined with
   * `allowedGroups` (either a matching group OR a matching subject permits);
   * when either is present the workstream Gateway must run in CUSTOM_JWT mode
   * because AWS_IAM has no JWT claims to evaluate.
   */
  readonly allowedSubjects?: readonly string[];
}

/** JWT `sub` values: opaque, printable, bounded. */
const SUBJECT_REGEX = /^[A-Za-z0-9._:@|-]{1,255}$/;

/** Cognito group names: lower-case kebab/snake, max 128 chars (AWS limit). */
const COGNITO_GROUP_REGEX = /^[A-Za-z0-9_+=,.@-]{1,128}$/;

/** The authoritative catalogue. Append-only in v1; version bumps via PR. */
export const PLATFORM_TOOL_CATALOGUE: Readonly<Record<ToolId, ToolSpec>> = {
  // Two demo tools to start — platform-owned, approved.
  "tool-echo": {
    toolId: "tool-echo",
    targetArn:
      "arn:aws:lambda:${PLATFORM_REGION}:${PLATFORM_ACCOUNT_ID}:function:agenticai-d03-tool-echo:PROD",
    cedarPolicy:
      'permit(principal, action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    ownerTeam: "platform-ai",
    costCentre: "platform",
    description: "Echoes the input string. Canonical health-check tool.",
    approvalStatus: "approved",
    inputSchema: {
      type: "object",
      properties: { message: { type: "string" } },
      required: ["message"],
    },
  },
  "tool-ping": {
    toolId: "tool-ping",
    targetArn:
      "arn:aws:lambda:${PLATFORM_REGION}:${PLATFORM_ACCOUNT_ID}:function:agenticai-d03-tool-ping:PROD",
    cedarPolicy:
      'permit(principal, action == Action::"InvokeTool", resource == Tool::"tool-ping");',
    ownerTeam: "platform-ai",
    costCentre: "platform",
    description:
      "Returns pong + timestamp + caller principal. Observability probe.",
    approvalStatus: "approved",
    inputSchema: { type: "object", properties: {} },
  },
};

export const PLATFORM_TOOL_CATALOGUE_VERSION = "2";

// Pattern matchers:
const LAMBDA_ARN_WITH_ALIAS =
  /^arn:aws:lambda:(?:\$\{PLATFORM_REGION\}|[a-z0-9-]+):(?:\$\{PLATFORM_ACCOUNT_ID\}|\d{12}):function:[a-zA-Z0-9-_]+:[a-zA-Z0-9-_$]+$/;

/** Validate a ToolSpec at synth; throws with actionable message. */
export function validateToolSpec(spec: ToolSpec): void {
  if (!/^[a-z0-9-]{3,50}$/.test(spec.toolId)) {
    throw new Error(`ToolId must be kebab-case 3-50 chars: ${spec.toolId}`);
  }
  const toolType: ToolType = spec.toolType ?? "lambda";
  if (toolType === "lambda") {
    if (!LAMBDA_ARN_WITH_ALIAS.test(spec.targetArn)) {
      throw new Error(
        `Tool ${spec.toolId}: targetArn MUST end with a Lambda alias (Q5 pin-via-alias). Got: ${spec.targetArn}`,
      );
    }
    if (spec.a2aEndpointUrl) {
      throw new Error(
        `Tool ${spec.toolId}: a2aEndpointUrl is only valid when toolType='agent-a2a'`,
      );
    }
  } else if (toolType === "agent-a2a") {
    if (!spec.a2aEndpointUrl || !/^https:\/\//.test(spec.a2aEndpointUrl)) {
      throw new Error(
        `Tool ${spec.toolId}: agent-a2a tools require a2aEndpointUrl starting with https://`,
      );
    }
  } else {
    throw new Error(`Tool ${spec.toolId}: unsupported toolType ${toolType}`);
  }
  if (spec.targetAccountId && !/^\d{12}$/.test(spec.targetAccountId)) {
    throw new Error(`Tool ${spec.toolId}: targetAccountId must be 12 digits`);
  }
  if (!spec.cedarPolicy.includes("permit")) {
    throw new Error(`Tool ${spec.toolId}: cedarPolicy must contain permit()`);
  }
  if (
    !["approved", "experimental", "deprecated"].includes(spec.approvalStatus)
  ) {
    throw new Error(`Tool ${spec.toolId}: approvalStatus invalid`);
  }
  if (spec.allowedGroups !== undefined) {
    if (!Array.isArray(spec.allowedGroups) || spec.allowedGroups.length === 0) {
      throw new Error(
        `Tool ${spec.toolId}: allowedGroups, when present, must be a non-empty array of Cognito group names`,
      );
    }
    for (const g of spec.allowedGroups) {
      if (typeof g !== "string" || !COGNITO_GROUP_REGEX.test(g)) {
        throw new Error(
          `Tool ${spec.toolId}: allowedGroups entry '${g}' is not a valid Cognito group name`,
        );
      }
    }
  }
  if (spec.allowedSubjects !== undefined) {
    if (
      !Array.isArray(spec.allowedSubjects) ||
      spec.allowedSubjects.length === 0
    ) {
      throw new Error(
        `Tool ${spec.toolId}: allowedSubjects, when present, must be a non-empty array of JWT sub values`,
      );
    }
    for (const s of spec.allowedSubjects) {
      if (typeof s !== "string" || !SUBJECT_REGEX.test(s)) {
        throw new Error(
          `Tool ${spec.toolId}: allowedSubjects entry '${s}' is not a valid JWT sub value`,
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
export function resolveSubscribedTools(
  allowedToolIds: readonly ToolId[],
): ToolSpec[] {
  const unknown = allowedToolIds.filter(
    (id) => !(id in PLATFORM_TOOL_CATALOGUE),
  );
  if (unknown.length > 0) {
    throw new Error(
      `Unknown tool id(s) in allowedToolIds: ${unknown.join(", ")}. ` +
        `Known: ${Object.keys(PLATFORM_TOOL_CATALOGUE).join(", ")}. ` +
        `Add the tool to PLATFORM_TOOL_CATALOGUE first.`,
    );
  }
  const subset = allowedToolIds.map((id) => PLATFORM_TOOL_CATALOGUE[id]);
  const deprecated = subset.filter((s) => s.approvalStatus === "deprecated");
  if (deprecated.length > 0) {
    throw new Error(
      `Cannot subscribe to deprecated tool(s): ${deprecated.map((t) => t.toolId).join(", ")}. ` +
        `Remove from allowedToolIds or unmark as deprecated in PLATFORM_TOOL_CATALOGUE.`,
    );
  }
  return subset;
}

/**
 * Resolve a ToolSpec's targetArn into a concrete ARN by substituting
 * `${PLATFORM_REGION}` and `${PLATFORM_ACCOUNT_ID}`. Platform-owned tools use
 * the supplied account; cross-account tools use their explicit account id.
 */
export function resolveTargetArn(
  spec: ToolSpec,
  platformAccountId: string,
  platformRegion: string,
): string {
  const acct = spec.targetAccountId ?? platformAccountId;
  return spec.targetArn
    .replace("${PLATFORM_REGION}", platformRegion)
    .replace("${PLATFORM_ACCOUNT_ID}", acct);
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
export function composeCedarPolicyDocument(
  subset: readonly ToolSpec[],
): string {
  const parts = subset.map((s) => {
    const hasGroups = !!(s.allowedGroups && s.allowedGroups.length > 0);
    const hasSubjects = !!(s.allowedSubjects && s.allowedSubjects.length > 0);
    if (hasGroups || hasSubjects) {
      const permits: string[] = [];
      if (hasGroups) {
        for (const g of s.allowedGroups!) {
          permits.push(
            `permit(principal in CognitoGroup::"${g}", action == Action::"InvokeTool", resource == Tool::"${s.toolId}");`,
          );
        }
      }
      if (hasSubjects) {
        for (const sub of s.allowedSubjects!) {
          permits.push(
            `permit(principal == Developer::"${sub}", action == Action::"InvokeTool", resource == Tool::"${s.toolId}");`,
          );
        }
      }
      const scopeNote = [
        hasGroups ? `groups [${s.allowedGroups!.join(", ")}]` : "",
        hasSubjects ? `subjects [${s.allowedSubjects!.length} sub(s)]` : "",
      ]
        .filter(Boolean)
        .join(" or ");
      return (
        `// Tool: ${s.toolId} (owner: ${s.ownerTeam})\n` +
        `// entitlement: principal-bound; only ${scopeNote} may invoke.\n` +
        permits.join("\n")
      );
    }
    return `// Tool: ${s.toolId} (owner: ${s.ownerTeam})\n${s.cedarPolicy.trim()}`;
  });
  // Default forbid — belt-and-braces; Gateway's authorizer is permit-only in practice
  parts.push(
    "// Default forbid — everything not explicitly permitted above\nforbid(principal, action, resource) unless { principal has allowed && resource has allowed };",
  );
  return parts.join("\n\n");
}

export type AgentCoreGatewayAuthorizerType = "AWS_IAM" | "CUSTOM_JWT";

export interface AgentCorePolicyEngineOptions {
  readonly authorizerType: AgentCoreGatewayAuthorizerType;
  readonly gatewayArn: string;
  readonly policyNamePrefix: string;
  readonly targetNames: Readonly<Record<string, string>>;
  /** Exact IAM role ARNs permitted to call an AWS_IAM Gateway. */
  readonly iamRoleArns?: readonly string[];
}

export interface AgentCorePolicyDefinition {
  readonly policyName: string;
  readonly toolId: string;
  readonly statement: string;
}

const AGENTCORE_POLICY_NAME = /^[A-Za-z][A-Za-z0-9_]{0,47}$/;
const AGENTCORE_TARGET_NAME = /^[0-9A-Za-z][0-9A-Za-z-]{0,99}$/;
const IAM_ROLE_ARN =
  /^arn:(aws|aws-us-gov|aws-cn):iam::(\d{12}):role\/([A-Za-z0-9+=,.@_-]{1,64})$/;

function agentCorePolicyName(raw: string): string {
  let normalized = raw.replace(/[^A-Za-z0-9_]/g, "_");
  if (!/^[A-Za-z]/.test(normalized)) normalized = `P_${normalized}`;
  if (normalized.length > 48) {
    const digest = createHash("sha256")
      .update(raw, "utf8")
      .digest("hex")
      .slice(0, 8);
    normalized = `${normalized.slice(0, 39)}_${digest}`;
  }
  if (!AGENTCORE_POLICY_NAME.test(normalized)) {
    throw new Error(
      `AgentCore Policy name '${normalized}' must match ${AGENTCORE_POLICY_NAME.source}.`,
    );
  }
  return normalized;
}

function cedarString(value: string, label: string): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    /["\\\r\n]/.test(value)
  ) {
    throw new Error(`${label} contains a character that is unsafe in Cedar.`);
  }
  return value;
}

function assumedRolePrincipal(roleArn: string): string {
  const match = IAM_ROLE_ARN.exec(roleArn);
  if (!match) {
    throw new Error(
      `PolicyEngine IAM principal '${roleArn}' must be an exact pathless IAM role ARN.`,
    );
  }
  const [, partition, accountId, roleName] = match;
  return `arn:${partition}:sts::${accountId}:assumed-role/${roleName}`;
}

function agentCorePermit(
  principal: string,
  action: string,
  gatewayArn: string,
  condition?: string,
): string {
  const statement = [
    "permit(",
    `  ${principal},`,
    `  action == AgentCore::Action::"${action}",`,
    `  resource == AgentCore::Gateway::"${gatewayArn}"`,
    ")",
  ];
  if (condition) {
    statement.push(`when { ${condition} };`);
  } else {
    statement[statement.length - 1] += ";";
  }
  return statement.join("\n");
}

/**
 * Compile one strict AgentCore Policy definition per subscribed tool.
 *
 * Policy in AgentCore uses a service schema that is intentionally different
 * from the legacy Lambda-wrapper grammar: principals are `IamEntity` or
 * `OAuthUser`, actions are `<TargetName>___<ToolName>`, and resources are an
 * exact Gateway ARN. Default deny is provided by the PolicyEngine itself, so
 * this compiler emits permits only and never a catch-all forbid that would
 * override them.
 */
export function composeAgentCorePolicyDefinitions(
  subset: readonly ToolSpec[],
  options: AgentCorePolicyEngineOptions,
): readonly AgentCorePolicyDefinition[] {
  if (subset.length === 0) {
    throw new Error("PolicyEngine requires at least one subscribed tool.");
  }
  const gatewayArn = cedarString(options.gatewayArn, "Gateway ARN");
  if (gatewayArn.includes("*")) {
    throw new Error("PolicyEngine Gateway ARN must not contain a wildcard.");
  }

  const configuredRoleArns = options.iamRoleArns ?? [];
  if (new Set(configuredRoleArns).size !== configuredRoleArns.length) {
    throw new Error("PolicyEngine IAM role ARNs must not contain duplicates.");
  }
  const roleArns = [...configuredRoleArns].sort();
  if (options.authorizerType === "AWS_IAM" && roleArns.length === 0) {
    throw new Error(
      "AWS_IAM PolicyEngine mode requires at least one exact IAM role ARN.",
    );
  }
  if (options.authorizerType === "CUSTOM_JWT" && roleArns.length > 0) {
    throw new Error(
      "CUSTOM_JWT PolicyEngine mode must not carry IAM role principals.",
    );
  }
  const iamPrincipals = roleArns.map(assumedRolePrincipal);

  return [...subset]
    .sort((left, right) => left.toolId.localeCompare(right.toolId))
    .map((tool) => {
      validateToolSpec(tool);
      const targetName = options.targetNames[tool.toolId];
      if (!targetName || !AGENTCORE_TARGET_NAME.test(targetName)) {
        throw new Error(
          `PolicyEngine target name for '${tool.toolId}' is absent or invalid.`,
        );
      }
      const action = cedarString(
        `${targetName}___${tool.toolId}`,
        `PolicyEngine action for '${tool.toolId}'`,
      );
      let statements: string[];

      if (options.authorizerType === "AWS_IAM") {
        if (tool.allowedGroups && tool.allowedGroups.length > 0) {
          throw new Error(
            `Tool '${tool.toolId}' has allowedGroups and therefore requires CUSTOM_JWT PolicyEngine mode.`,
          );
        }
        statements = iamPrincipals.map((principal) =>
          agentCorePermit(
            `principal == AgentCore::IamEntity::"${principal}"`,
            action,
            gatewayArn,
          ),
        );
      } else if (tool.allowedGroups && tool.allowedGroups.length > 0) {
        const groupConditions = tool.allowedGroups.map((group) => {
          cedarString(group, `Cognito group for '${tool.toolId}'`);
          // AgentCore exposes this list-valued claim as JSON array text. The
          // isolated PolicyEngine campaign live-proved quote-delimited element
          // matching; do not weaken this to a bare substring, which lets one
          // group name collide with another. See evidence/live/2026-09-19-
          // policyengine-compatibility-spike.md.
          const slash = String.fromCharCode(92);
          const quotedGroup = `${slash}"${group}${slash}"`;
          return (
            `(principal.hasTag("cognito:groups") && ` +
            `principal.getTag("cognito:groups") like "*${quotedGroup}*")`
          );
        });
        statements = [
          agentCorePermit(
            "principal is AgentCore::OAuthUser",
            action,
            gatewayArn,
            groupConditions.join(" || "),
          ),
        ];
      } else {
        statements = [
          agentCorePermit(
            "principal is AgentCore::OAuthUser",
            action,
            gatewayArn,
          ),
        ];
      }

      return {
        policyName: agentCorePolicyName(
          `${options.policyNamePrefix}_${tool.toolId}`,
        ),
        toolId: tool.toolId,
        statement: statements.join("\n\n"),
      };
    });
}
