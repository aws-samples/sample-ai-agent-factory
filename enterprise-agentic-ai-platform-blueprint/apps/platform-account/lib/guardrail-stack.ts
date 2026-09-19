/**
 * GuardrailStack — deployed into agenticai-platform-{nonprod,prod}.
 *
 * Stands up the platform Guardrail Admin role + the baseline guardrail.
 * Phase 5 replicates the baseline to every workload account; this stack is
 * the source of truth.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, StackProps, CfnOutput } from 'aws-cdk-lib';
import { IRole, Role } from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

import { GuardrailAdminRole, PlatformBaselineGuardrail } from '@agenticai/bedrock-guardrails';

export interface GuardrailStackProps extends StackProps {
  /**
   * ARN of the CI/CD pipeline role that may assume the Guardrail Admin role.
   */
  readonly pipelineRoleArn: string;

  /**
   * Reuse this role instead of creating another fixed-name admin role. Used
   * when nonproduction and production deliberately share one Platform account.
   */
  readonly existingAdminRoleArn?: string;

  /**
   * Override the baseline guardrail name when two stages share one account and
   * Region. Separate-account deployments retain the SSOT default.
   */
  readonly baselineGuardrailName?: string;
}

export class GuardrailStack extends Stack {
  readonly adminRole: IRole;
  readonly baseline: PlatformBaselineGuardrail;

  constructor(scope: Construct, id: string, props: GuardrailStackProps) {
    super(scope, id, props);

    if (props.existingAdminRoleArn && !props.existingAdminRoleArn.startsWith('arn:')) {
      throw new Error(
        `GuardrailStack: existingAdminRoleArn must be an IAM role ARN (got '${props.existingAdminRoleArn}').`,
      );
    }

    this.adminRole = props.existingAdminRoleArn
      ? Role.fromRoleArn(this, 'ImportedGuardrailAdminRole', props.existingAdminRoleArn, {
          mutable: false,
        })
      : new GuardrailAdminRole(this, 'GuardrailAdmin', {
          trustedPipelineRoleArn: props.pipelineRoleArn,
        }).role;

    this.baseline = new PlatformBaselineGuardrail(this, 'BaselineGuardrail', {
      name: props.baselineGuardrailName,
    });

    new CfnOutput(this, 'GuardrailAdminRoleArn', {
      value: this.adminRole.roleArn,
      description: 'Admin role ARN — paste into the management-account Org stack SCP-05 context value.',
      exportName: `${this.stackName}-GuardrailAdminRoleArn`,
    });

    new CfnOutput(this, 'BaselineGuardrailArn', {
      value: this.baseline.guardrail.attrGuardrailArn,
      description: 'Baseline guardrail ARN for workload-account replication.',
      exportName: `${this.stackName}-BaselineGuardrailArn`,
    });
  }
}
