/**
 * @agenticai/organizations
 *
 * AWS Organizations + OU hierarchy + Service Control Policies 01-08.
 *
 * Spec references:
 *   - §2.1.2 OU hierarchy (lines 313-328)
 *   - §2.2 Service Control Policies (lines 565-1013) — SCPs 01-08
 *
 * Why L1 (CfnOrganization / CfnOrganizationalUnit / CfnPolicy)?
 *   CDK has no L2 construct library for AWS Organizations at the time of
 *   writing. AFT is Terraform-only and is rejected by D-02.
 *
 * Sandbox-first attachment strategy (per R-SCP-018):
 *   The `OrganizationConstruct` attaches SCPs to the Sandbox OU only. Once
 *   the four canonical denial tests pass in sandbox
 *   (`bash scripts/scp-sandbox-soak.sh`), a follow-up PR promotes the
 *   attachment to the AgenticAI-Workloads OU.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export { OrganizationConstruct, type OrganizationConstructProps } from './organization-construct';
export { buildScpSet, type ScpDefinition } from './scps';
