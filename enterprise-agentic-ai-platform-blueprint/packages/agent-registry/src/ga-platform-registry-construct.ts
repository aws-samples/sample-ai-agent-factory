/*
 * Native GA Agent Registry producer used by the pipeline-owned RegistryStack.
 *
 * This construct is deliberately additive during the blue-green migration. It
 * does not replace or rename the existing DynamoDB AgentCoreRegistryConstruct.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  CfnOutput,
  CfnResource,
  Duration,
  RemovalPolicy,
  Stack,
  Tags,
} from "aws-cdk-lib";
import {
  AccountPrincipal,
  CfnRole,
  CompositePrincipal,
  Effect,
  ManagedPolicy,
  PolicyStatement,
  Role,
} from "aws-cdk-lib/aws-iam";
import { StringParameter } from "aws-cdk-lib/aws-ssm";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

import {
  PLATFORM_TOOL_CATALOGUE,
  PLATFORM_TOOL_CATALOGUE_VERSION,
  resolveTargetArn,
  validateToolSpec,
  type ToolSpec,
} from "@agenticai/platform-tool-catalogue";

export interface GaPlatformRegistryTags {
  readonly applicationId: string;
  readonly agentId: string;
  readonly tenantId: string;
  readonly costCentre: string;
  readonly environment: string;
}

export interface GaPlatformRegistryConstructProps {
  readonly envName: "nonprod" | "prod";
  readonly workloadAccountIds: readonly string[];
  /** Account that owns the Workload pipeline's explicitly named synth role. */
  readonly registrySynthAccountId: string;
  /** Pipeline-owned environment-specific tool alias ARNs keyed by stable tool ID. */
  readonly toolTargetArns?: Readonly<Record<string, string>>;
  /**
   * Optional monotonic replacement generation for terminal Registry records.
   * Omitted records retain their original CloudFormation logical identity.
   */
  readonly recordGenerations?: Readonly<Record<string, number>>;
  readonly tags: GaPlatformRegistryTags;
}

export interface GaToolGovernanceDocument {
  readonly schemaVersion: "agenticai.tool-governance/1.0";
  readonly catalogueVersion: string;
  readonly toolId: string;
  readonly description: string;
  readonly desiredApprovalStatus: ToolSpec["approvalStatus"];
  readonly target: {
    readonly type: NonNullable<ToolSpec["toolType"]>;
    readonly arn: string;
  };
  readonly mcp: {
    readonly toolName: string;
    readonly description: string;
    readonly inputSchema: Record<string, unknown>;
  };
  readonly authorization: {
    readonly defaultDecision: "DENY";
    readonly cedarPolicy: string;
    readonly allowedSubjects: readonly string[];
    readonly allowedGroups: readonly string[];
    readonly combination: "AUTHENTICATED" | "GROUP_ONLY";
  };
  readonly ownership: {
    readonly ownerTeam: string;
    readonly costCentre: string;
  };
}

/** Build the opaque governance document stored in a GA CUSTOM RegistryRecord. */
export function buildGaToolGovernanceDocument(
  tool: ToolSpec,
  platformAccountId: string,
  platformRegion: string,
  targetArnOverride?: string,
): GaToolGovernanceDocument {
  validateToolSpec(tool);
  const allowedGroups = [...(tool.allowedGroups ?? [])];
  return {
    schemaVersion: "agenticai.tool-governance/1.0",
    catalogueVersion: PLATFORM_TOOL_CATALOGUE_VERSION,
    toolId: tool.toolId,
    description: tool.description,
    desiredApprovalStatus: tool.approvalStatus,
    target: {
      type: tool.toolType ?? "lambda",
      arn:
        targetArnOverride ??
        resolveTargetArn(tool, platformAccountId, platformRegion),
    },
    mcp: {
      toolName: tool.toolId,
      description: tool.description,
      inputSchema: tool.inputSchema ?? { type: "object" },
    },
    authorization: {
      defaultDecision: "DENY",
      cedarPolicy: tool.cedarPolicy.trim(),
      allowedSubjects: [],
      allowedGroups,
      combination: allowedGroups.length > 0 ? "GROUP_ONLY" : "AUTHENTICATED",
    },
    ownership: {
      ownerTeam: tool.ownerTeam,
      costCentre: tool.costCentre,
    },
  };
}

