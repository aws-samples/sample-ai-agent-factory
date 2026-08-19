/**
 * SCP-11 — AgentCore Registry Mutation Lockdown (D-03 v3 / v0.5.0).
 *
 * Mirrors SCP-09 but for the AWS Bedrock AgentCore Registry control plane.
 *
 * Under v0.5.0 the platform-account hosts the org-wide AgentCore Registry. Any
 * mutation of that registry (creating, deleting, or transitioning record
 * approval status) must only be possible from the
 * `AgenticAI-RegistryAdmin` role in the platform account. Every other
 * principal — including workstream developer permission sets, runtime roles,
 * and the workload account's root — must be denied at the Org boundary.
 *
 * Actions denied (paired with the curator + admin actions in
 * the AgentCore Registry sample IAM documentation):
 *   - bedrock-agentcore:CreateRegistry
 *   - bedrock-agentcore:DeleteRegistry
 *   - bedrock-agentcore:UpdateRegistry
 *   - bedrock-agentcore:UpdateRegistryRecordStatus  (approve/reject/deprecate)
 *
 * `CreateRegistryRecord`, `UpdateRegistryRecord`, and the data-plane
 * `SearchRegistryRecords`/`InvokeRegistryMcp` are intentionally **not**
 * denied here — those are publisher + consumer surfaces and are scoped
 * separately via IAM at the consumer/publisher principal.
 *
 * SECURITY NOTE (bypass-regression — same shape as SCP-05/SCP-09):
 *   When the admin role is assumed, IAM evaluates `aws:PrincipalArn` as the
 *   assumed-role session ARN; we therefore ArnNotLike against BOTH the role
 *   ARN and the `assumed-role/<name>/*` form. `PrincipalIsAWSService=false`
 *   guard prevents AWS-owned principals from being self-denied.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from './index';

export interface Scp11Options {
  /** Platform account id hosting `AgenticAI-RegistryAdmin`. Required. */
  readonly platformAccountId: string;
  /**
   * Optional override for the admin role name. Default
   * `AgenticAI-RegistryAdmin`. Conformance tests pin the default; only
   * override in test fixtures.
   */
  readonly registryAdminRoleName?: string;
}

export function scp11RegistryMutationLockdown(opts: Scp11Options): ScpDefinition {
  if (!/^[0-9]{12}$/.test(opts.platformAccountId)) {
    throw new Error(
      `SCP-11: platformAccountId must be a 12-digit AWS account id; got '${opts.platformAccountId}'.`,
    );
  }
  const adminRoleName = opts.registryAdminRoleName ?? 'AgenticAI-RegistryAdmin';
  const adminRoleArn = `arn:aws:iam::${opts.platformAccountId}:role/${adminRoleName}`;
  const adminSessionArn = `arn:aws:sts::${opts.platformAccountId}:assumed-role/${adminRoleName}/*`;

  const body = {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'DenyRegistryMutationExceptRegistryAdmin',
        Effect: 'Deny',
        Action: [
          'bedrock-agentcore:CreateRegistry',
          'bedrock-agentcore:DeleteRegistry',
          'bedrock-agentcore:UpdateRegistry',
          'bedrock-agentcore:UpdateRegistryRecordStatus',
        ],
        Resource: 'arn:aws:bedrock-agentcore:*:*:registry/*',
        Condition: {
          ArnNotLike: {
            'aws:PrincipalArn': [adminRoleArn, adminSessionArn],
          },
          BoolIfExists: {
            'aws:PrincipalIsAWSService': 'false',
          },
        },
      },
    ],
  };

  return toScpDefinition(
    'scp-11',
    'AgenticAI-SCP-11-RegistryMutationLockdown',
    'Only the platform AgenticAI-RegistryAdmin role may create/update/delete the AgentCore Registry or change record approval status (D-03 v3 / v0.5.0).',
    body,
  );
}
