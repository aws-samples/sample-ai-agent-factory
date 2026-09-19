/**
 * RegistryStack — deployed to agenticai-platform-{nonprod,prod}.
 *
 * The existing DynamoDB registry remains unchanged as the rollback path while
 * the native GA Agent Registry is introduced blue-green alongside it.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, StackProps, CfnOutput } from 'aws-cdk-lib';
import { Construct } from 'constructs';

import { GaPlatformRegistryConstruct } from '@agenticai/agent-registry';
import { AgentCoreRegistryConstruct } from '@agenticai/agentcore-registry';

export interface RegistryStackProps extends StackProps {
  readonly envName: 'nonprod' | 'prod';
  readonly workloadAccountIds: readonly string[];
  readonly applicationId: string;
  readonly agentId: string;
  readonly tenantId: string;
  readonly costCentre: string;
}

export class RegistryStack extends Stack {
  /** Existing DynamoDB placeholder retained unchanged during migration. */
  readonly registry: AgentCoreRegistryConstruct;
  /** Native GA producer; Workstreams do not consume it until revision R2. */
  readonly gaRegistry: GaPlatformRegistryConstruct;

  constructor(scope: Construct, id: string, props: RegistryStackProps) {
    super(scope, id, props);
    this.registry = new AgentCoreRegistryConstruct(this, 'Registry', {
      envName: props.envName,
    });
    new CfnOutput(this, 'AgentTableName', { value: this.registry.agentTable.tableName });
    new CfnOutput(this, 'ToolTableName', { value: this.registry.toolTable.tableName });

    this.gaRegistry = new GaPlatformRegistryConstruct(this, 'GaRegistry', {
      envName: props.envName,
      workloadAccountIds: props.workloadAccountIds,
      tags: {
        applicationId: props.applicationId,
        agentId: props.agentId,
        tenantId: props.tenantId,
        costCentre: props.costCentre,
        environment: props.envName,
      },
    });
  }
}