/**
 * Pipeline-owned GA Registry producer for one Platform environment.
 *
 * Resources use RetainExceptOnCreate semantics: a failed first deployment
 * cleans itself up, while a later stack rollback cannot destroy approved
 * governance state. The old DynamoDB registry remains the rollback path until
 * a live rollback deployment from the R2 revision passes independently.
 */
export class GaPlatformRegistryConstruct extends Construct {
  readonly registry: CfnResource;
  readonly registryId: string;
  readonly registryArn: string;
  readonly records: Readonly<Record<string, CfnResource>>;
  readonly recordIdParameters: Readonly<Record<string, StringParameter>>;
  readonly readerRole: Role;
  readonly readerExternalId: string;
  readonly parameterPrefix: string;

  constructor(
    scope: Construct,
    id: string,
    props: GaPlatformRegistryConstructProps,
  ) {
    super(scope, id);

    if (props.workloadAccountIds.length === 0) {
      throw new Error(
        "GaPlatformRegistryConstruct: workloadAccountIds must not be empty.",
      );
    }
    const workloadAccountIds = [...new Set(props.workloadAccountIds)].sort();
    for (const accountId of workloadAccountIds) {
      if (!/^\d{12}$/.test(accountId)) {
        throw new Error(
          `GaPlatformRegistryConstruct: workload account '${accountId}' must be 12 digits.`,
        );
      }
    }
    if (!/^\d{12}$/.test(props.registrySynthAccountId)) {
      throw new Error(
        "GaPlatformRegistryConstruct: registrySynthAccountId must be 12 digits.",
      );
    }
    if (props.toolTargetArns) {
      const expected = Object.keys(PLATFORM_TOOL_CATALOGUE).sort();
      const actual = Object.keys(props.toolTargetArns).sort();
      if (JSON.stringify(actual) !== JSON.stringify(expected)) {
        throw new Error(
          "GaPlatformRegistryConstruct: toolTargetArns must exactly match the platform catalogue.",
        );
      }
    }
    const recordGenerations = props.recordGenerations ?? {};
    for (const [toolId, generation] of Object.entries(recordGenerations)) {
      if (!(toolId in PLATFORM_TOOL_CATALOGUE)) {
        throw new Error(
          `GaPlatformRegistryConstruct: record generation names unknown tool '${toolId}'.`,
        );
      }
      if (
        !Number.isSafeInteger(generation) ||
        generation < 2 ||
        generation > 999
      ) {
        throw new Error(
          `GaPlatformRegistryConstruct: record generation for '${toolId}' must be an integer from 2 through 999.`,
        );
      }
    }

    const stack = Stack.of(this);
    const tags = {
      "application-id": props.tags.applicationId,
      "agent-id": props.tags.agentId,
      "tenant-id": props.tags.tenantId,
      "cost-centre": props.tags.costCentre,
      environment: props.tags.environment,
    };
    const cfnTags = Object.entries(tags)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([Key, Value]) => ({ Key, Value }));
    const tag = (resource: Construct): void => {
      for (const [key, value] of Object.entries(tags)) {
        Tags.of(resource).add(key, value);
      }
    };

    const registryName = `agenticai-platform-${props.envName}-v1`;
    this.registry = new CfnResource(this, "Registry", {
      type: "AWS::AgentRegistry::Registry",
      properties: {
        Name: registryName,
        Description: `Platform-owned GA Agent Registry (${props.envName}).`,
        AuthorizerType: "AWS_IAM",
        ApprovalConfiguration: { AutoApprovalRules: ["APPROVE_ALL"] },
        Tags: cfnTags,
      },
    });
    this.registry.applyRemovalPolicy(RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE);
    this.registryId = this.registry.getAtt("RegistryId").toString();
    this.registryArn = this.registry.getAtt("RegistryArn").toString();

