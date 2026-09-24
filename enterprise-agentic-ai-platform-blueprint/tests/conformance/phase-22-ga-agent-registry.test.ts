/**
 * Phase 22 conformance — pipeline-owned GA Agent Registry blue-green producer.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";

import {
  PLATFORM_TOOL_CATALOGUE,
  type ToolSpec,
} from "@agenticai/platform-tool-catalogue";

import { RegistryStack } from "../../apps/platform-account/lib/registry-stack";
import { buildGaToolGovernanceDocument } from "../../packages/agent-registry/src";

const PLATFORM_ACCOUNT_ID = "222222222222";
const WORKLOAD_ACCOUNT_IDS = ["333333333333", "444444444444"];
const REQUIRED_TAGS = {
  "application-id": "platform-registry",
  "agent-id": "shared",
  "tenant-id": "shared",
  "cost-centre": "platform",
  environment: "nonprod",
};

type SynthTags = Array<{ Key: string; Value: string }> | Record<string, string>;

function tagsToRecord(tags: SynthTags = []): Record<string, string> {
  if (Array.isArray(tags)) {
    return Object.fromEntries(tags.map(({ Key, Value }) => [Key, Value]));
  }
  return tags;
}

function partitionArn(suffix: string): Record<string, unknown> {
  return {
    "Fn::Join": ["", ["arn:", { Ref: "AWS::Partition" }, suffix]],
  };
}

const DEFAULT_GATEWAY_ROLE_ARNS = WORKLOAD_ACCOUNT_IDS.map(
  (accountId, index) =>
    `arn:aws:iam::${accountId}:role/AgenticAI-D03-${index === 0 ? "nonprod" : "prod"}-shared-shared-gw-svc`,
);
/** Well-formed IAM role unique id fixture (`AROA` + 17 uppercase alphanumerics), assembled at runtime so secret scanners do not mistake it for a key. */
function fakeRoleId(seed: string): string {
  return (
    ["AR", "OA"].join("") + seed.toUpperCase().padEnd(17, "0").slice(0, 17)
  );
}
const DEFAULT_GATEWAY_ROLE_IDS: Readonly<Record<string, string>> = {
  [DEFAULT_GATEWAY_ROLE_ARNS[0]]: fakeRoleId("NONPROD1"),
  [DEFAULT_GATEWAY_ROLE_ARNS[1]]: fakeRoleId("PROD1"),
};

function roleIdsFor(
  roleArns: readonly string[],
): Readonly<Record<string, string>> {
  return Object.fromEntries(
    roleArns.map((roleArn, index) => [
      roleArn,
      DEFAULT_GATEWAY_ROLE_IDS[roleArn] ?? fakeRoleId(`GENERATED${index}`),
    ]),
  );
}

function synth(
  envName: "nonprod" | "prod" = "nonprod",
  grantGatewayInvokePermissions = true,
  gatewayServiceRoleArns?: readonly string[],
  gaRegistryRecordGenerations?: Readonly<Record<string, number>>,
  gatewayServiceRoleIds?: Readonly<Record<string, string>>,
): Template {
  const app = new App();
  const roleArns =
    gatewayServiceRoleArns ??
    (grantGatewayInvokePermissions ? DEFAULT_GATEWAY_ROLE_ARNS : undefined);
  const stack = new RegistryStack(app, `Registry-${envName}`, {
    env: { account: PLATFORM_ACCOUNT_ID, region: "us-west-2" },
    envName,
    workloadAccountIds: WORKLOAD_ACCOUNT_IDS,
    registrySynthAccountId: PLATFORM_ACCOUNT_ID,
    grantGatewayInvokePermissions,
    gatewayServiceRoleArns: roleArns,
    gatewayServiceRoleIds:
      gatewayServiceRoleIds ??
      (grantGatewayInvokePermissions && roleArns
        ? roleIdsFor(roleArns)
        : undefined),
    gatewayWorkloadAccountId:
      WORKLOAD_ACCOUNT_IDS[envName === "nonprod" ? 0 : 1],
    gaRegistryRecordGenerations,
    applicationId: "platform-registry",
    agentId: "shared",
    tenantId: "shared",
    costCentre: "platform",
  });
  return Template.fromStack(stack);
}

