/*
 * Phase 23 conformance — opt-in pipeline-owned AgentCore Runtime + Memory.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";

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
  agentImageVariant?: "compatibility" | "generated-agent",
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
      agentImageVariant,
      generatedAgentRuntimeConfig:
        agentImageVariant === "generated-agent"
          ? {
              mcpGatewayUrl: `https://gw-${envName}.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp`,
              inferenceGatewayUrl: `https://inf-${envName}.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp`,
              modelId: `agenticai-inference-${envName}-bedrock/openai.gpt-oss-120b`,
              guardrailId: `arn:aws:bedrock:${REGION}:${PLATFORM_ACCOUNT}:guardrail/example`,
              subscribedTools: [
                "target-tool-echo___echo",
                "target-tool-ping___ping",
              ],
              inferenceScope: `agenticai-inference-${envName}-api/invoke`,
              m2mSecretArn: `arn:aws:secretsmanager:${REGION}:${PLATFORM_ACCOUNT}:secret:agenticai/inference-m2m/agenticai-inference-${envName}-abc`,
            }
          : undefined,
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

function roleTemplateWithGrants(): Template {
  const app = new App();
  return Template.fromStack(
    new D03WorkstreamRegistryRolesStack(app, "Roles-grants", {
      env: { account: NONPROD_ACCOUNT, region: REGION },
      envName: "nonprod",
      tenantId: "demo",
      agentId: "primary",
      applicationId: "demo",
      costCentre: "engineering",
      registryContext: gaContext("nonprod"),
      enablePipelineRuntimeMemory: true,
      generatedAgentGrants: {
        m2mSecretArn: `arn:aws:secretsmanager:${REGION}:${PLATFORM_ACCOUNT}:secret:agenticai/inference-m2m/agenticai-inference-nonprod-abc`,
        credentialProviderName: "AgenticAI-D03-nonprod-demo-primary-inference",
        workloadIdentityName: "AgenticAI-D03-nonprod-demo-primary",
      },
    }),
  );
}

function pipeline(
  enabled: boolean,
  generated = false,
): {
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
    agentImageVariant: generated ? "generated-agent" : undefined,
    generatedAgentInference: generated
      ? {
          nonprod: {
            inferenceGatewayUrl: `https://inf-nonprod.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp`,
            inferenceScope: "agenticai-inference-nonprod-api/invoke",
            modelId: "agenticai-inference-nonprod-bedrock/openai.gpt-oss-120b",
            guardrailId: `arn:aws:bedrock:${REGION}:${PLATFORM_ACCOUNT}:guardrail/nonprod`,
            m2mSecretArn: `arn:aws:secretsmanager:${REGION}:${PLATFORM_ACCOUNT}:secret:agenticai/inference-m2m/nonprod`,
          },
          prod: {
            inferenceGatewayUrl: `https://inf-prod.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp`,
            inferenceScope: "agenticai-inference-prod-api/invoke",
            modelId: "agenticai-inference-prod-bedrock/openai.gpt-oss-120b",
            guardrailId: `arn:aws:bedrock:${REGION}:${PLATFORM_ACCOUNT}:guardrail/prod`,
            m2mSecretArn: `arn:aws:secretsmanager:${REGION}:${PLATFORM_ACCOUNT}:secret:agenticai/inference-m2m/prod`,
          },
        }
      : undefined,
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
    // The digest now arrives from the fail-closed scan gate, not from a bare
    // DescribeImages response field.
    expect(uri).toContain('"ImageDigest"');
    expect(uri).not.toContain("imageDetails.0.imageDigest");
    expect(uri).not.toContain('"imageTag"');
    template.resourceCountIs("AWS::BedrockAgentCore::RuntimeEndpoint", 0);

    const rendered = JSON.stringify(template.toJSON());
    expect(rendered).not.toContain("LLM_GATEWAY");
    expect(rendered).not.toContain("TOOL_GATEWAY");
    expect(rendered).not.toContain("ClientSecret");
  });

  it("agentImageVariant selects a distinct image asset and defaults to compatibility", () => {
    // The two variants build from different source directories, so their
    // DockerImageAsset hashes differ, which changes the Runtime ContainerUri /
    // asset references in the rendered template. The default and the explicit
    // 'compatibility' variant must render identically (back-compat, 442de00).
    const norm = (t: Template): string =>
      JSON.stringify(t.toJSON()).replace(/RuntimeMemory-nonprod/g, "S");

    const compat = norm(runtimeMemoryTemplate("nonprod", "compatibility"));
    const generated = norm(runtimeMemoryTemplate("nonprod", "generated-agent"));
    const defaulted = norm(runtimeMemoryTemplate("nonprod"));

    // Distinct image content => distinct rendered template.
    expect(generated).not.toEqual(compat);
    // Default must equal the compatibility variant.
    expect(defaulted).toEqual(compat);
  });

  it("compatibility variant sets only AGENTCORE_MEMORY_ID on the Runtime", () => {
    const template = runtimeMemoryTemplate("nonprod", "compatibility");
    const runtime = singleResource(template, "AWS::BedrockAgentCore::Runtime");
    const env = runtime.Properties.EnvironmentVariables as Record<
      string,
      unknown
    >;
    expect(Object.keys(env)).toEqual(["AGENTCORE_MEMORY_ID"]);
  });

  it("generated-agent variant wires the full LiteLLM/MCP/tenant env contract", () => {
    const template = runtimeMemoryTemplate("nonprod", "generated-agent");
    const runtime = singleResource(template, "AWS::BedrockAgentCore::Runtime");
    const env = runtime.Properties.EnvironmentVariables as Record<
      string,
      unknown
    >;
    // Every var the container entrypoint reads must be present.
    for (const key of [
      "AGENTCORE_MEMORY_ID",
      "AGENTCORE_TENANT_ID",
      "AGENTCORE_AGENT_ID",
      "AGENTCORE_ENV_NAME",
      "AGENTCORE_GUARDRAIL_ID",
      "AGENTCORE_MODEL_ID",
      "AGENTCORE_GATEWAY_URL",
      "AGENTCORE_INFERENCE_GATEWAY_URL",
      "AGENTCORE_SUBSCRIBED_TOOLS",
      "AGENTCORE_WORKLOAD_IDENTITY_NAME",
    ]) {
      expect(env).toHaveProperty(key);
    }
    expect(env.AGENTCORE_TENANT_ID).toBe("demo");
    expect(env.AGENTCORE_AGENT_ID).toBe("primary");
    expect(env.AGENTCORE_ENV_NAME).toBe("nonprod");
    // MCP (tools) and inference URLs are distinct Gateways, not the same value.
    expect(env.AGENTCORE_GATEWAY_URL).not.toEqual(
      env.AGENTCORE_INFERENCE_GATEWAY_URL,
    );
    expect(String(env.AGENTCORE_GATEWAY_URL)).toMatch(/\/mcp$/);
    expect(env.AGENTCORE_SUBSCRIBED_TOOLS).toBe(
      "target-tool-echo___echo,target-tool-ping___ping",
    );
    expect(env.AGENTCORE_WORKLOAD_IDENTITY_NAME).toBe(
      "AgenticAI_D03_nonprod_demo_primary",
    );
    const rendered = JSON.stringify(template.toJSON());
    expect(rendered).toContain("get_oauth2_credential_provider");
    expect(rendered).toContain("did not reach READY within 250 seconds");
  });

  it("initializes only the default token vault and tags Identity resources", () => {
    const template = runtimeMemoryTemplate("nonprod", "generated-agent");
    const role = Object.values(template.findResources("AWS::IAM::Role")).find(
      (resource: any) =>
        resource.Properties.RoleName ===
        "AgenticAI-D03-nonprod-demo-primary-idprov",
    ) as any;
    expect(role).toBeDefined();
    const statements = role.Properties.Policies.flatMap(
      (policy: any) => policy.PolicyDocument.Statement,
    );
    expect(statements).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          Sid: "InitializeDefaultTokenVault",
          Effect: "Allow",
          Action: "bedrock-agentcore:CreateTokenVault",
        }),
      ]),
    );
    const tokenVaultStatement = statements.find(
      (statement: any) => statement.Sid === "InitializeDefaultTokenVault",
    );
    // Partition-safe ARN renders as Fn::Join; assert the exact immutable suffix
    // and that it is the single default vault (no wildcard).
    const tokenVaultResourceJson = JSON.stringify(tokenVaultStatement.Resource);
    expect(tokenVaultResourceJson).toContain(
      `:bedrock-agentcore:${REGION}:${NONPROD_ACCOUNT}:token-vault/default"`,
    );
    expect(tokenVaultResourceJson).not.toContain("*");

    // Every bedrock-agentcore call the handler makes (create/get/update/delete,
    // tag, list-tags) is scoped to the modeled parent containers and the two
    // deterministic families (service reference: CreateWorkloadIdentity ->
    // workload-identity + workload-identity-directory; CreateOauth2Credential-
    // Provider -> oauth2credentialprovider + token-vault). Live-proven three
    // times on 2026-09-23; pin the exact action set and resources so it can
    // never widen to "*" nor silently drop a call the handler depends on.
    const lifecycleStatement = statements.find(
      (statement: any) => statement.Sid === "ManageIdentityAndProvider",
    );
    expect(lifecycleStatement).toBeDefined();
    expect(lifecycleStatement.Effect).toBe("Allow");
    expect([...lifecycleStatement.Action].sort()).toEqual(
      [
        "bedrock-agentcore:CreateOauth2CredentialProvider",
        "bedrock-agentcore:CreateWorkloadIdentity",
        "bedrock-agentcore:DeleteOauth2CredentialProvider",
        "bedrock-agentcore:DeleteWorkloadIdentity",
        "bedrock-agentcore:GetOauth2CredentialProvider",
        "bedrock-agentcore:GetWorkloadIdentity",
        "bedrock-agentcore:ListTagsForResource",
        "bedrock-agentcore:TagResource",
        "bedrock-agentcore:UpdateOauth2CredentialProvider",
      ].sort(),
    );
    const lifecycleResources = lifecycleStatement.Resource;
    expect(Array.isArray(lifecycleResources)).toBe(true);
    expect(lifecycleResources).toHaveLength(4);
    const lifecycleResourcesJson = JSON.stringify(lifecycleResources);
    for (const suffix of [
      `:bedrock-agentcore:${REGION}:${NONPROD_ACCOUNT}:workload-identity-directory/default"`,
      `:bedrock-agentcore:${REGION}:${NONPROD_ACCOUNT}:workload-identity-directory/default/workload-identity/*`,
      `:bedrock-agentcore:${REGION}:${NONPROD_ACCOUNT}:token-vault/default"`,
      `:bedrock-agentcore:${REGION}:${NONPROD_ACCOUNT}:token-vault/default/oauth2credentialprovider/*`,
    ]) {
      expect(lifecycleResourcesJson).toContain(suffix);
    }
    expect(lifecycleResourcesJson).not.toMatch(/"\*"/);
    // No statement in the provider role may carry a bare "*" resource.
    for (const statement of statements) {
      const resources = Array.isArray(statement.Resource)
        ? statement.Resource
        : [statement.Resource];
      if (statement.Sid === "DecryptPlatformM2mSecret") continue; // ViaService + context bound
      expect(resources).not.toContain("*");
    }

    const credentialProviderResource = Object.values(
      template.findResources("AWS::CloudFormation::CustomResource"),
    ).find(
      (resource: any) =>
        resource.Properties.ProviderName ===
        "AgenticAI_D03_nonprod_demo_primary_inference",
    ) as any;
    expect(credentialProviderResource).toBeDefined();
    expect(credentialProviderResource.Properties.Tags).toEqual(REQUIRED_TAGS);
    const providerArnJson = JSON.stringify(
      credentialProviderResource.Properties.ProviderArn,
    );
    expect(providerArnJson).toContain("AWS::Partition");
    expect(providerArnJson).toContain(
      `:bedrock-agentcore:${REGION}:${NONPROD_ACCOUNT}:token-vault/default/oauth2credentialprovider/AgenticAI_D03_nonprod_demo_primary_inference`,
    );
    const workloadArnJson = JSON.stringify(
      credentialProviderResource.Properties.WorkloadArn,
    );
    expect(workloadArnJson).toContain("AWS::Partition");
    expect(workloadArnJson).toContain(
      `:bedrock-agentcore:${REGION}:${NONPROD_ACCOUNT}:workload-identity-directory/default/workload-identity/AgenticAI_D03_nonprod_demo_primary`,
    );
    const handlerCode = JSON.stringify(template.toJSON());
    expect(handlerCode).toContain(
      "create_workload_identity(name=workload_name, tags=tags)",
    );
    expect(handlerCode).toContain(
      'code == \\"ValidationException\\" and \\"already exists\\" in message',
    );
    // Recovery of the exact zero-tag partial create is delete + recreate with
    // create-time tags (TagResource on an existing WorkloadIdentity returns a
    // deterministic service 500 -- live-proven 2026-09-23), and foreign or
    // partial tags remain a hard refusal. The handler must not tag in place.
    expect(handlerCode).toContain(
      "_recover_untagged_workload(client, workload_name, workload_arn, tags)",
    );
    expect(handlerCode).toContain("client.delete_workload_identity(name=name)");
    expect(handlerCode).toContain(
      "Refusing workload identity with foreign or partial ownership tags",
    );
    expect(handlerCode).toContain(
      "Refusing resource with missing or foreign ownership tags",
    );
    expect(handlerCode).not.toContain("allow_untagged");
    expect(handlerCode).not.toContain("client.tag_resource(");
    expect(handlerCode).toContain("oauth2ProviderConfigInput=provider_config");
    expect(handlerCode).toContain("tags=tags");
  });

  it("generated-agent variant fails closed when its runtime config is absent", () => {
    const app = new App();
    expect(
      () =>
        new D03WorkstreamRuntimeMemoryStack(app, "RuntimeMemory-missing-cfg", {
          env: { account: NONPROD_ACCOUNT, region: REGION },
          envName: "nonprod",
          applicationId: "demo",
          agentId: "primary",
          tenantId: "demo",
          costCentre: "engineering",
          runtimeExecutionRoleArnOverride: `arn:aws:iam::${NONPROD_ACCOUNT}:role/AgenticAI-D03-nonprod-demo-primary-runtime`,
          agentImageVariant: "generated-agent",
          // generatedAgentRuntimeConfig deliberately omitted
        }),
    ).toThrow(/generatedAgentRuntimeConfig is required/);
  });

  it("generated-agent variant fails closed on empty subscribedTools", () => {
    const app = new App();
    expect(
      () =>
        new D03WorkstreamRuntimeMemoryStack(app, "RuntimeMemory-empty-tools", {
          env: { account: NONPROD_ACCOUNT, region: REGION },
          envName: "nonprod",
          applicationId: "demo",
          agentId: "primary",
          tenantId: "demo",
          costCentre: "engineering",
          runtimeExecutionRoleArnOverride: `arn:aws:iam::${NONPROD_ACCOUNT}:role/AgenticAI-D03-nonprod-demo-primary-runtime`,
          agentImageVariant: "generated-agent",
          generatedAgentRuntimeConfig: {
            mcpGatewayUrl: `https://gw.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp`,
            inferenceGatewayUrl: `https://inf.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp`,
            modelId: "agenticai-inference-nonprod-bedrock/openai.gpt-oss-120b",
            guardrailId: `arn:aws:bedrock:${REGION}:${PLATFORM_ACCOUNT}:guardrail/example`,
            subscribedTools: [],
            inferenceScope: "agenticai-inference-nonprod-api/invoke",
            m2mSecretArn: `arn:aws:secretsmanager:${REGION}:${PLATFORM_ACCOUNT}:secret:agenticai/inference-m2m/x`,
          },
        }),
    ).toThrow(/subscribedTools must list at least one/);
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

describe("Phase 23 — fail-closed image scan preflight gate", () => {
  const GATE_TYPE = "Custom::AgenticAIAgentImageScanGate";
  const BOOTSTRAP_REPO = `cdk-hnb659fds-container-assets-${NONPROD_ACCOUNT}-${REGION}`;

  function gateHandlerSource(template: Template): string {
    const functions = Object.values(
      template.findResources("AWS::Lambda::Function"),
    ) as any[];
    const sources = functions
      .filter((fn) =>
        ["index.on_event", "index.is_complete"].includes(fn.Properties.Handler),
      )
      .map((fn) => fn.Properties.Code.ZipFile as string);
    // Both waiter halves are the SAME module; a divergence is itself a defect.
    expect(sources).toHaveLength(2);
    expect(sources[0]).toBe(sources[1]);
    return sources[0];
  }

  it("synthesizes valid Python and hashes the full handler policy into the gate", () => {
    const template = runtimeMemoryTemplate();
    const handler = gateHandlerSource(template);
    const parsed = spawnSync(
      "python3",
      ["-c", "import ast,sys; ast.parse(sys.stdin.read())"],
      { input: handler, encoding: "utf8" },
    );
    expect(parsed.stderr).toBe("");
    expect(parsed.status).toBe(0);

    const onEvent = Object.values(
      template.findResources("AWS::Lambda::Function"),
    ).find((fn: any) => fn.Properties.Handler === "index.on_event") as any;
    const expectedContractHash = createHash("sha256")
      .update(handler)
      .update("\0")
      .update(JSON.stringify(onEvent.Properties.Environment.Variables))
      .digest("hex");
    const gate = singleResource(template, GATE_TYPE);
    expect(gate.Properties.GateContractSha256).toBe(expectedContractHash);
  });

  it("applies all five allocation tags to every taggable support resource", () => {
    const template = runtimeMemoryTemplate();
    for (const type of [
      "AWS::IAM::Role",
      "AWS::Lambda::Function",
      "AWS::StepFunctions::StateMachine",
    ]) {
      const resources = Object.values(template.findResources(type)) as any[];
      expect(resources.length).toBeGreaterThan(0);
      for (const resource of resources) {
        expect(tagsToRecord(resource.Properties.Tags)).toEqual(REQUIRED_TAGS);
      }
    }
  });

  it("feeds the Runtime only a digest resolved through the completed gate", () => {
    const template = runtimeMemoryTemplate();
    template.resourceCountIs(GATE_TYPE, 1);
    const gate = singleResource(template, GATE_TYPE);
    const gateLogicalId = Object.keys(template.findResources(GATE_TYPE))[0];
    const runtime = singleResource(template, "AWS::BedrockAgentCore::Runtime");

    // The gate resolves the exact immutable asset tag, not a floating one.
    expect(gate.Properties).toMatchObject({
      RegistryId: NONPROD_ACCOUNT,
      RepositoryName: BOOTSTRAP_REPO,
      ImageTag: expect.stringMatching(/^[0-9a-f]{64}$/),
      AssetHash: expect.stringMatching(/^[0-9a-f]{64}$/),
      GateContractSha256: expect.stringMatching(/^[0-9a-f]{64}$/),
    });
    expect(gate.Properties.ImageTag).toBe(gate.Properties.AssetHash);

    // Only `<repositoryUri>@<digest>` reaches CfnRuntime — never a tag.
    const containerUri =
      runtime.Properties.AgentRuntimeArtifact.ContainerConfiguration
        .ContainerUri;
    expect(containerUri["Fn::Join"][1]).toEqual([
      `${NONPROD_ACCOUNT}.dkr.ecr.${REGION}.`,
      { Ref: "AWS::URLSuffix" },
      `/${BOOTSTRAP_REPO}@`,
      { "Fn::GetAtt": [gateLogicalId, "ImageDigest"] },
    ]);
    const rendered = JSON.stringify(containerUri);
    expect(rendered).not.toContain("imageTag");
    expect(rendered).not.toContain(gate.Properties.ImageTag);

    // Runtime creation is explicitly ordered after the completed gate.
    expect(runtime.DependsOn).toContain(gateLogicalId);
  });

  it("scopes the gate role to exactly three ECR actions on the exact bootstrap repository", () => {
    const template = runtimeMemoryTemplate();
    const role = Object.values(template.findResources("AWS::IAM::Role")).find(
      (candidate: any) =>
        candidate.Properties.RoleName ===
        "AgenticAI-D03-nonprod-demo-primary-imgscan",
    ) as any;
    expect(role).toBeDefined();
    expect(tagsToRecord(role.Properties.Tags)).toEqual(REQUIRED_TAGS);
    expect(role.Properties.AssumeRolePolicyDocument.Statement[0]).toMatchObject(
      { Principal: { Service: "lambda.amazonaws.com" } },
    );
    expect(role.Properties.Policies).toEqual([
      {
        PolicyName: "ReadBootstrapImageScan",
        PolicyDocument: {
          Version: "2012-10-17",
          Statement: [
            {
              Sid: "PreflightAgentImageScan",
              Effect: "Allow",
              // Exact set — DescribeImages resolves the tag, StartImageScan
              // starts the basic scan, DescribeImageScanFindings polls it.
              Action: [
                "ecr:DescribeImageScanFindings",
                "ecr:DescribeImages",
                "ecr:StartImageScan",
              ],
              Resource: {
                "Fn::Join": [
                  "",
                  [
                    "arn:",
                    { Ref: "AWS::Partition" },
                    `:ecr:${REGION}:${NONPROD_ACCOUNT}:repository/${BOOTSTRAP_REPO}`,
                  ],
                ],
              },
            },
          ],
        },
      },
    ]);
  });

  it("never grants or performs destruction of the shared bootstrap assets", () => {
    const template = runtimeMemoryTemplate();
    // The bootstrap repository is imported, never templated — so stack delete
    // cannot take it or its images with it.
    template.resourceCountIs("AWS::ECR::Repository", 0);

    const rendered = JSON.stringify(template.toJSON());
    for (const forbidden of [
      "ecr:BatchDeleteImage",
      "ecr:DeleteRepository",
      "ecr:DeleteRepositoryPolicy",
      "ecr:PutImage",
      "ecr:PutImageScanningConfiguration",
      "ecr:BatchDeleteImageScanFindings",
    ]) {
      expect(rendered).not.toContain(forbidden);
    }

    // Delete is a pure no-op: it returns the inbound physical id and makes no
    // ECR call at all (no client is even constructed on that branch).
    const handler = gateHandlerSource(template);
    expect(handler).toContain(
      'if request_type == "Delete":\n        # Shared bootstrap repository and images are NEVER deleted here.\n        return {"PhysicalResourceId": event.get("PhysicalResourceId")}',
    );
    expect(handler).toContain(
      'if event.get("RequestType") == "Delete":\n        return {"IsComplete": True}',
    );
  });

  it("bounds the waiter to a fixed interval and a hard attempt ceiling", () => {
    const template = runtimeMemoryTemplate();
    const machine = singleResource(
      template,
      "AWS::StepFunctions::StateMachine",
    );
    const definition = JSON.stringify(machine.Properties.DefinitionString);
    // 15 s polls x 120 attempts == the declared 30 min ceiling, then the
    // framework's onTimeout catch reports FAILED to CloudFormation.
    expect(definition).toContain('\\"IntervalSeconds\\":15');
    expect(definition).toContain('\\"MaxAttempts\\":120');
    expect(definition).toContain('\\"BackoffRate\\":1');
    expect(definition).toContain("framework-onTimeout-task");

    // Each invocation is also individually bounded on the SDK side. The
    // onEvent handler can make two sequential ECR calls, while isComplete
    // makes one; both Lambda timeouts enclose those budgets.
    const gateFunctions = Object.values(
      template.findResources("AWS::Lambda::Function"),
    ) as any[];
    const onEvent = gateFunctions.find(
      (fn) => fn.Properties.Handler === "index.on_event",
    ) as any;
    const isComplete = gateFunctions.find(
      (fn) => fn.Properties.Handler === "index.is_complete",
    ) as any;
    expect(onEvent.Properties.Runtime).toBe("python3.13");
    expect(onEvent.Properties.Timeout).toBe(120);
    expect(isComplete.Properties.Timeout).toBe(60);
    expect(onEvent.Properties.Environment.Variables).toMatchObject({
      SDK_TOTAL_MAX_ATTEMPTS: "2",
      SDK_CONNECT_TIMEOUT_SECONDS: "3",
      SDK_READ_TIMEOUT_SECONDS: "10",
    });
    const handler = gateHandlerSource(template);
    expect(handler).toContain('"total_max_attempts": int(');
    expect(handler).not.toContain('"max_attempts": int(');
    expect(handler).toContain('"mode": "standard"');
    // No unbounded loop lives inside the handler; the waiter owns iteration.
    expect(handler).not.toMatch(/\bwhile\b/);
    expect(handler).not.toContain("time.sleep");
  });

  it("pins the exact ECR scan-status and severity vocabulary into both halves", () => {
    const template = runtimeMemoryTemplate();
    const gateFunctions = Object.values(
      template.findResources("AWS::Lambda::Function"),
    ).filter((fn: any) =>
      ["index.on_event", "index.is_complete"].includes(fn.Properties.Handler),
    ) as any[];
    expect(gateFunctions).toHaveLength(2);
    for (const fn of gateFunctions) {
      // Verbatim from the pinned botocore ecr/2015-09-21 `ScanStatus` and
      // `FindingSeverity` shapes.
      expect(fn.Properties.Environment.Variables).toMatchObject({
        USABLE_SCAN_STATUSES: "COMPLETE,ACTIVE",
        PENDING_SCAN_STATUSES: "IN_PROGRESS,PENDING",
        TERMINAL_SCAN_STATUSES:
          "FAILED,UNSUPPORTED_IMAGE,SCAN_ELIGIBILITY_EXPIRED,FINDINGS_UNAVAILABLE,LIMIT_EXCEEDED,IMAGE_ARCHIVED",
        BLOCKING_SCAN_SEVERITIES: "CRITICAL,HIGH",
      });
    }
  });

  it("refuses Runtime creation when CRITICAL or HIGH counts are nonzero", () => {
    const handler = gateHandlerSource(runtimeMemoryTemplate());
    // The count is read from the exact modeled response path.
    expect(handler).toContain(
      'counts = (response.get("imageScanFindings") or {}).get("findingSeverityCounts") or {}',
    );
    expect(handler).toContain(
      'for severity in sorted(_status_set("BLOCKING_SCAN_SEVERITIES")):',
    );
    expect(handler).toContain("observed = int(counts.get(severity) or 0)");
    expect(handler).toContain("if observed > 0:");
    // ...and any nonzero count raises BEFORE IsComplete can ever be returned.
    expect(handler).toContain(
      "    blocking = blocking_findings(response)\n    if blocking:\n        _deny(",
    );
    expect(handler).toContain('"refusing Runtime creation for digest "');
    const successIndex = handler.indexOf(
      'return {\n        "IsComplete": True',
    );
    expect(successIndex).toBeGreaterThan(handler.indexOf("if blocking:"));
  });

  it("fails closed on terminal, unknown, and non-usable scan statuses", () => {
    const handler = gateHandlerSource(runtimeMemoryTemplate());
    // Anything outside the three declared vocabularies is UNKNOWN, never
    // silently treated as pending or usable.
    expect(handler).toContain('    return "UNKNOWN"');
    expect(handler).toContain(
      '    if state == "TERMINAL":\n        _deny("refusing image with terminal scan status \'" + status + "\'.")',
    );
    expect(handler).toContain(
      '    if state == "UNKNOWN":\n        _deny("refusing image with unknown scan status \'" + status + "\'.")',
    );
    expect(handler).toContain(
      '    if state != "USABLE":\n        _deny("refusing non-usable scan status \'" + status + "\'.")',
    );
    // Only the two converging statuses keep the waiter running.
    expect(handler).toContain(
      '    if state == "PENDING":\n        return {"IsComplete": False}',
    );
  });

  it("starts at most one basic scan and then polls only that digest", () => {
    const handler = gateHandlerSource(runtimeMemoryTemplate());
    // A scan is started ONLY when the image carries no scan status at all.
    expect(handler).toContain(
      '    if state == "ABSENT":\n        status = start_basic_scan(client, registry_id, repository_name, digest)',
    );
    expect(handler).toContain(
      '        response = client.start_image_scan(\n            registryId=registry_id,\n            repositoryName=repository_name,\n            imageId={"imageDigest": digest},\n        )',
    );
    // An already-existing scan (24 h basic-scan limit) is polled, not retried.
    expect(handler).toContain('if code == "LimitExceededException":');
    // Polling is digest-addressed only; the mutable tag is never re-used.
    expect(handler).toContain(
      '    response = client.describe_image_scan_findings(\n        registryId=registry_id,\n        repositoryName=repository_name,\n        imageId={"imageDigest": digest},\n        maxResults=1,\n    )',
    );
    expect(handler).not.toContain(
      'describe_image_scan_findings(\n        repositoryName=repository_name,\n        imageId={"imageTag"',
    );
  });

  it("fails closed on repository, tag, or digest identity drift", () => {
    const handler = gateHandlerSource(runtimeMemoryTemplate());
    for (const denial of [
      '_deny("registry identity drift in DescribeImages.")',
      '_deny("repository identity drift in DescribeImages.")',
      '_deny("image tag identity drift in DescribeImages.")',
      '_deny("registry identity drift in StartImageScan.")',
      '_deny("repository identity drift in StartImageScan.")',
      '_deny("digest identity drift in StartImageScan.")',
      '_deny("registry identity drift in DescribeImageScanFindings.")',
      '_deny("repository identity drift in DescribeImageScanFindings.")',
      '_deny("digest identity drift in DescribeImageScanFindings.")',
      '_deny("registry identity drift between waiter phases.")',
      '_deny("repository identity drift between waiter phases.")',
      '_deny("image tag does not equal the content-addressed asset hash.")',
    ]) {
      expect(handler).toContain(denial);
    }
    // Exactly one image may match the immutable asset tag.
    expect(handler).toContain("if len(details) != 1:");
    // Every digest that crosses a boundary is shape-checked.
    expect(handler).toContain(
      'DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")',
    );
    expect(handler).toContain(
      '    digest = _checked_digest(data.get("ImageDigest"))',
    );
  });

  it("keeps the identical gate in production and absent when the feature is off", () => {
    const prod = runtimeMemoryTemplate("prod");
    prod.resourceCountIs(GATE_TYPE, 1);
    const prodGate = singleResource(prod, GATE_TYPE);
    expect(prodGate.Properties.RepositoryName).toBe(
      `cdk-hnb659fds-container-assets-${PROD_ACCOUNT}-${REGION}`,
    );
    expect(
      Object.values(prod.findResources("AWS::IAM::Role")).some(
        (role: any) =>
          role.Properties.RoleName ===
          "AgenticAI-D03-prod-demo-primary-imgscan",
      ),
    ).toBe(true);

    // Default-off parity: no gate anywhere in the disabled pipeline graph.
    const { stack, template } = pipeline(false);
    expect(JSON.stringify(template.toJSON())).not.toContain(
      "AgenticAIAgentImageScanGate",
    );
    const stage = stack.node.findChild("Nonprod") as WorkloadDeploymentStage;
    expect(stage.runtimeMemoryStack).toBeUndefined();
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

  it("omits generated-agent grants when generatedAgentGrants is absent", () => {
    const template = roleTemplate(true);
    const json = JSON.stringify(template.toJSON());
    expect(json).not.toContain("bedrock-agentcore:InvokeGateway");
    expect(json).not.toContain("GetResourceOauth2Token");
    expect(json).not.toContain("ReadPlatformM2mSecret");
  });

  it("adds scoped generated-agent grants when generatedAgentGrants is set", () => {
    const template = roleTemplateWithGrants();
    const roleEntries = Object.entries(
      template.findResources("AWS::IAM::Role"),
    ) as Array<[string, any]>;
    const [runtimeRoleLogicalId] = roleEntries.find(
      ([, c]) =>
        c.Properties.RoleName === "AgenticAI-D03-nonprod-demo-primary-runtime",
    )!;
    const statements = Object.values(template.findResources("AWS::IAM::Policy"))
      .filter((policy: any) =>
        JSON.stringify(policy.Properties.Roles).includes(runtimeRoleLogicalId),
      )
      .flatMap((policy: any) => policy.Properties.PolicyDocument.Statement);
    expect(statements).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          Sid: "InvokeToolGateway",
          Effect: "Allow",
          Action: "bedrock-agentcore:InvokeGateway",
          Resource: expect.stringContaining(`:${NONPROD_ACCOUNT}:gateway/*`),
        }),
        expect.objectContaining({
          Sid: "AgentCoreIdentityInferenceToken",
          Effect: "Allow",
          Action: [
            "bedrock-agentcore:GetWorkloadAccessToken",
            "bedrock-agentcore:GetResourceOauth2Token",
          ],
        }),
        expect.objectContaining({
          Sid: "ReadPlatformM2mSecret",
          Effect: "Allow",
          Resource: expect.stringContaining("secret:agenticai/inference-m2m/"),
        }),
        expect.objectContaining({
          Sid: "DecryptPlatformM2mSecret",
          Effect: "Allow",
          Action: "kms:Decrypt",
          Condition: expect.objectContaining({
            StringEquals: expect.objectContaining({
              "kms:ViaService": `secretsmanager.${REGION}.amazonaws.com`,
            }),
          }),
        }),
      ]),
    );
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

  it("persists generated-agent inference inputs through self-mutation", () => {
    const { template } = pipeline(true, true);
    const rendered = JSON.stringify(template.toJSON());
    expect(rendered).toContain("agenticai/generatedAgentInference");
    expect(rendered).toContain(
      "agenticai-inference-nonprod-bedrock/openai.gpt-oss-120b",
    );
    expect(rendered).toContain("agenticai/inference-m2m/nonprod");
    expect(rendered).toContain("agenticai/agentImageVariant");
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
