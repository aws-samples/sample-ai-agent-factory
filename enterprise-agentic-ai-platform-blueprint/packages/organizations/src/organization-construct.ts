/**
 * OrganizationConstruct
 *
 * Emits the AWS Organization, OU hierarchy (Security, Shared Services,
 * AgenticAI-Platform, AgenticAI-Workloads, Sandbox), and SCPs 01-08.
 *
 * SCP ATTACHMENT STRATEGY (Phase 1, sandbox-first):
 *   - Sandbox OU:          all 8 SCPs attached (soak before promotion).
 *   - AgenticAI-Workloads: attached iff `attachToWorkloadsOu: true`.
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
  PLATFORM_ALLOWED_MODELS,
  PLATFORM_APPROVED_REGIONS,
  allowedModelArns,
} from '@agenticai/platform-baselines';

import { buildScpSet, type ScpDefinition, SCP_BODY_SOFT_LIMIT, SCP_BODY_HARD_LIMIT } from './scps';

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
   * Approved Bedrock Guardrail identifiers (ids or ARNs) passed into SCP-02's
   * positive allow-list check. Wire from Phase 3's GuardrailStack outputs.
   * If omitted, SCP-02 falls back to the Null-only gate (present but empty
   * GuardrailIdentifier values can bypass) and emits a synth-time warning.
   */
  readonly approvedGuardrailIds?: readonly string[];
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
    this.ous = {
      security: new CfnOrganizationalUnit(this, 'SecurityOu', {
        name: 'Security',
        parentId: rootId,
      }),
      sharedServices: new CfnOrganizationalUnit(this, 'SharedServicesOu', {
        name: 'SharedServices',
        parentId: rootId,
      }),
      agenticaiPlatform: new CfnOrganizationalUnit(this, 'AgenticAiPlatformOu', {
        name: 'AgenticAI-Platform',
        parentId: rootId,
      }),
      agenticaiWorkloads: new CfnOrganizationalUnit(this, 'AgenticAiWorkloadsOu', {
        name: 'AgenticAI-Workloads',
        parentId: rootId,
      }),
      sandbox: new CfnOrganizationalUnit(this, 'SandboxOu', {
        name: 'AgenticAI-Sandbox',
        parentId: rootId,
      }),
    };

    // ------ SCPs 01-08 ------
    const scpDefinitions: readonly ScpDefinition[] = buildScpSet({
      allowedModelArns: allowedModelArns(primaryRegion),
      approvedRegions: PLATFORM_APPROVED_REGIONS,
      platformGuardrailAdminRoleArn: props.platformGuardrailAdminRoleArn,
      approvedGuardrailIds: props.approvedGuardrailIds,
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

    // Rough sanity: verify we produced exactly 8 SCPs.
    if (this.scps.size !== 8) {
      throw new Error(
        `OrganizationConstruct produced ${this.scps.size} SCPs; expected 8 per spec §2.2 SCPs 01-08.`,
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
