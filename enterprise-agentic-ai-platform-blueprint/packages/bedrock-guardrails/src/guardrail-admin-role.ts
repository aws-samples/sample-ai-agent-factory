/**
 * GuardrailAdminRole — platform-only role authorised to create/update/delete
 * Bedrock Guardrails. SCP-05 denies these actions for any other principal.
 *
 * Spec §2.4.3 L1612-1634, R-BED-011, R-BED-012.
 *
 * Trust: only the platform CI/CD pipeline role (passed as a construct prop)
 * may assume this role. The role has no trust-policy wildcard.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { ArnPrincipal, PolicyDocument, PolicyStatement, Effect, Role } from 'aws-cdk-lib/aws-iam';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface GuardrailAdminRoleProps {
  /**
   * ARN of the CI/CD pipeline role that may assume this admin role.
   * Typically the CodeBuild role inside `agenticai-platform-nonprod`.
   */
  readonly trustedPipelineRoleArn: string;

  /**
   * Name of the role. Keep stable — SCP-05 references the role ARN.
   * Default 'AgenticAI-GuardrailAdmin'.
   */
  readonly roleName?: string;
}

export class GuardrailAdminRole extends Construct {
  readonly role: Role;

  constructor(scope: Construct, id: string, props: GuardrailAdminRoleProps) {
    super(scope, id);

    if (!props.trustedPipelineRoleArn.startsWith('arn:aws:iam::')) {
      throw new Error(
        `GuardrailAdminRole: trustedPipelineRoleArn must be an IAM role ARN (got '${props.trustedPipelineRoleArn}').`,
      );
    }

    const inline = new PolicyDocument({
      statements: [
        new PolicyStatement({
          sid: 'GuardrailAdminActions',
          effect: Effect.ALLOW,
          actions: [
            'bedrock:CreateGuardrail',
            'bedrock:UpdateGuardrail',
            'bedrock:DeleteGuardrail',
            'bedrock:CreateGuardrailVersion',
            'bedrock:GetGuardrail',
            'bedrock:ListGuardrails',
          ],
          resources: ['*'],
        }),
      ],
    });

    this.role = new Role(this, 'Role', {
      roleName: props.roleName ?? 'AgenticAI-GuardrailAdmin',
      assumedBy: new ArnPrincipal(props.trustedPipelineRoleArn),
      description: 'Platform-only role for Bedrock Guardrail administration. Assumable only by the CI/CD pipeline role. Enforced by SCP-05.',
      inlinePolicies: { guardrail: inline },
    });

    NagSuppressions.addResourceSuppressions(
      this.role,
      [
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: ['Resource::*'],
          reason:
            'SEC-004: Guardrail admin actions (Create/Update/Delete/Get/ListGuardrail) do not support resource-level scoping today — AWS returns AccessDenied if a non-* resource is specified. Role is gated by SCP-05 and a narrow trust policy (pipeline role only). Will be tightened when Bedrock adds guardrail-ARN-scoped IAM conditions.',
        },
        {
          id: 'NIST.800.53.R5-IAMNoInlinePolicy',
          reason:
            'SEC-005: Single-purpose admin role; inline policy keeps guardrail permissions co-located with the role definition for reviewability. Managed-policy indirection adds no security and hinders audit (SCP-05 references the role ARN, not the managed policy ARN).',
        },
      ],
      true,
    );
  }
}
