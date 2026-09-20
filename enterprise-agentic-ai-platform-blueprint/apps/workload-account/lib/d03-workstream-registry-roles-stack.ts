/*
 * Stable IAM prerequisites for the pipeline-owned Workstream GA Gateway.
 * These roles deploy in a dedicated pipeline stage before any Gateway target,
 * so Lambda resource policies can resolve the exact Gateway role principal ID.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, Stack, StackProps, Tags } from "aws-cdk-lib";
import {
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

import type { GaRegistryConsumerContext } from "@agenticai/agent-registry";

export interface D03WorkstreamRegistryRolesStackProps extends StackProps {
  readonly envName: "nonprod" | "prod";
  readonly tenantId: string;
  readonly agentId: string;
  readonly applicationId: string;
  readonly costCentre: string;
  readonly registryContext: GaRegistryConsumerContext;
}

export class D03WorkstreamRegistryRolesStack extends Stack {
  readonly gatewayServiceRole: Role;
  readonly registryValidatorRole: Role;
  readonly gatewayAdminRole: Role;

  constructor(
    scope: Construct,
    id: string,
    props: D03WorkstreamRegistryRolesStackProps,
  ) {
    super(scope, id, props);
    if (props.registryContext.environment !== props.envName) {
      throw new Error(
        "D03WorkstreamRegistryRolesStack: Registry environment does not match.",
      );
    }
    const base = `${props.envName}-${props.tenantId}-${props.agentId}`;
    const names = {
      gateway: `AgenticAI-D03-${base}-gw-svc`,
      validator: `AgenticAI-D03-${base}-RegistryValidator`,
      admin: `AgenticAI-D03-${props.envName}-GatewayAdmin`,
    };
    for (const [kind, name] of Object.entries(names)) {
      if (name.length > 64) {
        throw new Error(
          `D03WorkstreamRegistryRolesStack: ${kind} role name exceeds 64 characters.`,
        );
      }
    }
    const targetArns = props.registryContext.records.map(
      (record) => record.document.target.arn,
    );
    if (new Set(targetArns).size !== targetArns.length) {
      throw new Error(
        "D03WorkstreamRegistryRolesStack: Registry target ARNs must be unique.",
      );
    }

    this.gatewayServiceRole = new Role(this, "GatewayServiceRole", {
      roleName: names.gateway,
      assumedBy: new ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      description:
        "Stable AgentCore Gateway service role scoped to exact Registry-resolved tool aliases.",
      inlinePolicies: {
        InvokeSubscribedTools: new PolicyDocument({
          statements: [
            new PolicyStatement({
              sid: "InvokeSubscribedTools",
              actions: ["lambda:InvokeFunction"],
              resources: targetArns,
            }),
          ],
        }),
      },
    });

    this.registryValidatorRole = new Role(this, "RegistryValidatorRole", {
      roleName: names.validator,
      assumedBy: new ServicePrincipal("lambda.amazonaws.com"),
      description:
        "Stable deploy-time role that assumes the Platform RegistryReaderRole.",
      inlinePolicies: {
        AssumeRegistryReader: new PolicyDocument({
          statements: [
            new PolicyStatement({
              actions: ["sts:AssumeRole"],
              resources: [props.registryContext.readerRoleArn],
            }),
          ],
        }),
      },
      managedPolicies: [
        ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole",
        ),
      ],
    });

    this.gatewayAdminRole = new Role(this, "GatewayAdminRole", {
      roleName: names.admin,
      assumedBy: new ServicePrincipal("lambda.amazonaws.com"),
      description:
        "Workstream-local provisioning role allowed by SCP-09 to manage AgentCore Gateways.",
      inlinePolicies: {
        ProvisionGateway: new PolicyDocument({
          statements: [
            new PolicyStatement({
              actions: ["bedrock-agentcore:*"],
              resources: ["*"],
            }),
            new PolicyStatement({
              actions: ["iam:PassRole"],
              resources: [this.gatewayServiceRole.roleArn],
              conditions: {
                StringEquals: {
                  "iam:PassedToService": "bedrock-agentcore.amazonaws.com",
                },
              },
            }),
          ],
        }),
      },
      managedPolicies: [
        ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole",
        ),
      ],
    });

    new CfnOutput(this, "GatewayServiceRoleArn", {
      description:
        "Exact existing principal ARN for agenticai/gaGatewayServiceRoleArns.",
      value: this.gatewayServiceRole.roleArn,
    });

    for (const role of [
      this.gatewayServiceRole,
      this.registryValidatorRole,
      this.gatewayAdminRole,
    ]) {
      Tags.of(role).add("application-id", props.applicationId);
      Tags.of(role).add("agent-id", props.agentId);
      Tags.of(role).add("tenant-id", props.tenantId);
      Tags.of(role).add("cost-centre", props.costCentre);
      Tags.of(role).add("environment", props.envName);
    }
    NagSuppressions.addResourceSuppressions(
      this.registryValidatorRole,
      [
        {
          id: "AwsSolutions-IAM4",
          appliesTo: [
            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
          ],
          reason:
            "SEC-010: AWSLambdaBasicExecutionRole is the documented logging policy for the validator Lambda.",
        },
      ],
      true,
    );
    NagSuppressions.addResourceSuppressions(
      this.gatewayAdminRole,
      [
        {
          id: "AwsSolutions-IAM4",
          appliesTo: [
            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
          ],
          reason:
            "SEC-010: AWSLambdaBasicExecutionRole is the documented logging policy for the AgentCore provisioning Lambda.",
        },
        {
          id: "AwsSolutions-IAM5",
          reason:
            "SEC-028: The AgentCore control-plane action family requires Resource:* during resource creation; the role is Workstream-local, Lambda-trusted, pipeline-created, and SCP-09-name-bound.",
        },
      ],
      true,
    );
    NagSuppressions.addResourceSuppressions(
      this.gatewayServiceRole,
      [
        {
          id: "NIST.800.53.R5-IAMNoInlinePolicy",
          reason:
            "SEC-005: The exact Registry-resolved Lambda alias allowlist must remain visible on the stable Gateway service role.",
        },
      ],
      true,
    );
  }
}
