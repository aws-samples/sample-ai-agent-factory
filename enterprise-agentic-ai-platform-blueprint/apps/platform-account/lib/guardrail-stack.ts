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
import { Construct } from 'constructs';

import { GuardrailAdminRole, PlatformBaselineGuardrail } from '@agenticai/bedrock-guardrails';

export interface GuardrailStackProps extends StackProps {
  /**
   * ARN of the CI/CD pipeline role that may assume the Guardrail Admin role.
   */
  readonly pipelineRoleArn: string;
}

export class GuardrailStack extends Stack {
  readonly adminRole: GuardrailAdminRole;
  readonly baseline: PlatformBaselineGuardrail;

  constructor(scope: Construct, id: string, props: GuardrailStackProps) {
    super(scope, id, props);

    this.adminRole = new GuardrailAdminRole(this, 'GuardrailAdmin', {
      trustedPipelineRoleArn: props.pipelineRoleArn,
    });

    this.baseline = new PlatformBaselineGuardrail(this, 'BaselineGuardrail');

    new CfnOutput(this, 'GuardrailAdminRoleArn', {
      value: this.adminRole.role.roleArn,
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
