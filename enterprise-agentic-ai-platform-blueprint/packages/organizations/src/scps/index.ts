/**
 * Service Control Policy bodies — SCPs 01 through 10.
 *
 * Each SCP is emitted as a TypeScript function returning the inline JSON policy
 * body. The functions reference the shared platform-baselines constants so a
 * change to the model allow-list or approved-region list propagates to SCP-01
 * and SCP-06 without duplication. Conformance tests
 * (`tests/conformance/model-allowlist-ssot.test.ts` in Phase 5) assert that
 * the LiteLLM router config, Bedrock VPCE policy, and IAM resource scopes
 * agree with these SCP bodies.
 *
 * SCPs 09 and 10 are the D-03 Gateway-governance additions:
 *   - SCP-09 locks down AgentCore Gateway mutation to the platform admin role.
 *   - SCP-10 restricts Lambda invocations from D-03 runtime roles to the set
 *     of catalogued tool target ARNs.
 * SCPs 03, 04 and 09-12 are conditionally emitted — callers wire them through
 * `buildScpSet` options; if the inputs are absent a synth-time warning is
 * logged. SCP-03/04 are VPC-mode controls and need explicit endpoint ids.
 * Organizations allows at most 10 SCPs per target including FullAWSAccess
 * (`SCP_MAX_ATTACHED_PER_TARGET`); the full twelve cannot share one OU.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

import { scp01ModelAllowlist } from "./scp-01-model-allowlist";
import { scp02EnforceGuardrail } from "./scp-02-enforce-guardrail";
import { scp03EnforceAgentCoreVpce } from "./scp-03-enforce-agentcore-vpce";
import { scp04EnforceBedrockVpce } from "./scp-04-enforce-bedrock-vpce";
import { scp05DenyGuardrailModification } from "./scp-05-deny-guardrail-modification";
import { scp06RestrictRegions } from "./scp-06-restrict-regions";
import { scp07DenyPublicAgentCore } from "./scp-07-deny-public-agentcore";
import { scp08DenyEcrPublic } from "./scp-08-deny-ecr-public";
import { scp09GatewayMutationLockdown } from "./scp-09-gateway-mutation-lockdown";
import { scp10ToolInvokeAllowlist } from "./scp-10-tool-invoke-allowlist";
import { scp11RegistryMutationLockdown } from "./scp-11-registry-mutation-lockdown";
import { scp12DeveloperPlatformTagDeny } from "./scp-12-developer-platform-tag-deny";

/**
 * A single SCP definition: stable id, display name, rendered JSON body.
 *
 * `body` is a plain JS object to match `CfnPolicy.content` (which expects an
 * object, not a string). `bodyJson` is the same object serialised so callers
 * can do size checks, snapshots, or diffs without re-stringifying.
 */
export interface ScpDefinition {
  /** Short id, e.g. 'scp-01'. Used for CFN logical-id derivation. */
  readonly id: string;
  /** Human-readable policy name. */
  readonly name: string;
  /** Paragraph describing what the SCP enforces. */
  readonly description: string;
  /** Policy body as a plain object, for passing to CfnPolicy.content. */
  readonly body: Record<string, unknown>;
  /** Same body serialised to JSON, for size checks and snapshot tests. */
  readonly bodyJson: string;
}

/**
 * Helper used by individual SCP modules to package the (id, name, description, body) tuple
 * into an ScpDefinition.
 */
export function toScpDefinition(
  id: string,
  name: string,
  description: string,
  body: Record<string, unknown>,
): ScpDefinition {
  return {
    id,
    name,
    description,
    body,
    bodyJson: JSON.stringify(body),
  };
}

/**
 * Build the canonical set of 8 SCPs.
 *
 * `platformGuardrailAdminRoleArn` is passed into SCP-05 so that only the named
 * role may modify Bedrock Guardrails. The caller (OrganizationConstruct)
 * typically derives this from `Fn.sub` at deploy time — until the platform
 * account exists, a placeholder may be used in unit tests.
 */