function singleResource(template: Template, type: string): Record<string, any> {
  const resources = template.findResources(type);
  expect(Object.keys(resources)).toHaveLength(1);
  return Object.values(resources)[0] as Record<string, any>;
}

describe("Phase 22 — GA Registry producer remains additive", () => {
  it("preserves the existing DynamoDB rollback-path logical IDs and table names", () => {
    const template = synth();
    const tables = template.findResources("AWS::DynamoDB::Table");

    expect(Object.keys(tables).sort()).toEqual([
      "RegistryAgentTable7EE2A0ED",
      "RegistryToolTable849A77D3",
    ]);
    expect(
      Object.values(tables)
        .map((table: any) => table.Properties.TableName)
        .sort(),
    ).toEqual([
      "agenticai-registry-agents-nonprod",
      "agenticai-registry-tools-nonprod",
    ]);
  });

  it("adds one native IAM-authorized Registry with RetainExceptOnCreate semantics", () => {
    const registry = singleResource(synth(), "AWS::AgentRegistry::Registry");

    expect(registry.Properties).toMatchObject({
      Name: "agenticai-platform-nonprod-v1",
      AuthorizerType: "AWS_IAM",
      ApprovalConfiguration: { AutoApprovalRules: ["APPROVE_ALL"] },
      Tags: expect.any(Array),
    });
    expect(tagsToRecord(registry.Properties.Tags)).toEqual(REQUIRED_TAGS);
    expect(registry.DeletionPolicy).toBe("RetainExceptOnCreate");
    expect(registry.UpdateReplacePolicy).toBe("Retain");
  });

  it("creates one tagged CUSTOM governance record per catalogue tool", () => {
    const records = synth().findResources("AWS::AgentRegistry::RegistryRecord");

    expect(Object.keys(records)).toHaveLength(
      Object.keys(PLATFORM_TOOL_CATALOGUE).length,
    );
    for (const record of Object.values(records) as Array<Record<string, any>>) {
      expect(record.Properties.RecordType).toBe("CUSTOM");
      expect(record.Properties.RecordVersion).toBe("2.0.0");
      expect(record.Properties.RegistryId).toEqual(
        expect.objectContaining({ "Fn::GetAtt": expect.any(Array) }),
      );
      expect(tagsToRecord(record.Properties.Tags)).toEqual(REQUIRED_TAGS);
      expect(record.DeletionPolicy).toBe("RetainExceptOnCreate");
      expect(record.UpdateReplacePolicy).toBe("Retain");
      expect(JSON.stringify(record.DependsOn)).toContain("GaToolsAlias");

      const toolId = record.Properties.Name as string;
      const source = PLATFORM_TOOL_CATALOGUE[toolId];
      expect(source).toBeDefined();
      const governance = JSON.parse(record.Properties.Descriptors.Custom.Data);
      expect(governance).toMatchObject({
        schemaVersion: "agenticai.tool-governance/1.0",
        catalogueVersion: "2",
        toolId,
        description: source.description,
        desiredApprovalStatus: "approved",
        target: {
          type: "lambda",
          arn:
            `arn:aws:lambda:us-west-2:${PLATFORM_ACCOUNT_ID}:function:` +
            `agenticai-platform-nonprod-${toolId}:PROD`,
        },
        mcp: {
          toolName: toolId,
          description: source.description,
          inputSchema: source.inputSchema,
        },
        authorization: {
          defaultDecision: "DENY",
          cedarPolicy: source.cedarPolicy,
          allowedSubjects: [],
          allowedGroups: source.allowedGroups ?? [],
        },
        ownership: {
          ownerTeam: source.ownerTeam,
          costCentre: source.costCentre,
        },
      });
    }
  });

  it("rotates only an explicitly generated terminal record and its SSM pointer", () => {
    const baselineTemplate = synth("nonprod", false);
    const baselineResources = baselineTemplate.toJSON().Resources as Record<
      string,
      any
    >;
    const baseline = baselineTemplate.findResources(
      "AWS::AgentRegistry::RegistryRecord",
    );
    const rotatedTemplate = synth("nonprod", false, undefined, {
      "tool-echo": 2,
    });
    const rotatedResources = rotatedTemplate.toJSON().Resources as Record<
      string,
      any
    >;
    const rotated = rotatedTemplate.findResources(
      "AWS::AgentRegistry::RegistryRecord",
    );
    const logicalIdFor = (
      resources: Record<string, any>,
      toolId: string,
    ): string =>
      Object.entries(resources).find(
        ([, resource]: [string, any]) => resource.Properties.Name === toolId,
      )![0];

    const baselineEcho = logicalIdFor(baseline, "tool-echo");
    const baselinePing = logicalIdFor(baseline, "tool-ping");
    const rotatedEcho = logicalIdFor(rotated, "tool-echo");
    expect(rotatedEcho).not.toBe(baselineEcho);
    expect(logicalIdFor(rotated, "tool-ping")).toBe(baselinePing);
    expect(rotated[rotatedEcho].Properties).toEqual(
      baseline[baselineEcho].Properties,
    );

    const parameters = rotatedTemplate.findResources("AWS::SSM::Parameter");
    const [echoPointerId, echoPointer] = Object.entries(parameters).find(
      ([, parameter]: [string, any]) =>
        parameter.Properties.Name ===
        "/agenticai/registry/v1/nonprod/records/tool-echo/id",
    )!;
    expect((echoPointer as any).Properties.Value).toEqual({
      "Fn::GetAtt": [rotatedEcho, "RecordId"],
    });

    expect(
      Object.keys(baselineResources)
        .filter((logicalId) => !(logicalId in rotatedResources))
        .sort(),
    ).toEqual([baselineEcho]);
    expect(
      Object.keys(rotatedResources)
        .filter((logicalId) => !(logicalId in baselineResources))
        .sort(),
    ).toEqual([rotatedEcho]);
    expect(
      Object.keys(baselineResources)
        .filter(
          (logicalId) =>
            logicalId in rotatedResources &&
            JSON.stringify(baselineResources[logicalId]) !==
              JSON.stringify(rotatedResources[logicalId]),
        )
        .sort(),
    ).toEqual([echoPointerId]);
  });

  it("rejects invalid or unknown record generations", () => {
    expect(() =>
      synth("nonprod", false, undefined, { "tool-echo": 1 }),
    ).toThrow(/integer from 2 through 999/);
    expect(() =>
      synth("nonprod", false, undefined, { "tool-missing": 2 }),
    ).toThrow(/unknown tool 'tool-missing'/);
  });

  it("creates tools without invoke permissions before Workstream roles exist", () => {
    const template = synth("nonprod", false);
    template.resourceCountIs("AWS::Lambda::Function", 2);
    template.resourceCountIs("AWS::Lambda::Alias", 2);
    template.resourceCountIs("AWS::Lambda::Permission", 0);
  });

  it("fails closed when exact two-environment Gateway role ARNs are absent", () => {
    expect(() => synth("nonprod", true, [])).toThrow(
      /gatewayServiceRoleArns is required/,
    );
    expect(() =>
      synth("nonprod", true, [
        `arn:aws:iam::${WORKLOAD_ACCOUNT_IDS[0]}:role/AgenticAI-D03-nonprod-shared-shared-gw-svc`,
        `arn:aws:iam::${WORKLOAD_ACCOUNT_IDS[0]}:role/AgenticAI-D03-nonprod-other-agent-gw-svc`,
      ]),
    ).toThrow(/exactly one (?:nonprod|prod) Gateway role ARN is required/);
    expect(() =>
      synth("nonprod", true, [
        `arn:aws:iam::${WORKLOAD_ACCOUNT_IDS[1]}:role/AgenticAI-D03-nonprod-shared-shared-gw-svc`,
        `arn:aws:iam::${WORKLOAD_ACCOUNT_IDS[0]}:role/AgenticAI-D03-prod-shared-shared-gw-svc`,
      ]),
    ).toThrow(/nonprod Gateway role must be owned by Workload account/);
  });

  it("fails closed when a Gateway role ARN has no current RoleId", () => {
    expect(() =>
      synth("nonprod", true, DEFAULT_GATEWAY_ROLE_ARNS, undefined, {
        [DEFAULT_GATEWAY_ROLE_ARNS[0]]: fakeRoleId("NONPROD1"),
      }),
    ).toThrow(
      /must map 'arn:aws:iam::\d{12}:role\/AgenticAI-D03-prod-shared-shared-gw-svc' to its current IAM RoleId/,
    );
    expect(() =>
      synth("nonprod", true, DEFAULT_GATEWAY_ROLE_ARNS, undefined, {
        ...DEFAULT_GATEWAY_ROLE_IDS,
        [DEFAULT_GATEWAY_ROLE_ARNS[0]]: "not-a-role-id",
      }),
    ).toThrow(/current IAM RoleId \(AROA\.\.\.\)/);
    expect(() =>
      synth("nonprod", true, DEFAULT_GATEWAY_ROLE_ARNS, undefined, {
        ...DEFAULT_GATEWAY_ROLE_IDS,
        [`arn:aws:iam::${WORKLOAD_ACCOUNT_IDS[0]}:role/AgenticAI-D03-nonprod-other-agent-gw-svc`]:
          fakeRoleId("UNRELATED1"),
      }),
    ).toThrow(/is not a supplied Gateway role ARN/);
    expect(() =>
      synth("nonprod", false, undefined, undefined, DEFAULT_GATEWAY_ROLE_IDS),
    ).toThrow(/role IDs must not be supplied before the permission phase/);
  });

  it("binds each alias permission to the Gateway role instance, not only its ARN", () => {
    // Live redeploy defect (2026-09-24): after teardown + redeploy the
    // Workstream role keeps its ARN but gets a new IAM RoleId; Lambda had
    // stored the old RoleId, so an ARN-only permission is a silent no-op
    // update and the recreated role is denied. A new RoleId must therefore
    // produce a new permission logical id (a replacement), while the same
    // RoleId must stay stable (no churn on ordinary redeploys).
    const permissionIds = (template: Template): string[] =>
      Object.keys(template.findResources("AWS::Lambda::Permission")).sort();
    const baseline = permissionIds(synth());
    expect(baseline).toHaveLength(Object.keys(PLATFORM_TOOL_CATALOGUE).length);
    expect(permissionIds(synth())).toEqual(baseline);

    const recreated = permissionIds(
      synth("nonprod", true, DEFAULT_GATEWAY_ROLE_ARNS, undefined, {
        ...DEFAULT_GATEWAY_ROLE_IDS,
        [DEFAULT_GATEWAY_ROLE_ARNS[0]]: fakeRoleId("NONPROD2"),
      }),
    );
    expect(recreated).toHaveLength(baseline.length);
    expect(recreated.filter((id) => baseline.includes(id))).toHaveLength(0);

    // The other environment's RoleId is irrelevant to this environment's aliases.
    const prodRotated = permissionIds(
      synth("nonprod", true, DEFAULT_GATEWAY_ROLE_ARNS, undefined, {
        ...DEFAULT_GATEWAY_ROLE_IDS,
        [DEFAULT_GATEWAY_ROLE_ARNS[1]]: fakeRoleId("PROD2"),
      }),
    );
    expect(prodRotated).toEqual(baseline);
  });

  it("creates environment-isolated pipeline-owned tools and exact permissions", () => {
    const template = synth();
    const functions = Object.values(
      template.findResources("AWS::Lambda::Function"),
    ) as any[];
    const functionNames = functions
      .map((fn) => fn.Properties.FunctionName)
      .filter((name): name is string => typeof name === "string")
      .sort();
    expect(functionNames).toEqual([
      "agenticai-platform-nonprod-tool-echo",
      "agenticai-platform-nonprod-tool-ping",
    ]);
    template.resourceCountIs("AWS::Lambda::Alias", 2);
    template.resourceCountIs("AWS::Logs::LogGroup", 2);
    template.resourceCountIs("AWS::KMS::Key", 2);
    const keys = Object.values(
      template.findResources("AWS::KMS::Key"),
    ) as any[];
    expect(keys.every((key) => key.Properties.EnableKeyRotation === true)).toBe(
      true,
    );
    const permissions = Object.values(
      template.findResources("AWS::Lambda::Permission"),
    ) as any[];
    expect(permissions).toHaveLength(
      Object.keys(PLATFORM_TOOL_CATALOGUE).length,
    );
    const principals = permissions.map((permission) =>
      JSON.stringify(permission.Properties.Principal),
    );
    const expected =
      `:iam::${WORKLOAD_ACCOUNT_IDS[0]}:role/` +
      "AgenticAI-D03-nonprod-shared-shared-gw-svc";
    expect(
      principals.filter((principal) => principal.includes(expected)),
    ).toHaveLength(2);
    expect(
      principals.some((principal) =>
        principal.includes("AgenticAI-D03-prod-shared-shared-gw-svc"),
      ),
    ).toBe(false);
  });

  it("names Platform tool roles inside the deployment boundary", () => {
    const roles = Object.values(
      synth("nonprod", false).findResources("AWS::IAM::Role"),
    ) as any[];
    for (const toolId of Object.keys(PLATFORM_TOOL_CATALOGUE)) {
      const role = roles.find(
        (candidate) =>
          candidate.Properties.RoleName ===
          `AgenticAI-Platform-nonprod-${toolId}-exec`,
      );
      expect(role).toBeDefined();
      expect(role.Properties.AssumeRolePolicyDocument.Statement).toContainEqual(
        expect.objectContaining({
          Action: "sts:AssumeRole",
          Effect: "Allow",
          Principal: { Service: "lambda.amazonaws.com" },
        }),
      );
      expect(role.Properties.ManagedPolicyArns).toBeUndefined();
      const statements = role.Properties.Policies[0].PolicyDocument.Statement;
      expect(statements).toHaveLength(1);
      expect(statements[0].Action).toEqual([
        "logs:CreateLogStream",
        "logs:PutLogEvents",
      ]);
      expect(JSON.stringify(statements[0].Resource)).toContain("LogGroup");
    }
  });

  it("does not emit the deprecated preview Registry custom resources", () => {
    const rendered = JSON.stringify(synth().toJSON());

    expect(rendered).not.toContain("Custom::BedrockAgentCoreRegistry");
    expect(rendered).not.toContain("Custom::BedrockAgentCoreRegistryRecord");
  });
});