    const records: Record<string, CfnResource> = {};
    const recordIdParameters: Record<string, StringParameter> = {};
    this.parameterPrefix = `/agenticai/registry/v1/${props.envName}`;

    const parameter = (
      constructId: string,
      name: string,
      value: string,
      description: string,
    ): StringParameter => {
      const result = new StringParameter(this, constructId, {
        parameterName: `${this.parameterPrefix}/${name}`,
        stringValue: value,
        description,
      });
      result.applyRemovalPolicy(RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE);
      tag(result);
      return result;
    };

    for (const tool of Object.values(PLATFORM_TOOL_CATALOGUE).sort(
      (left, right) => left.toolId.localeCompare(right.toolId),
    )) {
      const governance = buildGaToolGovernanceDocument(
        tool,
        stack.account,
        stack.region,
        props.toolTargetArns?.[tool.toolId],
      );
      const generation = recordGenerations[tool.toolId];
      const recordConstructId =
        generation === undefined
          ? `Record-${tool.toolId}`
          : `Record-${tool.toolId}-generation-${generation}`;
      const record = new CfnResource(this, recordConstructId, {
        type: "AWS::AgentRegistry::RegistryRecord",
        properties: {
          RegistryId: this.registryId,
          Name: tool.toolId,
          DisplayName: tool.toolId,
          Description: tool.description,
          RecordType: "CUSTOM",
          RecordVersion: "2.0.0",
          Descriptors: {
            Custom: {
              Data: JSON.stringify(governance),
            },
          },
          Tags: cfnTags,
        },
      });
      record.addDependency(this.registry);
      record.applyRemovalPolicy(RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE);
      records[tool.toolId] = record;
      recordIdParameters[tool.toolId] = parameter(
        `RecordIdParameter-${tool.toolId}`,
        `records/${tool.toolId}/id`,
        record.getAtt("RecordId").toString(),
        `GA Agent Registry record id for ${tool.toolId}.`,
      );
    }
    this.records = records;
    this.recordIdParameters = recordIdParameters;

    this.readerExternalId = `agenticai-registry-v1-${props.envName}-${stack.account}`;
    const workloadPrincipals = workloadAccountIds.map(
      (accountId) => new AccountPrincipal(accountId),
    );
    this.readerRole = new Role(this, "RegistryReaderRole", {
      roleName: `AgenticAI-RegistryReader-${props.envName}`,
      assumedBy: new CompositePrincipal(...workloadPrincipals),
      description: `Read-only GA Agent Registry role for ${props.envName} Workstreams.`,
      maxSessionDuration: Duration.hours(1),
    });
    tag(this.readerRole);

    const cfnReaderRole = this.readerRole.node.defaultChild as CfnRole;
    cfnReaderRole.assumeRolePolicyDocument = {
      Version: "2012-10-17",
      Statement: [
        {
          Sid: "AllowWorkstreamRegistryValidators",
          Effect: "Allow",
          Principal: {
            AWS: workloadAccountIds.map(
              (accountId) => `arn:${stack.partition}:iam::${accountId}:root`,
            ),
          },
          Action: "sts:AssumeRole",
          Condition: {
            StringEquals: {
              "sts:ExternalId": this.readerExternalId,
            },
            StringLike: {
              "aws:PrincipalArn": workloadAccountIds.map(
                (accountId) =>
                  `arn:${stack.partition}:iam::${accountId}:role/AgenticAI-D03-*-RegistryValidator`,
              ),
              "sts:RoleSessionName": "registry-*",
            },
          },
        },
        {
          Sid: "AllowWorkloadPipelineRegistryResolution",
          Effect: "Allow",
          Principal: {
            AWS: `arn:${stack.partition}:iam::${props.registrySynthAccountId}:root`,
          },
          Action: "sts:AssumeRole",
          Condition: {
            StringEquals: {
              "sts:ExternalId": `agenticai-registry-synth-v1-${props.envName}-${stack.account}`,
            },
            ArnEquals: {
              "aws:PrincipalArn": `arn:${stack.partition}:iam::${props.registrySynthAccountId}:role/AgenticAI-WLP-${props.tags.tenantId}-${props.tags.agentId}-RegistrySynth`,
            },
            StringLike: {
              "sts:RoleSessionName": "registry-synth-*",
            },
          },
        },
      ],
    };

