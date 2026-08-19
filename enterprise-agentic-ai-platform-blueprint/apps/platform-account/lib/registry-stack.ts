/**
 * RegistryStack — deployed to agenticai-platform-{nonprod,prod}.
 *
 * Spec §2.1.3 L336-338 / R-ARCH-010. Emits Agent + Tool DynamoDB tables
 * that replace the imperative notebook registration pattern from
 * source/module-3b-agentcore.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, StackProps, CfnOutput } from 'aws-cdk-lib';
import { Construct } from 'constructs';

import { AgentCoreRegistryConstruct } from '@agenticai/agentcore-registry';

export interface RegistryStackProps extends StackProps {
  readonly envName: string;
}

export class RegistryStack extends Stack {
  readonly registry: AgentCoreRegistryConstruct;

  constructor(scope: Construct, id: string, props: RegistryStackProps) {
    super(scope, id, props);
    this.registry = new AgentCoreRegistryConstruct(this, 'Registry', {
      envName: props.envName,
    });
    new CfnOutput(this, 'AgentTableName', { value: this.registry.agentTable.tableName });
    new CfnOutput(this, 'ToolTableName', { value: this.registry.toolTable.tableName });
  }
}
