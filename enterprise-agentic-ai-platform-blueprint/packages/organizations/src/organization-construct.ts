/**
 * OrganizationConstruct
 *
 * Emits the AWS Organization, OU hierarchy (Security, Shared Services,
 * AgenticAI-Platform, AgenticAI-Workloads, Sandbox), and SCPs 01-08
 * (SCP-03/04 only when explicit VPC endpoint ids are supplied — they are
 * VPC-mode controls).
 *
 * SCP ATTACHMENT STRATEGY (Phase 1, sandbox-first):
 *   - Sandbox OU:          every emitted SCP attached (soak before promotion).
 *   - AgenticAI-Workloads: attached iff `attachToWorkloadsOu: true`.
 * AWS Organizations allows at most 10 SCPs per target including
 * FullAWSAccess; synth fails if the emitted set would exceed it.
 *
 * The soak runs the four canonical denial tests against a real account under
 * Sandbox OU. Once those pass, a follow-up deployment with
 * `attachToWorkloadsOu: true` promotes the SCPs to the Workloads OU.
 *
 * ASSUMPTION: if the AWS Organization already exists, the caller should
 * construct `OrganizationConstruct` with `importExistingOrganization: true`
 * and `existingRootId`. Otherwise a new Organization is created (ALL
 * features enabled per spec §2.1.1).
 *
 * SCP SIZE ENFORCEMENT: every rendered body is checked against the 5000-char
 * soft limit (R-SCP-017). Violations fail synth.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Annotations } from 'aws-cdk-lib';
import {
  CfnOrganization,
  CfnOrganizationalUnit,
  CfnPolicy,
} from 'aws-cdk-lib/aws-organizations';
import { Construct } from 'constructs';

import {
  PLATFORM_APPROVED_REGIONS,
  allowedModelArns,
} from '@agenticai/platform-baselines';

import {
  buildScpSet,
  type ScpDefinition,
  SCP_BODY_SOFT_LIMIT,
  SCP_BODY_HARD_LIMIT,
  SCP_MAX_ATTACHED_PER_TARGET,
} from './scps';

/** The five OUs the construct creates directly under the root, by key. */
export type OrganizationalUnitKey =
  | 'security'
  | 'sharedServices'
  | 'agenticaiPlatform'
  | 'agenticaiWorkloads'
  | 'sandbox';

/** OU names from spec §2.1.2. Override per key with `organizationalUnitNames`. */
export const DEFAULT_ORGANIZATIONAL_UNIT_NAMES: Readonly<Record<OrganizationalUnitKey, string>> = {
  security: 'Security',
  sharedServices: 'SharedServices',
  agenticaiPlatform: 'AgenticAI-Platform',
  agenticaiWorkloads: 'AgenticAI-Workloads',
  sandbox: 'AgenticAI-Sandbox',
};

/** AWS Organizations caps an OU name at 128 characters. */
const OU_NAME_MAX_LENGTH = 128;

function resolveOrganizationalUnitNames(
  overrides: Partial<Record<OrganizationalUnitKey, string>> | undefined,
): Record<OrganizationalUnitKey, string> {
  const names: Record<OrganizationalUnitKey, string> = { ...DEFAULT_ORGANIZATIONAL_UNIT_NAMES };
  for (const [key, value] of Object.entries(overrides ?? {})) {
    if (value === undefined) continue;
    if (!Object.prototype.hasOwnProperty.call(DEFAULT_ORGANIZATIONAL_UNIT_NAMES, key)) {
      throw new Error(
        `organizationalUnitNames: unknown OU key '${key}' (expected one of ` +
          `${Object.keys(DEFAULT_ORGANIZATIONAL_UNIT_NAMES).join(', ')}).`,
      );
    }
    if (typeof value !== 'string' || value.trim().length === 0 || value.length > OU_NAME_MAX_LENGTH) {
      throw new Error(
        `organizationalUnitNames.${key} must be a non-empty string of at most ${OU_NAME_MAX_LENGTH} characters.`,
      );
    }
    names[key as OrganizationalUnitKey] = value;
  }
  if (new Set(Object.values(names)).size !== Object.keys(names).length) {
    throw new Error(
      'organizationalUnitNames must give the five OUs distinct names; sibling OUs cannot share one.',
    );
  }
  return names;
}

