/**
 * OrgStack — deployed to the AWS Organization management account.
 *
 * Creates the OU hierarchy + SCPs 01-08 via OrganizationConstruct. Sandbox-first:
 * SCPs are attached only to the Sandbox OU until the canonical denial tests
 * pass, then a follow-up deploy with `attachToWorkloadsOu: true` promotes
 * them.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';

import { OrganizationConstruct } from '@agenticai/organizations';

export interface OrgStackProps extends StackProps {
  /**
   * Platform Guardrail Admin role ARN. Enforced by SCP-05 (only this role may
   * create/update/delete Bedrock Guardrails). Pass the role ARN once the
   * platform-nonprod account and GuardrailAdminStack exist (Phase 3).
   */
  readonly platformGuardrailAdminRoleArn: string;

  /**
   * Whether to attach SCPs to AgenticAI-Workloads OU. Defaults to false
   * (sandbox-first). Set true after the Phase 1 soak tests pass.
   */
  readonly attachToWorkloadsOu?: boolean;

  /** Import existing Organization (e.g. when Control Tower owns it). */
  readonly importExistingOrganization?: boolean;

  /** Existing Organization root id (required when importExistingOrganization). */
  readonly existingRootId?: string;
}

export class OrgStack extends Stack {
  readonly organization: OrganizationConstruct;

  constructor(scope: Construct, id: string, props: OrgStackProps) {
    super(scope, id, props);

    this.organization = new OrganizationConstruct(this, 'Organization', {
      platformGuardrailAdminRoleArn: props.platformGuardrailAdminRoleArn,
      attachToWorkloadsOu: props.attachToWorkloadsOu ?? false,
      importExistingOrganization: props.importExistingOrganization,
      existingRootId: props.existingRootId,
      primaryRegion: this.region,
    });
  }
}