export interface BuildScpSetOptions {
  readonly allowedModelArns: readonly string[];
  readonly approvedRegions: readonly string[];
  readonly platformGuardrailAdminRoleArn: string;
  /**
   * Approved Bedrock Guardrail identifiers for SCP-02's allow-list. Passed
   * through to `scp02EnforceGuardrail`. When omitted the SCP falls back to
   * the Null-only gate and emits a synth-time warning.
   */
  readonly approvedGuardrailIds?: readonly string[];
  /**
   * Workstream accounts that host pipeline-created
   * `AgenticAI-D03-*-GatewayAdmin` roles. Required to emit SCP-09.
   */
  readonly gatewayAdminWorkloadAccountIds?: readonly string[];
  /**
   * Platform account id used by Registry SCP-11.
   */
  readonly platformAccountId?: string;
  /**
   * Fully-resolved Lambda target ARNs from PLATFORM_TOOL_CATALOGUE — required
   * by SCP-10. When omitted or empty, SCP-10 is skipped and a synth-time
   * warning is emitted.
   */
  readonly allowedToolTargetArns?: readonly string[];
  /**
   * When set, SCP-11 (Registry mutation lockdown) is emitted with the
   * platform account as the only principal allowed to mutate the AgentCore
   * Registry. Defaults to the same `platformAccountId` as SCP-09 when both
   * are wired by the caller. v0.5.0+.
   */
  readonly enableRegistryLockdown?: boolean;
  /**
   * When `true`, SCP-12 (Developer permission-set / platform-tag deny) is
   * emitted. Requires the workstream Identity Center permission sets to be
   * deployed (Phase M of v0.5.0). v0.5.0+.
   */
  readonly enableDeveloperPlatformTagDeny?: boolean;
  /**
   * Override the default RegistryAdmin role name used by SCP-11. Default
   * `AgenticAI-RegistryAdmin`. Test fixtures only.
   */
  readonly registryAdminRoleName?: string;
  /**
   * Override the default developer permission-set prefix used by SCP-12.
   * Default `AgenticAI-WS-Dev-`. Test fixtures only.
   */
  readonly developerPermissionSetPrefix?: string;
  /**
   * Exact AgentCore interface VPC endpoint ids for SCP-03. SCP-03 is emitted
   * only when supplied: it is a VPC-mode control, and attaching it to an OU
   * whose Runtimes run `networkMode: PUBLIC` denies their own AgentCore
   * calls (live IAM evaluator, 2026-09-25).
   */
  readonly approvedAgentCoreVpceIds?: readonly string[];
  /** Exact Bedrock Runtime VPC endpoint ids for SCP-04 (same rule as SCP-03). */
  readonly approvedBedrockVpceIds?: readonly string[];
  /** Platform pipeline role-name patterns exempted by SCP-11 (default: CDK cfn-exec role). */
  readonly registryPipelineRoleNamePatterns?: readonly string[];
}