    const readerPolicy = new ManagedPolicy(this, "RegistryReaderPolicy", {
      managedPolicyName: `AgenticAI-RegistryReader-${props.envName}`,
      description: `Read-only access to the ${props.envName} GA Agent Registry.`,
      statements: [
        new PolicyStatement({
          sid: "ReadRegistryMetadata",
          effect: Effect.ALLOW,
          actions: [
            "agent-registry:GetRegistry",
            "agent-registry:ListRegistryRecords",
          ],
          resources: [this.registryArn],
        }),
        new PolicyStatement({
          sid: "ReadRegistryRecords",
          effect: Effect.ALLOW,
          actions: [
            "agent-registry:GetRegistryRecord",
            "agent-registry:GetDiscoverableRegistryRecord",
          ],
          resources: [`${this.registryArn}/record/*`],
        }),
        new PolicyStatement({
          sid: "ReadRegistryTags",
          effect: Effect.ALLOW,
          actions: ["agent-registry:ListTagsForResource"],
          resources: [this.registryArn, `${this.registryArn}/record/*`],
        }),
        new PolicyStatement({
          sid: "DiscoverApprovedRegistryRecords",
          effect: Effect.ALLOW,
          actions: [
            "agent-registry:ListDiscoverableRegistryRecords",
            "agent-registry:SearchDiscoverableRegistryRecords",
          ],
          resources: [this.registryArn],
        }),
        new PolicyStatement({
          sid: "ReadRegistryDiscoveryParameters",
          effect: Effect.ALLOW,
          actions: ["ssm:GetParameters"],
          resources: [
            `arn:${stack.partition}:ssm:${stack.region}:${stack.account}:parameter${this.parameterPrefix}/*`,
          ],
        }),
      ],
    });
    this.readerRole.addManagedPolicy(readerPolicy);
    NagSuppressions.addResourceSuppressions(
      readerPolicy,
      [
        {
          id: "AwsSolutions-IAM5",
          reason:
            "SEC-030: RegistryRecord IDs are service-generated and SSM discovery uses a versioned environment prefix; both wildcards are constrained under one Registry ARN or one exact parameter path, and conformance tests pin every action/resource pair.",
        },
      ],
      true,
    );

    const registryIdParameter = parameter(
      "RegistryIdParameter",
      "id",
      this.registryId,
      `GA Agent Registry id for ${props.envName}.`,
    );
    const registryArnParameter = parameter(
      "RegistryArnParameter",
      "arn",
      this.registryArn,
      `GA Agent Registry ARN for ${props.envName}.`,
    );
    const readerRoleArnParameter = parameter(
      "ReaderRoleArnParameter",
      "reader-role-arn",
      this.readerRole.roleArn,
      `GA Agent Registry reader role ARN for ${props.envName}.`,
    );
    const readerExternalIdParameter = parameter(
      "ReaderExternalIdParameter",
      "reader-external-id",
      this.readerExternalId,
      `GA Agent Registry reader ExternalId for ${props.envName}.`,
    );

    new CfnOutput(this, "RegistryId", { value: this.registryId });
    new CfnOutput(this, "RegistryArn", { value: this.registryArn });
    new CfnOutput(this, "RegistryReaderRoleArn", {
      value: this.readerRole.roleArn,
    });
    new CfnOutput(this, "RegistryIdParameterName", {
      value: registryIdParameter.parameterName,
    });
    new CfnOutput(this, "RegistryArnParameterName", {
      value: registryArnParameter.parameterName,
    });
    new CfnOutput(this, "RegistryReaderRoleArnParameterName", {
      value: readerRoleArnParameter.parameterName,
    });
    new CfnOutput(this, "RegistryReaderExternalIdParameterName", {
      value: readerExternalIdParameter.parameterName,
    });
  }
}