export interface OrganizationConstructProps {
  /**
   * Region used to render foundation-model ARNs in SCP-01 (the platform
   * allow-list). Defaults to 'us-west-2', the reference-deployment region.
   */
  readonly primaryRegion?: string;

  /**
   * Platform Guardrail Admin role ARN. Only this role may modify Bedrock
   * Guardrails (enforced by SCP-05). Resolved to the role created in
   * Phase 3 once the platform account exists.
   */
  readonly platformGuardrailAdminRoleArn: string;

  /**
   * Whether to attach SCPs to AgenticAI-Workloads OU in addition to the
   * Sandbox OU. Default false (sandbox-first; promote only after soak).
   */
  readonly attachToWorkloadsOu?: boolean;

  /**
   * If true, the construct skips creating `CfnOrganization`, assuming the
   * Organization already exists. Use when adopting into a pre-existing
   * Organization or when deploying alongside AWS Control Tower which owns
   * the Organization resource.
   */
  readonly importExistingOrganization?: boolean;

  /**
   * When `importExistingOrganization` is true, supply the existing root ID
   * (e.g. 'r-xxxx'). Used as the parent for the OUs we create.
   */
  readonly existingRootId?: string;

  /**
   * Names for the five OUs, keyed by OU; defaults to
   * `DEFAULT_ORGANIZATIONAL_UNIT_NAMES`. AWS Organizations rejects two sibling
   * OUs with one name, so override a key when the root already holds an OU of
   * the default name (AWS Control Tower's `Security` OU, for example) instead
   * of renaming or deleting the existing OU.
   */
  readonly organizationalUnitNames?: Partial<Record<OrganizationalUnitKey, string>>;

  /**
   * Approved Bedrock Guardrail identifiers (ids or ARNs) passed into SCP-02's
   * positive allow-list check. Wire from Phase 3's GuardrailStack outputs.
   * If omitted, SCP-02 falls back to the Null-only gate (present but empty
   * GuardrailIdentifier values can bypass) and emits a synth-time warning.
   */
  readonly approvedGuardrailIds?: readonly string[];

  /**
   * Exact AgentCore / Bedrock interface VPC endpoint ids. SCP-03 / SCP-04 are
   * emitted only when these are supplied: they are VPC-mode controls, and
   * attaching them to an OU whose workloads use public AgentCore/Bedrock
   * endpoints denies those workloads' own calls.
   */
  readonly approvedAgentCoreVpceIds?: readonly string[];
  readonly approvedBedrockVpceIds?: readonly string[];

  /**
   * Emit SCP-13 (CloudWatch Omni telemetry lockdown, ADR-15). Opt-in: it
   * spends one of the nine SCP slots a target can hold. Default false.
   */
  readonly enableOmniTelemetryLockdown?: boolean;
  /** Pipeline role-name patterns exempted by SCP-13 (default: the CDK cfn-exec role). */
  readonly omniPipelineRoleNamePatterns?: readonly string[];
  /** Exact break-glass role names exempted by SCP-13 (default none). */
  readonly omniBreakGlassRoleNames?: readonly string[];
}

export class OrganizationConstruct extends Construct {
  readonly organization?: CfnOrganization;
  readonly ous: Record<string, CfnOrganizationalUnit>;
  readonly scps: ReadonlyMap<string, CfnPolicy>;