export function buildScpSet(
  opts: BuildScpSetOptions,
): readonly ScpDefinition[] {
  const set: ScpDefinition[] = [
    scp01ModelAllowlist(opts.allowedModelArns),
    scp02EnforceGuardrail({ approvedGuardrailIds: opts.approvedGuardrailIds }),
  ];
  if (opts.approvedAgentCoreVpceIds && opts.approvedAgentCoreVpceIds.length > 0) {
    set.push(scp03EnforceAgentCoreVpce(opts.approvedAgentCoreVpceIds));
  } else {
    // eslint-disable-next-line no-console
    console.warn(
      "SCP-03: approvedAgentCoreVpceIds was not supplied. Skipping the AgentCore VPCE lockdown — " +
        "it is a VPC-mode control and would deny PUBLIC-network Runtimes' own calls. [TODO-AGENTCORE-VPCE-IDS]",
    );
  }
  if (opts.approvedBedrockVpceIds && opts.approvedBedrockVpceIds.length > 0) {
    set.push(scp04EnforceBedrockVpce(opts.approvedBedrockVpceIds));
  } else {
    // eslint-disable-next-line no-console
    console.warn(
      "SCP-04: approvedBedrockVpceIds was not supplied. Skipping the Bedrock VPCE lockdown — " +
        "it is a VPC-mode control. [TODO-BEDROCK-VPCE-IDS]",
    );
  }
  set.push(
    scp05DenyGuardrailModification(opts.platformGuardrailAdminRoleArn),
    scp06RestrictRegions(opts.approvedRegions),
  );
  if (opts.approvedAgentCoreVpceIds && opts.approvedAgentCoreVpceIds.length > 0) {
    // SCP-07 (Runtimes must be VPC-attached) belongs to the same VPC-mode
    // posture as SCP-03: the reference Runtime runs networkMode PUBLIC, and
    // SCP-07 would deny the pipeline's own Create/UpdateAgentRuntime.
    set.push(scp07DenyPublicAgentCore());
  } else {
    // eslint-disable-next-line no-console
    console.warn(
      "SCP-07: no VPC-mode AgentCore endpoint ids were supplied. Skipping the VPC-only Runtime rule — " +
        "it would deny PUBLIC-network Runtime deployments. [TODO-AGENTCORE-VPCE-IDS]",
    );
  }
  set.push(scp08DenyEcrPublic());

  if (
    opts.gatewayAdminWorkloadAccountIds &&
    opts.gatewayAdminWorkloadAccountIds.length > 0
  ) {
    set.push(
      scp09GatewayMutationLockdown({
        workloadAccountIds: opts.gatewayAdminWorkloadAccountIds,
      }),
    );
  } else {
    // eslint-disable-next-line no-console
    console.warn(
      "SCP-09: gatewayAdminWorkloadAccountIds was not supplied. Skipping Gateway-mutation lockdown — " +
        "workstream principals will not be blocked from mutating AgentCore Gateways. " +
        "Wire the exact Workstream account IDs before promotion. " +
        "[TODO-WORKSTREAM-GATEWAY-ADMIN-ACCOUNTS]",
    );
  }

  if (opts.allowedToolTargetArns && opts.allowedToolTargetArns.length > 0) {
    set.push(
      scp10ToolInvokeAllowlist({
        allowedToolTargetArns: opts.allowedToolTargetArns,
      }),
    );
  } else {
    // eslint-disable-next-line no-console
    console.warn(
      "SCP-10: allowedToolTargetArns was empty. Skipping tool-invoke allow-list — " +
        "D-03 runtime roles will not be restricted to catalogued Lambdas at the SCP layer. " +
        "Wire the resolved ARNs from PLATFORM_TOOL_CATALOGUE.resolveSubscribedTools() " +
        "before promoting to AgenticAI-Workloads. [TODO-TOOL-CATALOGUE]",
    );
  }

  if (opts.enableRegistryLockdown && opts.platformAccountId) {
    set.push(
      scp11RegistryMutationLockdown({
        platformAccountId: opts.platformAccountId,
        registryAdminRoleName: opts.registryAdminRoleName,
        pipelineRoleNamePatterns: opts.registryPipelineRoleNamePatterns,
      }),
    );
  } else if (opts.enableRegistryLockdown && !opts.platformAccountId) {
    // eslint-disable-next-line no-console
    console.warn(
      "SCP-11: enableRegistryLockdown=true but platformAccountId is missing. " +
        "Skipping Registry-mutation lockdown — workstream principals will not be " +
        "blocked from mutating the AgentCore Registry. [TODO-PLATFORM-ACCOUNT-ID]",
    );
  }

  if (opts.enableDeveloperPlatformTagDeny) {
    set.push(
      scp12DeveloperPlatformTagDeny({
        developerPermissionSetPrefix: opts.developerPermissionSetPrefix,
      }),
    );
  }

  return set;
}

/**
 * AWS Organizations hard limit on SCP body size.
 * Spec §2.2.11 L995 calls this out explicitly (`R-SCP-017`).
 */
export const SCP_BODY_HARD_LIMIT = 5120;

/**
 * AWS Organizations hard limit on SCPs directly attached to one root, OU or
 * account (10), which includes the mandatory `FullAWSAccess` policy. A
 * construct that attaches this set must stay at or below
 * `SCP_MAX_ATTACHED_PER_TARGET - 1` of its own policies per target.
 */
export const SCP_MAX_ATTACHED_PER_TARGET = 10;

/**
 * Our build-time soft limit. 120-char headroom lets the allow-list grow
 * without bumping up against the hard limit.
 */
export const SCP_BODY_SOFT_LIMIT = 5000;
