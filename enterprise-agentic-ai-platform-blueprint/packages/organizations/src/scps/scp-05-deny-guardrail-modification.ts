/**
 * SCP-05 — Deny Guardrail Modification in Workload Accounts.
 *
 * Spec §2.2.6 L752-783. Delivery teams in workload accounts cannot
 * create/update/delete Bedrock Guardrails. Only the platform Guardrail Admin
 * role (deployed in platform-nonprod/prod per Phase 3) may do so. This
 * enforces segregation of duties per R-BED-011 and R-BED-012.
 *
 * SECURITY NOTE (bypass-regression fix):
 *   Previous revision used `StringNotEquals` against the role ARN only.
 *   When the role is assumed, IAM evaluates `aws:PrincipalArn` as the
 *   assumed-role session ARN (`arn:aws:sts::<acct>:assumed-role/<name>/<sess>`),
 *   not the role ARN itself (`arn:aws:iam::<acct>:role/<name>`). The exact
 *   `StringNotEquals` would reject the admin session and allow anyone else.
 *   We now use `ArnNotLike` against BOTH the role ARN and the
 *   assumed-role/<name>/* session form derived from the role name. A
 *   `PrincipalIsAWSService=false` guard keeps AWS-owned service principals
 *   from being self-denied.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from './index';

export function scp05DenyGuardrailModification(platformGuardrailAdminRoleArn: string): ScpDefinition {
  if (!platformGuardrailAdminRoleArn.startsWith('arn:aws:iam::')) {
    throw new Error(
      `SCP-05: platformGuardrailAdminRoleArn must be a full IAM role ARN; got '${platformGuardrailAdminRoleArn}'.`,
    );
  }

  // Derive the assumed-role session ARN pattern from the role ARN.
  // Role ARN: arn:aws:iam::<acct>:role/<path?>/<name>
  // Session ARN: arn:aws:sts::<acct>:assumed-role/<name>/*
  const match = /^arn:aws:iam::([0-9]{12}):role\/(?:.*\/)?([^/]+)$/.exec(
    platformGuardrailAdminRoleArn,
  );
  if (!match) {
    throw new Error(
      `SCP-05: platformGuardrailAdminRoleArn '${platformGuardrailAdminRoleArn}' is not a parseable role ARN.`,
    );
  }
  const accountId = match[1];
  const roleName = match[2];
  const assumedRoleArn = `arn:aws:sts::${accountId}:assumed-role/${roleName}/*`;

  const body = {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'DenyGuardrailModificationOutsidePlatformAdmin',
        Effect: 'Deny',
        Action: [
          'bedrock:CreateGuardrail',
          'bedrock:UpdateGuardrail',
          'bedrock:DeleteGuardrail',
          'bedrock:CreateGuardrailVersion',
        ],
        Resource: '*',
        Condition: {
          ArnNotLike: {
            'aws:PrincipalArn': [platformGuardrailAdminRoleArn, assumedRoleArn],
          },
          BoolIfExists: {
            'aws:PrincipalIsAWSService': 'false',
          },
        },
      },
    ],
  };

  return toScpDefinition(
    'scp-05',
    'AgenticAI-SCP-05-DenyGuardrailModification',
    'Only the platform Guardrail Admin role may modify Bedrock Guardrails (spec §2.2.6).',
    body,
  );
}
