/*
 * Phase 23 conformance — opt-in pipeline-owned AgentCore Runtime + Memory.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";

import type { GaRegistryConsumerContext } from "@agenticai/agent-registry";
import { D03WorkstreamRegistryRolesStack } from "../../apps/workload-account/lib/d03-workstream-registry-roles-stack";
import { D03WorkstreamRuntimeMemoryStack } from "../../apps/workload-account/lib/d03-workstream-runtime-memory-stack";
import {
  WorkloadDeploymentStage,
  WorkloadPipelineStack,
  WorkstreamRegistryRolesStage,
} from "../../pipelines/workload-pipeline-stack";

const PLATFORM_ACCOUNT = "111111111111";
const NONPROD_ACCOUNT = "444444444444";
const PROD_ACCOUNT = "555555555555";
const REGION = "us-west-2";
const REQUIRED_TAGS = {
  "application-id": "demo",
  "agent-id": "primary",
  "tenant-id": "demo",
  "cost-centre": "engineering",
  environment: "nonprod",
};

function gaContext(environment: "nonprod" | "prod"): GaRegistryConsumerContext {
  const registryId =
    environment === "nonprod" ? "ABCDEFGHIJKLMNOP" : "QRSTUVWXYZABCDEF";
  const registryArn = `arn:aws:agent-registry:${REGION}:${PLATFORM_ACCOUNT}:registry/${registryId}`;
  return {
    schemaVersion: "agenticai.ga-registry-consumer-context/1.0",
    environment,
    region: REGION,
    platformAccountId: PLATFORM_ACCOUNT,
    sourceRevision: "a".repeat(40),
    registryId,
    registryArn,
    readerRoleArn: `arn:aws:iam::${PLATFORM_ACCOUNT}:role/AgenticAI-RegistryReader-${environment}`,
    readerExternalId: `agenticai-registry-v1-${environment}-${PLATFORM_ACCOUNT}`,
    records: [
      {
        recordId: "ABCDEFGHIJKL",
        recordArn: `${registryArn}/record/ABCDEFGHIJKL`,
        descriptorSha256: "1".repeat(64),
        document: {
          schemaVersion: "agenticai.tool-governance/1.0",
          catalogueVersion: "2",
          toolId: "tool-echo",
          description: "Echo tool",
          desiredApprovalStatus: "approved",
          target: {
            type: "lambda",
            arn: `arn:aws:lambda:${REGION}:${PLATFORM_ACCOUNT}:function:agenticai-platform-${environment}-tool-echo:PROD`,
          },
          mcp: {
            toolName: "tool-echo",
            description: "Echo tool",
            inputSchema: { type: "object" },
          },
          authorization: {
            defaultDecision: "DENY",
            cedarPolicy:
              'permit(principal, action, resource == Tool::"tool-echo");',
            allowedSubjects: [],
            allowedGroups: [],
            combination: "AUTHENTICATED",
          },
          ownership: { ownerTeam: "platform-ai", costCentre: "platform" },
        },
      },
    ],
  };
}

function tagsToRecord(
  tags: Array<{ Key: string; Value: string }> | Record<string, string>,
): Record<string, string> {
  return Array.isArray(tags)
    ? Object.fromEntries(tags.map(({ Key, Value }) => [Key, Value]))
    : tags;
}

function singleResource(template: Template, type: string): Record<string, any> {
  const resources = template.findResources(type);
  expect(Object.keys(resources)).toHaveLength(1);
  return Object.values(resources)[0] as Record<string, any>;
}

function runtimeMemoryTemplate(
  envName: "nonprod" | "prod" = "nonprod",
): Template {
  const app = new App();
  const account = envName === "nonprod" ? NONPROD_ACCOUNT : PROD_ACCOUNT;
  return Template.fromStack(
    new D03WorkstreamRuntimeMemoryStack(app, `RuntimeMemory-${envName}`, {
      env: { account, region: REGION },
      envName,
      applicationId: "demo",
      agentId: "primary",
      tenantId: "demo",
      costCentre: "engineering",
      runtimeExecutionRoleArnOverride: `arn:aws:iam::${account}:role/AgenticAI-D03-${envName}-demo-primary-runtime`,
    }),
  );
}

function roleTemplate(enabled: boolean): Template {
  const app = new App();
  return Template.fromStack(
    new D03WorkstreamRegistryRolesStack(app, `Roles-${enabled}`, {
      env: { account: NONPROD_ACCOUNT, region: REGION },
      envName: "nonprod",
      tenantId: "demo",
      agentId: "primary",
      applicationId: "demo",
      costCentre: "engineering",
      registryContext: gaContext("nonprod"),
      enablePipelineRuntimeMemory: enabled,
    }),
  );
}

function pipeline(enabled: boolean): {
  readonly stack: WorkloadPipelineStack;
  readonly template: Template;
} {
  const app = new App();
  const stack = new WorkloadPipelineStack(app, `Pipeline-${enabled}`, {
    env: { account: PLATFORM_ACCOUNT, region: REGION },
    githubRepo: "aws-samples/sample-ai-agent-factory",
    githubConnectionArn: `arn:aws:codestar-connections:${REGION}:${PLATFORM_ACCOUNT}:connection/example`,
    tenantId: "demo",
    agentId: "primary",
    applicationId: "demo",
    costCentre: "engineering",
    workloadNonprodEnv: { account: NONPROD_ACCOUNT, region: REGION },
    workloadProdEnv: { account: PROD_ACCOUNT, region: REGION },
    workloadNonprodAvailabilityZones: ["us-west-2a", "us-west-2b"],
    workloadProdAvailabilityZones: ["us-west-2a", "us-west-2b"],
    gaRegistry: {
      nonprod: gaContext("nonprod"),
      prod: gaContext("prod"),
      gatewayRegion: REGION,
    },
    enablePipelineRuntimeMemory: enabled,
  });
  return { stack, template: Template.fromStack(stack) };
}

describe("Phase 23 — native Runtime and Memory resources", () => {
  it("emits digest-resolved native resources with only the inert Memory environment", () => {
    const template = runtimeMemoryTemplate();
    const memory = singleResource(template, "AWS::BedrockAgentCore::Memory");
    const runtime = singleResource(template, "AWS::BedrockAgentCore::Runtime");

    expect(memory.Properties).toMatchObject({
      Name: "AgenticAI_D03_nonprod_demo_primary_memory",
      EventExpiryDuration: 30,
      Tags: REQUIRED_TAGS,
    });
    expect(memory.Properties.EncryptionKeyArn).toEqual({
      "Fn::GetAtt": [expect.any(String), "Arn"],
    });
    expect(memory.DeletionPolicy).toBe("Delete");
    expect(memory.UpdateReplacePolicy).toBe("Delete");

    expect(runtime.Properties).toMatchObject({
      AgentRuntimeName: "AgenticAI_D03_nonprod_demo_primary_runtime",
      NetworkConfiguration: { NetworkMode: "PUBLIC" },
      RoleArn: `arn:aws:iam::${NONPROD_ACCOUNT}:role/AgenticAI-D03-nonprod-demo-primary-runtime`,
      EnvironmentVariables: {
        AGENTCORE_MEMORY_ID: {
          "Fn::GetAtt": [expect.any(String), "MemoryId"],
        },
      },
      Tags: REQUIRED_TAGS,
    });
    expect(runtime.DeletionPolicy).toBe("Delete");
    expect(JSON.stringify(runtime.DependsOn)).toContain("Memory");

    const uri = JSON.stringify(
      runtime.Properties.AgentRuntimeArtifact.ContainerConfiguration
        .ContainerUri,
    );
    expect(uri).toContain("@");
    expect(uri).toContain("imageDetails.0.imageDigest");
    expect(uri).not.toContain('"imageTag"');
    template.resourceCountIs("AWS::BedrockAgentCore::RuntimeEndpoint", 0);

    const rendered = JSON.stringify(template.toJSON());
    expect(rendered).not.toContain("LLM_GATEWAY");
    expect(rendered).not.toContain("TOOL_GATEWAY");
    expect(rendered).not.toContain("ClientSecret");
  });

  it("uses a rotating five-tagged key with live-observed grant-operation constraints", () => {
    const template = runtimeMemoryTemplate();
    const key = singleResource(template, "AWS::KMS::Key");
    expect(key.Properties).toMatchObject({
      EnableKeyRotation: true,
      PendingWindowInDays: 7,
    });
    expect(tagsToRecord(key.Properties.Tags)).toEqual(REQUIRED_TAGS);
    expect(key.DeletionPolicy).toBe("Delete");

    const statements = key.Properties.KeyPolicy.Statement as any[];
    const crypto = statements.find(
      (statement) => statement.Sid === "AllowAgentCoreMemoryCrypto",
    );
    expect(crypto).toMatchObject({
      Principal: { Service: "bedrock-agentcore.amazonaws.com" },
      Condition: {
        StringEquals: { "aws:SourceAccount": NONPROD_ACCOUNT },
        ArnLike: {
          "aws:SourceArn": expect.stringContaining(
            "memory/AgenticAI_D03_nonprod_demo_primary_memory-*",
          ),
        },
      },
    });
    const grant = statements.find(
      (statement) => statement.Sid === "AllowAgentCoreMemoryCreateGrant",
    );
    expect(grant.Condition.Bool).toEqual({
      "kms:GrantIsForAWSResource": "true",
    });
    expect(
      grant.Condition["ForAllValues:StringEquals"]["kms:GrantOperations"],
    ).toEqual([
      "CreateGrant",
      "Decrypt",
      "DescribeKey",
      "GenerateDataKey",
      "GenerateDataKeyWithoutPlaintext",
      "ReEncryptFrom",
      "ReEncryptTo",
    ]);
  });

  it("always deletes Runtime and Memory while retaining only the production key", () => {
    const template = runtimeMemoryTemplate("prod");
    const memory = singleResource(template, "AWS::BedrockAgentCore::Memory");
    const runtime = singleResource(template, "AWS::BedrockAgentCore::Runtime");
    const key = singleResource(template, "AWS::KMS::Key");
    expect(memory.DeletionPolicy).toBe("Delete");
    expect(memory.UpdateReplacePolicy).toBe("Retain");
    expect(runtime.DeletionPolicy).toBe("Delete");
    expect(key.DeletionPolicy).toBe("Retain");
    expect(key.Properties.PendingWindowInDays).toBe(30);
  });

  it("rejects a Runtime role outside the exact prior-stage contract", () => {
    const app = new App();
    expect(
      () =>
        new D03WorkstreamRuntimeMemoryStack(app, "WrongRole", {
          env: { account: NONPROD_ACCOUNT, region: REGION },
          envName: "nonprod",
          applicationId: "demo",
          agentId: "primary",
          tenantId: "demo",
          costCentre: "engineering",
          runtimeExecutionRoleArnOverride: `arn:aws:iam::${NONPROD_ACCOUNT}:role/OtherRole`,
        }),
    ).toThrow(/must match the exact prior-stage role ARN/);
  });

  it("rejects invalid Memory retention", () => {
    const app = new App();
    expect(
      () =>
        new D03WorkstreamRuntimeMemoryStack(app, "Invalid", {
          env: { account: NONPROD_ACCOUNT, region: REGION },
          envName: "nonprod",
          applicationId: "demo",
          agentId: "primary",
          tenantId: "demo",
          costCentre: "engineering",
          eventExpiryDays: 2,
        }),
    ).toThrow(/integer from 3 through 365/);
  });
});

describe("Phase 23 — prior-stage Runtime role", () => {
  it("preserves the exact three-role R2 template while disabled", () => {
    const template = roleTemplate(false);
    template.resourceCountIs("AWS::IAM::Role", 3);
    expect(JSON.stringify(template.toJSON())).not.toContain(
      "RuntimeExecutionRole",
    );
  });

  it("adds one tagged, source-bound, least-privilege Runtime role when enabled", () => {
    const template = roleTemplate(true);
    template.resourceCountIs("AWS::IAM::Role", 4);
    const roleEntries = Object.entries(
      template.findResources("AWS::IAM::Role"),
    ) as Array<[string, any]>;
    const [runtimeRoleLogicalId, role] = roleEntries.find(
      ([, candidate]) =>
        candidate.Properties.RoleName ===
        "AgenticAI-D03-nonprod-demo-primary-runtime",
    )!;
    expect(role).toBeDefined();
    expect(tagsToRecord(role.Properties.Tags)).toEqual(REQUIRED_TAGS);
    expect(role.Properties.AssumeRolePolicyDocument.Statement[0]).toMatchObject(
      {
        Principal: { Service: "bedrock-agentcore.amazonaws.com" },
        Condition: {
          StringEquals: { "aws:SourceAccount": NONPROD_ACCOUNT },
          ArnLike: {
            "aws:SourceArn": expect.stringContaining(
              "runtime/AgenticAI_D03_nonprod_demo_primary_runtime-*",
            ),
          },
        },
      },
    );

    const statements = Object.values(template.findResources("AWS::IAM::Policy"))
      .filter((policy: any) =>
        JSON.stringify(policy.Properties.Roles).includes(runtimeRoleLogicalId),
      )
      .flatMap((policy: any) => policy.Properties.PolicyDocument.Statement);
    expect(statements).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          Sid: "PullAgentImage",
          Effect: "Allow",
          Resource: expect.stringContaining(
            `repository/cdk-hnb659fds-container-assets-${NONPROD_ACCOUNT}-${REGION}`,
          ),
        }),
        expect.objectContaining({
          Sid: "RuntimeLogControl",
          Effect: "Allow",
          Action: ["logs:DescribeLogGroups", "logs:PutResourcePolicy"],
          Resource: "*",
        }),
        expect.objectContaining({
          Sid: "DenyDirectBedrockInvoke",
          Effect: "Deny",
          Action: [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
          ],
          Resource: "*",
        }),
      ]),
    );
    expect(
      statements.some(
        (statement: any) =>
          statement.Effect === "Allow" &&
          JSON.stringify(statement.Action).includes("bedrock:InvokeModel"),
      ),
    ).toBe(false);
    expect(JSON.stringify(statements)).not.toContain("bedrock:Converse");
    template.hasOutput("RuntimeExecutionRoleArn", {});
  });
});

describe("Phase 23 — opt-in pipeline graph", () => {
  it("keeps native resources and ARM asset publishing absent by default", () => {
    const { stack, template } = pipeline(false);
    for (const stageName of ["Nonprod", "Prod"]) {
      const stage = stack.node.findChild(stageName) as WorkloadDeploymentStage;
      expect(stage.runtimeMemoryStack).toBeUndefined();
    }
    const rolesStage = stack.node.findChild(
      "RegistryRoles",
    ) as WorkstreamRegistryRolesStage;
    expect(
      Template.fromStack(rolesStage.nonprod).findResources(
        "AWS::BedrockAgentCore::Runtime",
      ),
    ).toEqual({});
    expect(JSON.stringify(template.toJSON())).not.toContain(
      "RuntimeRolePropagation",
    );
    expect(JSON.stringify(template.toJSON())).not.toContain(
      "amazonlinux-aarch64-standard:4.0",
    );
  });

  it("adds isolated RuntimeMemory stacks after ToolGateway and native ARM asset publishing", () => {
    const { stack, template } = pipeline(true);
    for (const stageName of ["Nonprod", "Prod"] as const) {
      const stage = stack.node.findChild(stageName) as WorkloadDeploymentStage;
      expect(stage.runtimeMemoryStack?.stackName).toBe(
        `AgenticAI-demo-primary-${stageName.toLowerCase()}-RuntimeMemory`,
      );
      expect(stage.runtimeMemoryStack?.dependencies).toContain(
        stage.gatewayStack,
      );
      Template.fromStack(stage.runtimeMemoryStack!).resourceCountIs(
        "AWS::BedrockAgentCore::Runtime",
        1,
      );
      Template.fromStack(stage.runtimeMemoryStack!).resourceCountIs(
        "AWS::BedrockAgentCore::Memory",
        1,
      );
    }

    const rolesStage = stack.node.findChild(
      "RegistryRoles",
    ) as WorkstreamRegistryRolesStage;
    Template.fromStack(rolesStage.nonprod).resourceCountIs("AWS::IAM::Role", 4);
    Template.fromStack(rolesStage.prod).resourceCountIs("AWS::IAM::Role", 4);

    const root = template.toJSON();
    const projects = Object.values(
      template.findResources("AWS::CodeBuild::Project"),
    ) as any[];
    const dockerAssets = projects.find((project) =>
      project.Properties.Description?.endsWith("Assets/DockerAssets"),
    );
    const fileAssets = projects.find((project) =>
      project.Properties.Description?.endsWith("Assets/FileAssets"),
    );
    for (const project of [dockerAssets, fileAssets]) {
      expect(project.Properties.Environment).toMatchObject({
        Type: "ARM_CONTAINER",
        Image: "aws/codebuild/amazonlinux-aarch64-standard:4.0",
      });
    }
    expect(dockerAssets.Properties.Environment.PrivilegedMode).toBe(true);
    expect(fileAssets.Properties.Environment.PrivilegedMode).toBe(false);
    expect(JSON.stringify(root)).toContain("RuntimeRolePropagation");
    expect(JSON.stringify(root)).toContain("sleep 360");
    expect(JSON.stringify(root)).toContain(
      "agenticai/enablePipelineRuntimeMemory",
    );

    const codePipeline = Object.values(
      template.findResources("AWS::CodePipeline::Pipeline"),
    )[0] as any;
    const rolesPipelineStage = codePipeline.Properties.Stages.find(
      (stage: any) => stage.Name === "RegistryRoles",
    );
    const permissionReady = rolesPipelineStage.Actions.find(
      (action: any) => action.Name === "GatewayPermissionReady",
    );
    const rolePropagation = rolesPipelineStage.Actions.find(
      (action: any) => action.Name === "RuntimeRolePropagation",
    );
    expect(rolePropagation.RunOrder).toBeGreaterThan(permissionReady.RunOrder);
  });

  it("rejects Runtime/Memory pipeline configuration without GA Registry mode", () => {
    const app = new App();
    expect(
      () =>
        new WorkloadPipelineStack(app, "InvalidPipeline", {
          env: { account: PLATFORM_ACCOUNT, region: REGION },
          githubRepo: "aws-samples/sample-ai-agent-factory",
          githubConnectionArn: `arn:aws:codestar-connections:${REGION}:${PLATFORM_ACCOUNT}:connection/example`,
          tenantId: "demo",
          agentId: "primary",
          costCentre: "engineering",
          workloadNonprodEnv: { account: NONPROD_ACCOUNT, region: REGION },
          workloadProdEnv: { account: PROD_ACCOUNT, region: REGION },
          workloadNonprodAvailabilityZones: ["us-west-2a", "us-west-2b"],
          workloadProdAvailabilityZones: ["us-west-2a", "us-west-2b"],
          enablePipelineRuntimeMemory: true,
        }),
    ).toThrow(/requires GA Registry mode/);
  });
});