  constructor(scope: Construct, id: string, props: OrganizationConstructProps) {
    super(scope, id);

    const primaryRegion = props.primaryRegion ?? 'us-west-2';

    // ------ Organization ------
    let rootId: string;
    if (props.importExistingOrganization) {
      if (!props.existingRootId) {
        throw new Error(
          'existingRootId is required when importExistingOrganization is true.',
        );
      }
      rootId = props.existingRootId;
    } else {
      this.organization = new CfnOrganization(this, 'Organization', {
        featureSet: 'ALL',
      });
      // CfnOrganization.RootId attribute gives us the root id for downstream OUs.
      rootId = this.organization.attrRootId;
    }

    // ------ OU hierarchy (spec §2.1.2) ------
    const ouNames = resolveOrganizationalUnitNames(props.organizationalUnitNames);
    this.ous = {
      security: new CfnOrganizationalUnit(this, 'SecurityOu', {
        name: ouNames.security,
        parentId: rootId,
      }),
      sharedServices: new CfnOrganizationalUnit(this, 'SharedServicesOu', {
        name: ouNames.sharedServices,
        parentId: rootId,
      }),
      agenticaiPlatform: new CfnOrganizationalUnit(this, 'AgenticAiPlatformOu', {
        name: ouNames.agenticaiPlatform,
        parentId: rootId,
      }),
      agenticaiWorkloads: new CfnOrganizationalUnit(this, 'AgenticAiWorkloadsOu', {
        name: ouNames.agenticaiWorkloads,
        parentId: rootId,
      }),
      sandbox: new CfnOrganizationalUnit(this, 'SandboxOu', {
        name: ouNames.sandbox,
        parentId: rootId,
      }),
    };

    // ------ SCPs 01-08 ------
    const scpDefinitions: readonly ScpDefinition[] = buildScpSet({
      allowedModelArns: allowedModelArns(primaryRegion),
      approvedRegions: PLATFORM_APPROVED_REGIONS,
      platformGuardrailAdminRoleArn: props.platformGuardrailAdminRoleArn,
      approvedGuardrailIds: props.approvedGuardrailIds,
      approvedAgentCoreVpceIds: props.approvedAgentCoreVpceIds,
      approvedBedrockVpceIds: props.approvedBedrockVpceIds,
      enableOmniTelemetryLockdown: props.enableOmniTelemetryLockdown,
      omniPipelineRoleNamePatterns: props.omniPipelineRoleNamePatterns,
      omniBreakGlassRoleNames: props.omniBreakGlassRoleNames,
    });

    const scpMap = new Map<string, CfnPolicy>();

    // Target OUs. Sandbox is always attached. Workloads gate on flag.
    const targetIds: string[] = [this.ous.sandbox.attrId];
    if (props.attachToWorkloadsOu) {
      targetIds.push(this.ous.agenticaiWorkloads.attrId);
    }

    for (const scp of scpDefinitions) {
      this.validateScpBodySize(scp);

      const cfnPolicy = new CfnPolicy(this, `Scp${scp.id.replace(/^scp-/, '')}`, {
        content: scp.body,
        description: scp.description,
        name: scp.name,
        type: 'SERVICE_CONTROL_POLICY',
        targetIds: [...targetIds],
      });

      scpMap.set(scp.id, cfnPolicy);
    }

    this.scps = scpMap;

    // AWS Organizations allows at most 10 SCPs directly attached per target,
    // including the mandatory FullAWSAccess policy. Fail synth instead of
    // failing the deployment.
    if (this.scps.size > SCP_MAX_ATTACHED_PER_TARGET - 1) {
      throw new Error(
        `OrganizationConstruct would attach ${this.scps.size} SCPs per target; AWS Organizations allows ` +
          `${SCP_MAX_ATTACHED_PER_TARGET - 1} plus FullAWSAccess.`,
      );
    }

    // Safety reminder if Workloads attachment is still opt-out.
    if (!props.attachToWorkloadsOu) {
      Annotations.of(this).addInfo(
        'SCPs attached to Sandbox OU only. Promote to AgenticAI-Workloads after the canonical soak tests pass (bash scripts/scp-sandbox-soak.sh).',
      );
    }
  }

  /** Enforce spec §2.2.11 L995 / R-SCP-017 at synth time. */
  private validateScpBodySize(scp: ScpDefinition): void {
    const len = scp.bodyJson.length;
    if (len > SCP_BODY_HARD_LIMIT) {
      throw new Error(
        `${scp.name} is ${len} chars — exceeds AWS hard limit of ${SCP_BODY_HARD_LIMIT} (R-SCP-017). Split the policy.`,
      );
    }
    if (len > SCP_BODY_SOFT_LIMIT) {
      Annotations.of(this).addWarning(
        `${scp.name} is ${len} chars — exceeds soft limit of ${SCP_BODY_SOFT_LIMIT}. Consider refactoring before the allow-list grows further.`,
      );
    }
  }
}