describe("Phase 22 — Registry reader trust and permissions", () => {
  it("trusts only Workstream validators and the named Workload synth role", () => {
    const roles = synth().findResources("AWS::IAM::Role");
    const role = Object.values(roles).find(
      (candidate: any) =>
        candidate.Properties?.RoleName === "AgenticAI-RegistryReader-nonprod",
    ) as any;
    expect(role).toBeDefined();
    const statements = role.Properties.AssumeRolePolicyDocument.Statement;

    expect(role.Properties.RoleName).toBe("AgenticAI-RegistryReader-nonprod");
    expect(statements).toEqual([
      {
        Sid: "AllowWorkstreamRegistryValidators",
        Effect: "Allow",
        Principal: {
          AWS: WORKLOAD_ACCOUNT_IDS.map((accountId) =>
            partitionArn(`:iam::${accountId}:root`),
          ),
        },
        Action: "sts:AssumeRole",
        Condition: {
          StringEquals: {
            "sts:ExternalId": `agenticai-registry-v1-nonprod-${PLATFORM_ACCOUNT_ID}`,
          },
          StringLike: {
            "aws:PrincipalArn": WORKLOAD_ACCOUNT_IDS.map((accountId) =>
              partitionArn(
                `:iam::${accountId}:role/AgenticAI-D03-*-RegistryValidator`,
              ),
            ),
            "sts:RoleSessionName": "registry-*",
          },
        },
      },
      {
        Sid: "AllowWorkloadPipelineRegistryResolution",
        Effect: "Allow",
        Principal: {
          AWS: partitionArn(`:iam::${PLATFORM_ACCOUNT_ID}:root`),
        },
        Action: "sts:AssumeRole",
        Condition: {
          StringEquals: {
            "sts:ExternalId": `agenticai-registry-synth-v1-nonprod-${PLATFORM_ACCOUNT_ID}`,
          },
          ArnEquals: {
            "aws:PrincipalArn": partitionArn(
              `:iam::${PLATFORM_ACCOUNT_ID}:role/AgenticAI-WLP-shared-shared-RegistrySynth`,
            ),
          },
          StringLike: {
            "sts:RoleSessionName": "registry-synth-*",
          },
        },
      },
    ]);
    expect(tagsToRecord(role.Properties.Tags)).toEqual(REQUIRED_TAGS);
  });

  it("pins every GA read action to its required registry resource type", () => {
    const policy = singleResource(synth(), "AWS::IAM::ManagedPolicy");
    const statements = Object.fromEntries(
      policy.Properties.PolicyDocument.Statement.map((statement: any) => [
        statement.Sid,
        statement,
      ]),
    );
    const registryArn = {
      "Fn::GetAtt": ["GaRegistry07EC8B10", "RegistryArn"],
    };
    const recordArn = {
      "Fn::Join": ["", [registryArn, "/record/*"]],
    };

    expect(Object.keys(statements).sort()).toEqual([
      "DiscoverApprovedRegistryRecords",
      "ReadRegistryDiscoveryParameters",
      "ReadRegistryMetadata",
      "ReadRegistryRecords",
      "ReadRegistryTags",
    ]);
    expect(statements.ReadRegistryMetadata).toEqual({
      Sid: "ReadRegistryMetadata",
      Effect: "Allow",
      Action: [
        "agent-registry:GetRegistry",
        "agent-registry:ListRegistryRecords",
      ],
      Resource: registryArn,
    });
    expect(statements.ReadRegistryRecords).toEqual({
      Sid: "ReadRegistryRecords",
      Effect: "Allow",
      Action: [
        "agent-registry:GetRegistryRecord",
        "agent-registry:GetDiscoverableRegistryRecord",
      ],
      Resource: recordArn,
    });
    expect(statements.ReadRegistryTags).toEqual({
      Sid: "ReadRegistryTags",
      Effect: "Allow",
      Action: "agent-registry:ListTagsForResource",
      Resource: [registryArn, recordArn],
    });
    expect(statements.DiscoverApprovedRegistryRecords).toEqual({
      Sid: "DiscoverApprovedRegistryRecords",
      Effect: "Allow",
      Action: [
        "agent-registry:ListDiscoverableRegistryRecords",
        "agent-registry:SearchDiscoverableRegistryRecords",
      ],
      Resource: registryArn,
    });
    expect(statements.ReadRegistryDiscoveryParameters).toEqual({
      Sid: "ReadRegistryDiscoveryParameters",
      Effect: "Allow",
      Action: "ssm:GetParameters",
      Resource: partitionArn(
        `:ssm:us-west-2:${PLATFORM_ACCOUNT_ID}:parameter/agenticai/registry/v1/nonprod/*`,
      ),
    });

    const actions = policy.Properties.PolicyDocument.Statement.flatMap(
      (statement: any) => statement.Action,
    ).sort();
    expect(actions).toEqual(
      [
        "agent-registry:GetDiscoverableRegistryRecord",
        "agent-registry:GetRegistry",
        "agent-registry:GetRegistryRecord",
        "agent-registry:ListDiscoverableRegistryRecords",
        "agent-registry:ListRegistryRecords",
        "agent-registry:ListTagsForResource",
        "agent-registry:SearchDiscoverableRegistryRecords",
        "ssm:GetParameters",
      ].sort(),
    );
    const wildcardResources =
      policy.Properties.PolicyDocument.Statement.flatMap((statement: any) =>
        Array.isArray(statement.Resource)
          ? statement.Resource
          : [statement.Resource],
      )
        .filter((resource: any) => JSON.stringify(resource).includes("*"))
        .map((resource: any) => JSON.stringify(resource));
    expect(new Set(wildcardResources)).toEqual(
      new Set([
        JSON.stringify(recordArn),
        JSON.stringify(
          partitionArn(
            `:ssm:us-west-2:${PLATFORM_ACCOUNT_ID}:parameter/agenticai/registry/v1/nonprod/*`,
          ),
        ),
      ]),
    );
    expect(JSON.stringify(policy)).not.toContain("bedrock-agentcore:");
    expect(JSON.stringify(policy)).not.toContain(
      "BatchGetDiscoverableRegistryRecord",
    );
  });

  it("records SEC-030 for the constrained record and parameter wildcards", () => {
    const policy = singleResource(synth(), "AWS::IAM::ManagedPolicy");

    expect(policy.Metadata.cdk_nag.rules_to_suppress).toEqual([
      {
        id: "AwsSolutions-IAM5",
        reason: expect.stringMatching(
          /^SEC-030:.*both wildcards.*every action\/resource pair\.$/,
        ),
      },
    ]);
  });
});

