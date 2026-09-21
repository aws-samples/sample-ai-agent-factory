/**
 * RegistryStack — deployed to agenticai-platform-{nonprod,prod}.
 *
 * The existing DynamoDB registry remains unchanged as the rollback path while
 * the native GA Agent Registry is introduced blue-green alongside it.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, CfnResource, Stack, StackProps } from "aws-cdk-lib";
import { Construct } from "constructs";

import {
  GaPlatformRegistryConstruct,
  GaPlatformToolsConstruct,
} from "@agenticai/agent-registry";
import { AgentCoreRegistryConstruct } from "@agenticai/agentcore-registry";

export interface RegistryStackProps extends StackProps {
  readonly envName: "nonprod" | "prod";
  readonly workloadAccountIds: readonly string[];
  readonly registrySynthAccountId: string;
  readonly grantGatewayInvokePermissions?: boolean;
  readonly gatewayServiceRoleArns?: readonly string[];
  readonly gatewayWorkloadAccountId?: string;
  readonly gaRegistryRecordGenerations?: Readonly<Record<string, number>>;
  readonly applicationId: string;
  readonly agentId: string;
  readonly tenantId: string;
  readonly costCentre: string;
}

export class RegistryStack extends Stack {
  /** Existing DynamoDB placeholder retained unchanged during migration. */
  readonly registry: AgentCoreRegistryConstruct;
  /** Pipeline-owned, environment-isolated Lambda tool aliases. */
  readonly gaTools: GaPlatformToolsConstruct;
  /** Native GA producer consumed by opt-in R2 Workstream pipelines. */
  readonly gaRegistry: GaPlatformRegistryConstruct;

  constructor(scope: Construct, id: string, props: RegistryStackProps) {
    super(scope, id, props);
    this.registry = new AgentCoreRegistryConstruct(this, "Registry", {
      envName: props.envName,
    });
    new CfnOutput(this, "AgentTableName", {
      value: this.registry.agentTable.tableName,
    });
    new CfnOutput(this, "ToolTableName", {
      value: this.registry.toolTable.tableName,
    });

    this.gaTools = new GaPlatformToolsConstruct(this, "GaTools", {
      envName: props.envName,
      workloadAccountIds: props.workloadAccountIds,
      applicationId: props.applicationId,
      agentId: props.agentId,
      tenantId: props.tenantId,
      costCentre: props.costCentre,
      grantGatewayInvokePermissions: props.grantGatewayInvokePermissions,
      gatewayServiceRoleArns: props.gatewayServiceRoleArns,
      gatewayWorkloadAccountId: props.gatewayWorkloadAccountId,
    });

    this.gaRegistry = new GaPlatformRegistryConstruct(this, "GaRegistry", {
      envName: props.envName,
      workloadAccountIds: props.workloadAccountIds,
      registrySynthAccountId: props.registrySynthAccountId,
      toolTargetArns: this.gaTools.aliasArns,
      recordGenerations: props.gaRegistryRecordGenerations,
      tags: {
        applicationId: props.applicationId,
        agentId: props.agentId,
        tenantId: props.tenantId,
        costCentre: props.costCentre,
        environment: props.envName,
      },
    });
    for (const [toolId, record] of Object.entries(this.gaRegistry.records)) {
      record.addDependency(
        this.gaTools.aliases[toolId].node.defaultChild as CfnResource,
      );
    }
  }
}