describe("Phase 22 — versioned late-binding contract", () => {
  it("publishes Registry, reader, ExternalId, and every record ID through SSM", () => {
    const parameters = synth().findResources("AWS::SSM::Parameter");
    const names = Object.values(parameters)
      .map((parameter: any) => parameter.Properties.Name)
      .sort();
    const expectedRecordNames = Object.keys(PLATFORM_TOOL_CATALOGUE).map(
      (toolId) => `/agenticai/registry/v1/nonprod/records/${toolId}/id`,
    );

    expect(names).toEqual(
      [
        "/agenticai/registry/v1/nonprod/arn",
        "/agenticai/registry/v1/nonprod/id",
        "/agenticai/registry/v1/nonprod/reader-external-id",
        "/agenticai/registry/v1/nonprod/reader-role-arn",
        ...expectedRecordNames,
      ].sort(),
    );
    for (const parameter of Object.values(parameters) as Array<
      Record<string, any>
    >) {
      expect(tagsToRecord(parameter.Properties.Tags)).toEqual(REQUIRED_TAGS);
      expect(parameter.DeletionPolicy).toBe("RetainExceptOnCreate");
      expect(parameter.UpdateReplacePolicy).toBe("Retain");
    }
  });

  it("keeps nonprod and prod names distinct in a shared Platform account", () => {
    const nonprod = synth("nonprod");
    const prod = synth("prod");
    const nonprodRegistry = singleResource(
      nonprod,
      "AWS::AgentRegistry::Registry",
    );
    const prodRegistry = singleResource(prod, "AWS::AgentRegistry::Registry");
    const nonprodRole = Object.values(
      nonprod.findResources("AWS::IAM::Role"),
    ).find(
      (role: any) =>
        role.Properties?.RoleName === "AgenticAI-RegistryReader-nonprod",
    ) as any;
    const prodRole = Object.values(prod.findResources("AWS::IAM::Role")).find(
      (role: any) =>
        role.Properties?.RoleName === "AgenticAI-RegistryReader-prod",
    ) as any;
    expect(nonprodRole).toBeDefined();
    expect(prodRole).toBeDefined();
    const nonprodParameters = Object.values(
      nonprod.findResources("AWS::SSM::Parameter"),
    ).map((parameter: any) => parameter.Properties.Name);
    const prodParameters = Object.values(
      prod.findResources("AWS::SSM::Parameter"),
    ).map((parameter: any) => parameter.Properties.Name);

    expect(nonprodRegistry.Properties.Name).not.toBe(
      prodRegistry.Properties.Name,
    );
    expect(nonprodRole.Properties.RoleName).not.toBe(
      prodRole.Properties.RoleName,
    );
    expect(new Set([...nonprodParameters, ...prodParameters]).size).toBe(
      nonprodParameters.length + prodParameters.length,
    );
  });
});

describe("buildGaToolGovernanceDocument", () => {
  it("fails on invalid tools and resolves platform-owned target ARNs", () => {
    const source = PLATFORM_TOOL_CATALOGUE["tool-echo"];
    const governance = buildGaToolGovernanceDocument(
      source,
      PLATFORM_ACCOUNT_ID,
    );

    expect(governance.catalogueVersion).toBe("2");
    expect(governance.target.arn).toContain(`:${PLATFORM_ACCOUNT_ID}:`);
    expect(governance.target.arn).not.toContain("${PLATFORM_ACCOUNT_ID}");
    const override = `arn:aws:lambda:us-west-2:${PLATFORM_ACCOUNT_ID}:function:agenticai-platform-nonprod-tool-echo:PROD`;
    expect(
      buildGaToolGovernanceDocument(source, PLATFORM_ACCOUNT_ID, override)
        .target.arn,
    ).toBe(override);
    expect(() =>
      buildGaToolGovernanceDocument(
        { ...source, approvalStatus: "invalid" } as unknown as ToolSpec,
        PLATFORM_ACCOUNT_ID,
      ),
    ).toThrow(/approvalStatus/);
  });
});
