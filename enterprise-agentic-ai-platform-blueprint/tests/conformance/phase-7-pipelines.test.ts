/**
 * Phase 7 conformance — CDK Pipelines (platform + workload) with evaluation
 * gate and manual approval.
 *
 * Spec: R-DEVX-002 mandatory stage sequence (§1.3.5 L210-212).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";

import { GuardrailStack } from "../../apps/platform-account/lib/guardrail-stack";
import {
  PlatformDeploymentStage,
  PlatformPipelineStack,
} from "../../pipelines/platform-pipeline-stack";
import { stageAwareSynthCommands } from "../../pipelines/synth-commands";
import type { GaRegistryConsumerContext } from "@agenticai/agent-registry";
import {
  WorkloadDeploymentStage,
  WorkloadPipelineStack,
  WorkstreamRegistryRolesStage,
} from "../../pipelines/workload-pipeline-stack";

const GITHUB_CONNECTION =
  "arn:aws:codestar-connections:us-west-2:111111111111:connection/abc-123";

function gaConsumerContext(
  environment: "nonprod" | "prod",
): GaRegistryConsumerContext {
  const registryId =
    environment === "nonprod" ? "ABCDEFGHIJKLMNOP" : "QRSTUVWXYZABCDEF";
  const registryArn = `arn:aws:agent-registry:us-west-2:111111111111:registry/${registryId}`;
  return {
    schemaVersion: "agenticai.ga-registry-consumer-context/1.0",
    environment,
    region: "us-west-2",
    platformAccountId: "111111111111",
    sourceRevision: "a".repeat(40),
    registryId,
    registryArn,
    readerRoleArn: `arn:aws:iam::111111111111:role/AgenticAI-RegistryReader-${environment}`,
    readerExternalId: `agenticai-registry-v1-${environment}-111111111111`,
    records: [
      ["tool-echo", "ABCDEFGHIJKL"],
      ["tool-ping", "MNOPQRSTUVWX"],
    ].map(([toolId, recordId], index) => ({
      recordId,
      recordArn: `${registryArn}/record/${recordId}`,
      descriptorSha256: String(index + 1).repeat(64),
      document: {
        schemaVersion: "agenticai.tool-governance/1.0",
        catalogueVersion: "2",
        toolId,
        description: `${toolId} description`,
        desiredApprovalStatus: "approved",
        target: {
          type: "lambda",
          arn:
            `arn:aws:lambda:us-west-2:111111111111:function:` +
            `agenticai-platform-${environment}-${toolId}:PROD`,
        },
        mcp: {
          toolName: toolId,
          description: `${toolId} description`,
          inputSchema: { type: "object" },
        },
        authorization: {
          defaultDecision: "DENY",
          cedarPolicy: `permit(principal, action, resource == Tool::"${toolId}");`,
          allowedSubjects: [],
          allowedGroups: [],
          combination: "AUTHENTICATED",
        },
        ownership: { ownerTeam: "platform-ai", costCentre: "platform" },
      },
    })),
  };
}

function createPlatformPipeline(
  app: App,
  platformProdAccount = "222222222222",
): PlatformPipelineStack {
  return new PlatformPipelineStack(app, "PP", {
    env: { account: "111111111111", region: "us-west-2" },
    githubRepo: "aws-samples/sample-ai-agent-factory",
    githubConnectionArn: GITHUB_CONNECTION,
    organizationId: "o-example123",
    logArchive: {
      env: { account: "333333333333", region: "us-west-2" },
      envName: "nonprod",
    },
    audit: {
      env: { account: "666666666666", region: "us-west-2" },
      envName: "nonprod",
    },
    platformNonprod: {
      env: { account: "111111111111", region: "us-west-2" },
      envName: "nonprod",
    },
    platformProd: {
      env: { account: platformProdAccount, region: "us-west-2" },
      envName: "prod",
    },
    workloadAccountIds: ["444444444444", "555555555555"],
    applicationId: "platform-inference",
    tenantId: "shared",
    agentId: "shared",
    costCentre: "platform",
    inferenceModelRateLimits: [
      {
        qualifiedModelId: "openai.gpt-oss-120b",
        requestsPerMinute: 10,
        tokensPerMinute: 10_000,
      },
    ],
  });
}

function synthPlatform() {
  const app = new App();
  return Template.fromStack(createPlatformPipeline(app));
}

function synthWorkload() {
  const app = new App();
  const stack = new WorkloadPipelineStack(app, "WP", {
    env: { account: "111111111111", region: "us-west-2" },
    githubRepo: "aws-samples/sample-ai-agent-factory",
    githubConnectionArn: GITHUB_CONNECTION,
    tenantId: "demo",
    agentId: "primary",
    costCentre: "engineering",
    workloadNonprodEnv: { account: "444444444444", region: "us-west-2" },
    workloadProdEnv: { account: "555555555555", region: "us-west-2" },
    workloadNonprodAvailabilityZones: [
      "us-west-2a",
      "us-west-2b",
      "us-west-2c",
    ],
    workloadProdAvailabilityZones: ["us-west-2a", "us-west-2b", "us-west-2c"],
  });
  return Template.fromStack(stack);
}

function synthWorkloadGa(): {
  readonly stack: WorkloadPipelineStack;
  readonly template: Template;
} {
  const app = new App();
  const stack = new WorkloadPipelineStack(app, "WPGA", {
    env: { account: "111111111111", region: "us-west-2" },
    githubRepo: "aws-samples/sample-ai-agent-factory",
    githubConnectionArn: GITHUB_CONNECTION,
    tenantId: "demo",
    agentId: "primary",
    applicationId: "demo",
    costCentre: "engineering",
    workloadNonprodEnv: { account: "444444444444", region: "us-west-2" },
    workloadProdEnv: { account: "555555555555", region: "us-west-2" },
    workloadNonprodAvailabilityZones: [
      "us-west-2a",
      "us-west-2b",
      "us-west-2c",
    ],
    workloadProdAvailabilityZones: ["us-west-2a", "us-west-2b", "us-west-2c"],
    gaRegistry: {
      nonprod: gaConsumerContext("nonprod"),
      prod: gaConsumerContext("prod"),
      gatewayRegion: "us-west-2",
    },
  });
  return { stack, template: Template.fromStack(stack) };
}

function expectCleanupSafeArtifactStore(
  template: Template,
  expectedTags: Record<string, string>,
): void {
  template.resourceCountIs("AWS::S3::Bucket", 1);
  template.hasResource("AWS::S3::Bucket", {
    DeletionPolicy: "Delete",
    UpdateReplacePolicy: "Delete",
    Properties: Match.objectLike({
      BucketEncryption: Match.anyValue(),
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({
            AbortIncompleteMultipartUpload: { DaysAfterInitiation: 7 },
            ExpirationInDays: 30,
            Status: "Enabled",
          }),
        ]),
      },
      OwnershipControls: {
        Rules: [{ ObjectOwnership: "BucketOwnerEnforced" }],
      },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    }),
  });
  template.resourceCountIs("AWS::KMS::Key", 1);
  template.hasResource("AWS::KMS::Key", {
    DeletionPolicy: "Delete",
    UpdateReplacePolicy: "Delete",
    Properties: Match.objectLike({
      EnableKeyRotation: true,
      PendingWindowInDays: 7,
    }),
  });

  for (const resourceType of ["AWS::S3::Bucket", "AWS::KMS::Key"]) {
    const resources = Object.values(
      template.findResources(resourceType),
    ) as any[];
    const tagMap = Object.fromEntries(
      resources[0].Properties.Tags.map(
        (tag: { Key: string; Value: string }) => [tag.Key, tag.Value],
      ),
    );
    expect(tagMap).toMatchObject(expectedTags);
  }

  template.resourceCountIs("Custom::S3AutoDeleteObjects", 1);
}

describe("Phase 7 — Platform pipeline", () => {
  it("emits a single CodePipeline", () => {
    const t = synthPlatform();
    t.resourceCountIs("AWS::CodePipeline::Pipeline", 1);
  });

  it("pipeline name is stable", () => {
    const t = synthPlatform();
    t.hasResourceProperties("AWS::CodePipeline::Pipeline", {
      Name: "agenticai-platform-pipeline",
    });
  });

  it("owns a stable service role used by CodePipeline", () => {
    const template = synthPlatform();
    const roles = template.findResources("AWS::IAM::Role");
    const [logicalId, role] = Object.entries(roles).find(
      ([, candidate]: [string, any]) =>
        candidate.Properties?.RoleName === "AgenticAI-PlatformPipelineRole",
    ) as [string, any];
    expect(role.Properties.AssumeRolePolicyDocument.Statement).toContainEqual(
      expect.objectContaining({
        Action: "sts:AssumeRole",
        Effect: "Allow",
        Principal: { Service: "codepipeline.amazonaws.com" },
      }),
    );
    const pipeline = Object.values(
      template.findResources("AWS::CodePipeline::Pipeline"),
    )[0] as any;
    expect(pipeline.Properties.RoleArn).toEqual({
      "Fn::GetAtt": [logicalId, "Arn"],
    });
  });

  it("deploys shared Management/Governance stacks exactly once", () => {
    const pipeline = Object.values(
      synthPlatform().findResources("AWS::CodePipeline::Pipeline"),
    )[0] as any;
    const stages = pipeline.Properties.Stages as any[];
    const actionNames = (stageName: string): string[] =>
      stages
        .find((stage) => stage.Name === stageName)
        .Actions.map((action: { Name: string }) => action.Name);

    const nonprodActions = actionNames("Nonprod");
    const prodActions = actionNames("Prod");
    for (const action of [
      "Audit.Prepare",
      "Audit.Deploy",
      "LogArchive.Prepare",
      "LogArchive.Deploy",
    ]) {
      expect(nonprodActions).toContain(action);
      expect(prodActions).not.toContain(action);
    }
  });

  it("reuses the admin role and isolates guardrail names in one Platform account", () => {
    const app = new App();
    const root = createPlatformPipeline(app, "111111111111");
    const nonprodStage = root.node.findChild(
      "Nonprod",
    ) as PlatformDeploymentStage;
    const prodStage = root.node.findChild("Prod") as PlatformDeploymentStage;
    const nonprodGuardrail = nonprodStage.node.findChild(
      "Guardrail",
    ) as GuardrailStack;
    const prodGuardrail = prodStage.node.findChild(
      "Guardrail",
    ) as GuardrailStack;
    const nonprodTemplate = Template.fromStack(nonprodGuardrail);
    const prodTemplate = Template.fromStack(prodGuardrail);

    nonprodTemplate.hasResourceProperties("AWS::IAM::Role", {
      RoleName: "AgenticAI-GuardrailAdmin",
    });
    const adminRole = Object.values(
      nonprodTemplate.findResources("AWS::IAM::Role"),
    ).find(
      (role: any) => role.Properties.RoleName === "AgenticAI-GuardrailAdmin",
    ) as any;
    expect(
      adminRole.Properties.AssumeRolePolicyDocument.Statement[0].Principal.AWS,
    ).toEqual({
      "Fn::Join": [
        "",
        [
          "arn:",
          { Ref: "AWS::Partition" },
          ":iam::111111111111:role/AgenticAI-PlatformPipelineRole",
        ],
      ],
    });
    nonprodTemplate.hasResourceProperties("AWS::Bedrock::Guardrail", {
      Name: "agenticai-guardrail-baseline",
    });
    prodTemplate.resourceCountIs("AWS::IAM::Role", 0);
    prodTemplate.hasResourceProperties("AWS::Bedrock::Guardrail", {
      Name: "agenticai-guardrail-baseline-prod",
    });
  });

  it("configures cross-account keys (required for multi-account stages)", () => {
    const t = synthPlatform();
    // CodePipelines emits encrypted artifact store with KMS key ARN when crossAccountKeys=true.
    const pipelines = t.findResources("AWS::CodePipeline::Pipeline");
    const pipeline = Object.values(pipelines)[0] as any;
    const stores = pipeline.Properties.ArtifactStores ?? [
      pipeline.Properties.ArtifactStore,
    ];
    const usesKms = stores.some(
      (s: any) =>
        s?.ArtifactStore?.EncryptionKey?.Type === "KMS" ||
        s?.EncryptionKey?.Type === "KMS",
    );
    expect(usesKms).toBe(true);
  });
});

describe("Phase 7 — pipeline artifact stores", () => {
  it("encrypts, tags, expires and removes Platform and Workload artifacts", () => {
    expectCleanupSafeArtifactStore(synthPlatform(), {
      "application-id": "platform-inference",
      "agent-id": "shared",
      "tenant-id": "shared",
      "cost-centre": "platform",
      environment: "pipeline",
    });
    expectCleanupSafeArtifactStore(synthWorkload(), {
      "application-id": "demo",
      "agent-id": "primary",
      "tenant-id": "demo",
      "cost-centre": "engineering",
      environment: "pipeline",
    });
  });
});

describe("Phase 7 — cross-account bootstrap role contract", () => {
  it("references both deploy and CloudFormation execution roles in target accounts", () => {
    const platform = JSON.stringify(synthPlatform().toJSON());
    expect(platform).toContain(
      "cdk-hnb659fds-deploy-role-333333333333-us-west-2",
    );
    expect(platform).toContain(
      "cdk-hnb659fds-cfn-exec-role-333333333333-us-west-2",
    );

    const workload = JSON.stringify(synthWorkload().toJSON());
    expect(workload).toContain(
      "cdk-hnb659fds-deploy-role-444444444444-us-west-2",
    );
    expect(workload).toContain(
      "cdk-hnb659fds-cfn-exec-role-444444444444-us-west-2",
    );

    const bootstrapSource = readFileSync(
      resolve(
        __dirname,
        "../../pipelines/bootstrap/bootstrap-cross-account.sh",
      ),
      "utf8",
    );
    expect(bootstrapSource).toContain(
      "iam:PassedToService=codepipeline.amazonaws.com",
    );
    expect(bootstrapSource).toContain(
      "iam:PassedToService=cloudformation.amazonaws.com",
    );
    expect(bootstrapSource).toContain(
      "Nonprod-LogArchive-CustomS3AutoDeleteObjects*",
    );
    expect(bootstrapSource).toContain("iam:AttachRolePolicy");
    expect(bootstrapSource).toContain("iam:DetachRolePolicy");
    expect(bootstrapSource).toContain(
      "iam:PassedToService=lambda.amazonaws.com",
    );
    expect(bootstrapSource).toContain(
      "iam:PassedToService=states.amazonaws.com",
    );
    expect(bootstrapSource).toContain(
      'LOG_ARCHIVE="$(json agenticai/logArchiveAccountId)"',
    );
    expect(bootstrapSource).toContain(
      '"$PLATFORM_NP" "$PLATFORM_PR" "$LOG_ARCHIVE" "$AUDIT"',
    );
    expect(bootstrapSource).toContain('TARGET_ACCOUNTS+=("$acct")');
  });
});

describe("Phase 7 — Workload pipeline has mandatory stages + eval gate", () => {
  it("emits an evaluation-gate CodeBuild with the 5 SLO threshold env vars", () => {
    const t = synthWorkload();
    const projects = t.findResources("AWS::CodeBuild::Project");
    const joined = JSON.stringify(projects);
    expect(joined).toContain("EVAL_REGRESSION_PASS_MIN_PCT");
    expect(joined).toContain("EVAL_GUARDRAIL_VIOLATION_MAX_PCT");
    expect(joined).toContain("EVAL_QUALITY_MIN_PCT");
    expect(joined).toContain("EVAL_TOOL_SUCCESS_MIN_PCT");
    expect(joined).toContain("EVAL_FIRST_TOKEN_P99_MAX_MS");
  });

  it("pipeline name embeds tenant + agent", () => {
    const t = synthWorkload();
    t.hasResourceProperties("AWS::CodePipeline::Pipeline", {
      Name: "agenticai-workload-demo-primary",
    });
  });
});

describe("Phase 7 — R2 GA Registry Workload pipeline", () => {
  it("creates one named synth role that may assume only the two reader roles", () => {
    const { template } = synthWorkloadGa();
    const roles = template.findResources("AWS::IAM::Role");
    const synthRoleEntry = Object.entries(roles).find(
      ([, role]: [string, any]) =>
        role.Properties?.RoleName ===
        "AgenticAI-WLP-demo-primary-RegistrySynth",
    );
    expect(synthRoleEntry).toBeDefined();
    const [synthRoleLogicalId] = synthRoleEntry!;
    const policies = template.findResources("AWS::IAM::Policy");
    const synthPolicy = Object.values(policies).find((policy: any) =>
      JSON.stringify(policy.Properties.Roles).includes(synthRoleLogicalId),
    ) as any;
    expect(synthPolicy).toBeDefined();
    const assume = synthPolicy.Properties.PolicyDocument.Statement.find(
      (statement: any) =>
        (Array.isArray(statement.Action)
          ? statement.Action
          : [statement.Action]
        ).includes("sts:AssumeRole"),
    );
    expect(assume.Resource).toEqual([
      "arn:aws:iam::111111111111:role/AgenticAI-RegistryReader-nonprod",
      "arn:aws:iam::111111111111:role/AgenticAI-RegistryReader-prod",
    ]);
  });

  it("deploys stable roles for both environments before the Gateway stage", () => {
    const { stack, template } = synthWorkloadGa();
    const rolesStage = stack.node.findChild(
      "RegistryRoles",
    ) as WorkstreamRegistryRolesStage;
    expect(rolesStage.nonprod.stackName).toBe(
      "AgenticAI-demo-primary-nonprod-RegistryRoles",
    );
    expect(rolesStage.prod.stackName).toBe(
      "AgenticAI-demo-primary-prod-RegistryRoles",
    );
    for (const [environment, roleStack] of [
      ["nonprod", rolesStage.nonprod],
      ["prod", rolesStage.prod],
    ] as const) {
      const roleTemplate = Template.fromStack(roleStack);
      const roleNames = Object.values(
        roleTemplate.findResources("AWS::IAM::Role"),
      )
        .map((role: any) => role.Properties.RoleName)
        .filter((name: unknown): name is string => typeof name === "string");
      expect(roleNames).toEqual(
        expect.arrayContaining([
          `AgenticAI-D03-${environment}-demo-primary-gw-svc`,
          `AgenticAI-D03-${environment}-demo-primary-RegistryValidator`,
          `AgenticAI-D03-${environment}-GatewayAdmin`,
        ]),
      );
      expect(Object.values(roleTemplate.toJSON().Outputs ?? {})).toContainEqual(
        expect.objectContaining({
          Description:
            "Exact existing principal ARN for agenticai/gaGatewayServiceRoleArns.",
          Value: { "Fn::GetAtt": [expect.any(String), "Arn"] },
        }),
      );
    }
    const pipeline = Object.values(
      template.findResources("AWS::CodePipeline::Pipeline"),
    )[0] as any;
    const stages = pipeline.Properties.Stages as any[];
    const rolesIndex = stages.findIndex(
      (stage) => stage.Name === "RegistryRoles",
    );
    const nonprodIndex = stages.findIndex((stage) => stage.Name === "Nonprod");
    expect(rolesIndex).toBeGreaterThanOrEqual(0);
    expect(rolesIndex).toBeLessThan(nonprodIndex);
    expect(JSON.stringify(stages[rolesIndex])).toContain(
      "GatewayPermissionReady",
    );
  });

  it("scopes each stable prerequisite role to its one responsibility", () => {
    const { stack } = synthWorkloadGa();
    const rolesStage = stack.node.findChild(
      "RegistryRoles",
    ) as WorkstreamRegistryRolesStage;
    const template = Template.fromStack(rolesStage.nonprod);
    const roles = Object.values(
      template.findResources("AWS::IAM::Role"),
    ) as any[];
    const byName = (name: string) =>
      roles.find((role) => role.Properties.RoleName === name);

    const gateway = byName("AgenticAI-D03-nonprod-demo-primary-gw-svc");
    const invoke = gateway.Properties.Policies[0].PolicyDocument.Statement[0];
    expect(invoke.Action).toBe("lambda:InvokeFunction");
    expect(invoke.Resource).toEqual([
      "arn:aws:lambda:us-west-2:111111111111:function:agenticai-platform-nonprod-tool-echo:PROD",
      "arn:aws:lambda:us-west-2:111111111111:function:agenticai-platform-nonprod-tool-ping:PROD",
    ]);

    const validator = byName(
      "AgenticAI-D03-nonprod-demo-primary-RegistryValidator",
    );
    const assume = validator.Properties.Policies[0].PolicyDocument.Statement[0];
    expect(assume).toMatchObject({
      Action: "sts:AssumeRole",
      Resource:
        "arn:aws:iam::111111111111:role/AgenticAI-RegistryReader-nonprod",
    });

    const admin = byName("AgenticAI-D03-nonprod-GatewayAdmin");
    const adminStatements =
      admin.Properties.Policies[0].PolicyDocument.Statement;
    expect(adminStatements).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          Action: "bedrock-agentcore:*",
          Resource: "*",
        }),
        expect.objectContaining({
          Action: "iam:PassRole",
          Resource: expect.anything(),
        }),
      ]),
    );
  });

  it("imports prerequisite roles into each full Gateway stack", () => {
    const { stack } = synthWorkloadGa();
    for (const stageName of ["Nonprod", "Prod"]) {
      const stage = stack.node.findChild(stageName) as WorkloadDeploymentStage;
      const gateway = Template.fromStack(stage.gatewayStack!);
      const rendered = JSON.stringify(gateway.toJSON());
      expect(rendered).toContain(
        `AgenticAI-D03-${stageName.toLowerCase()}-demo-primary-gw-svc`,
      );
      expect(rendered).toContain(
        `AgenticAI-D03-${stageName.toLowerCase()}-demo-primary-RegistryValidator`,
      );
      expect(rendered).toContain(
        `AgenticAI-D03-${stageName.toLowerCase()}-GatewayAdmin`,
      );
      const namedRoles = Object.values(gateway.findResources("AWS::IAM::Role"))
        .map((role: any) => role.Properties.RoleName)
        .filter((name: unknown): name is string => typeof name === "string");
      expect(namedRoles).not.toContain(
        `AgenticAI-D03-${stageName.toLowerCase()}-demo-primary-gw-svc`,
      );
    }
  });

  it("resolves both contexts with pinned SDKs before a Workload-only synth", () => {
    const blob = codeBuildBlob(synthWorkloadGa().template);
    expect(blob).toContain("requirements-ga-registry-resolver.txt");
    expect(blob).toContain("resolve_ga_registry_context.py");
    expect(blob.match(/--assume-reader/g)).toHaveLength(2);
    expect(blob).toContain("GA_REGISTRY_NONPROD_CONTEXT_FILE");
    expect(blob).toContain("GA_REGISTRY_PROD_CONTEXT_FILE");
    expect(blob).toContain("; export GA_REGISTRY_NONPROD_CONTEXT_FILE;");
    expect(blob).toContain("; export GA_REGISTRY_PROD_CONTEXT_FILE;");
    expect(blob).toContain("agenticai/gaRegistryNonprodContextFile");
    expect(blob).toContain("agenticai/gaRegistryProdContextFile");
    expect(blob).toContain("agenticai/pipelineSelection");
    expect(blob).toContain("workload");
    expect(blob).not.toContain("--context agenticai/pipelineSelection='both'");
  });

  it("composes only GA ToolGateway stacks in both deployment stages", () => {
    const { stack } = synthWorkloadGa();
    const nonprod = stack.node.findChild("Nonprod") as WorkloadDeploymentStage;
    const prod = stack.node.findChild("Prod") as WorkloadDeploymentStage;
    expect(nonprod.gatewayStack?.stackName).toBe(
      "AgenticAI-demo-primary-nonprod-ToolGateway",
    );
    expect(prod.gatewayStack?.stackName).toBe(
      "AgenticAI-demo-primary-prod-ToolGateway",
    );
    for (const stage of [nonprod, prod]) {
      expect(stage.gatewayStack).toBeDefined();
      expect(stage.networkStack).toBeUndefined();
      expect(stage.appStack).toBeUndefined();
      const gateway = Template.fromStack(stage.gatewayStack!);
      gateway.resourceCountIs("Custom::AgenticAIRegistryRecordValidator", 2);
      gateway.resourceCountIs("Custom::BedrockAgentCoreGatewayTarget", 2);
    }
  });

  it("uses a Gateway-specific production approval in GA mode", () => {
    const { template } = synthWorkloadGa();
    const pipeline = Object.values(
      template.findResources("AWS::CodePipeline::Pipeline"),
    )[0] as any;
    const stages = pipeline.Properties.Stages as any[];
    const prod = stages.find((stage) => stage.Name === "Prod");
    const actions = prod.Actions.map((action: any) => action.Name);
    const allActions = stages.flatMap((stage) =>
      stage.Actions.map((action: any) => action.Name),
    );
    expect(actions).toContain("ProdGatewayApproval");
    expect(allActions).not.toContain("EvaluationGate");
    expect(allActions).not.toContain("CanaryDeploy");
    expect(allActions).not.toContain("CanarySoak");
    expect(actions.some((name: string) => name.includes("Deploy"))).toBe(true);
  });

  it("keeps the legacy Network/App composition when GA mode is disabled", () => {
    const app = new App();
    const stack = new WorkloadPipelineStack(app, "Legacy", {
      env: { account: "111111111111", region: "us-west-2" },
      githubRepo: "aws-samples/sample-ai-agent-factory",
      githubConnectionArn: GITHUB_CONNECTION,
      tenantId: "demo",
      agentId: "primary",
      costCentre: "engineering",
      workloadNonprodEnv: { account: "444444444444", region: "us-west-2" },
      workloadProdEnv: { account: "555555555555", region: "us-west-2" },
      workloadNonprodAvailabilityZones: ["us-west-2a", "us-west-2b"],
      workloadProdAvailabilityZones: ["us-west-2a", "us-west-2b"],
    });
    const nonprod = stack.node.findChild("Nonprod") as WorkloadDeploymentStage;
    expect(nonprod.gatewayStack).toBeUndefined();
    expect(nonprod.networkStack).toBeDefined();
    expect(nonprod.appStack).toBeDefined();
    expect(JSON.stringify(Template.fromStack(stack).toJSON())).not.toContain(
      "RegistrySynth",
    );
  });

  it("rejects mismatched environment tool sets before creating a pipeline", () => {
    const prod = gaConsumerContext("prod");
    (prod as any).records = prod.records.slice(0, 1);
    expect(
      () =>
        new WorkloadPipelineStack(new App(), "Mismatch", {
          env: { account: "111111111111", region: "us-west-2" },
          githubRepo: "aws-samples/sample-ai-agent-factory",
          githubConnectionArn: GITHUB_CONNECTION,
          tenantId: "demo",
          agentId: "primary",
          costCentre: "engineering",
          workloadNonprodEnv: { account: "444444444444", region: "us-west-2" },
          workloadProdEnv: { account: "555555555555", region: "us-west-2" },
          workloadNonprodAvailabilityZones: ["us-west-2a", "us-west-2b"],
          workloadProdAvailabilityZones: ["us-west-2a", "us-west-2b"],
          gaRegistry: {
            nonprod: gaConsumerContext("nonprod"),
            prod,
            gatewayRegion: "us-west-2",
          },
        }),
    ).toThrow(/tool sets must match/);
  });
});

describe("Phase 7 — environment-backed synth context", () => {
  it("places resolver commands before quoted dynamic CDK context", () => {
    const commands = stageAwareSynthCommands({
      stage: "pipeline",
      context: { "agenticai/pipelineSelection": "workload" },
      preSynthCommands: [
        'DYNAMIC_CONTEXT="$PWD/context.json"; export DYNAMIC_CONTEXT',
      ],
      contextFromEnvironment: {
        "agenticai/gaRegistryNonprodContextFile": "DYNAMIC_CONTEXT",
      },
      expectedStackArtifactId: "WPGA",
      expectedStageAssemblyGlobs: ["cdk.out/assembly-*Nonprod"],
    });
    const resolverIndex = commands.findIndex((command) =>
      command.includes("DYNAMIC_CONTEXT="),
    );
    const synthIndex = commands.findIndex((command) =>
      command.startsWith("npx cdk synth"),
    );
    expect(resolverIndex).toBeGreaterThanOrEqual(0);
    expect(resolverIndex).toBeLessThan(synthIndex);
    expect(commands[synthIndex]).toContain(
      '--context agenticai/gaRegistryNonprodContextFile="${DYNAMIC_CONTEXT}"',
    );
    for (const command of commands) {
      expect(() =>
        execFileSync("/bin/sh", ["-n", "-c", command]),
      ).not.toThrow();
    }
  });

  it("rejects invalid context keys and shell variable names", () => {
    const base = {
      stage: "pipeline",
      context: {},
      expectedStackArtifactId: "WPGA",
      expectedStageAssemblyGlobs: ["cdk.out/assembly-*Nonprod"],
    };
    expect(() =>
      stageAwareSynthCommands({
        ...base,
        contextFromEnvironment: { "bad key": "GOOD_NAME" },
      }),
    ).toThrow(/Invalid CDK context key/);
    expect(() =>
      stageAwareSynthCommands({
        ...base,
        contextFromEnvironment: { "agenticai/value": "bad;name" },
      }),
    ).toThrow(/Invalid environment variable/);
  });
});

/**
 * Round 1B — false-green regressions (tasks/todo.md §Round 1 B).
 *
 * Each expectation below fails against the pre-Round-1B implementation:
 *   - synth ran a bare `npx cdk synth`, which the app answers with an EMPTY
 *     cloud assembly and exit 0;
 *   - evaluation, approval, canary and soak sat in one `pre` array with no
 *     declared dependencies, so CDK Pipelines gave them the same RunOrder and
 *     ran them concurrently;
 *   - the canary deploy called `aws lambda update-alias … || true` against a
 *     function this blueprint never creates, and the soak read a missing alarm
 *     as `OK`.
 */
let workloadTemplate: Template | undefined;
let platformTemplate: Template | undefined;

function workload(): Template {
  workloadTemplate = workloadTemplate ?? synthWorkload();
  return workloadTemplate;
}

function platform(): Template {
  platformTemplate = platformTemplate ?? synthPlatform();
  return platformTemplate;
}

/**
 * All CodeBuild projects as one string. Assertions must avoid double quotes:
 * buildspecs are embedded as JSON strings, so `"` appears escaped.
 */
function codeBuildBlob(t: Template): string {
  return JSON.stringify(t.findResources("AWS::CodeBuild::Project"));
}

function countOccurrences(haystack: string, needle: string): number {
  return haystack.split(needle).length - 1;
}

function prodStageActions(
  t: Template,
): Array<{ Name: string; RunOrder: number }> {
  const pipelines = t.findResources("AWS::CodePipeline::Pipeline");
  const pipeline = Object.values(pipelines)[0] as any;
  const stage = (pipeline.Properties.Stages as any[]).find(
    (s) => s.Name === "Prod",
  );
  if (!stage) {
    throw new Error(
      `No 'Prod' stage; got: ${(pipeline.Properties.Stages as any[]).map((s) => s.Name).join(", ")}`,
    );
  }
  return stage.Actions as Array<{ Name: string; RunOrder: number }>;
}

function runOrderOf(t: Template, needle: string): number {
  const actions = prodStageActions(t);
  const action = actions.find((a) => String(a.Name).includes(needle));
  if (!action) {
    throw new Error(
      `No Prod action matching '${needle}'; got: ${actions.map((a) => a.Name).join(", ")}`,
    );
  }
  return action.RunOrder;
}

describe("Round 1B — workload pipeline synth cannot produce an empty assembly", () => {
  assertSynthIsStageAware(workload);
});

describe("Round 1B — platform pipeline synth cannot produce an empty assembly", () => {
  assertSynthIsStageAware(platform);
});

/**
 * Shared synth-step expectations. Declared as a function so both pipelines get
 * the identical checks without relying on `describe.each` tuple inference.
 */
function assertSynthIsStageAware(template: () => Template): void {
  it("names the stage explicitly on every cdk synth invocation", () => {
    const blob = codeBuildBlob(template());
    const staged = countOccurrences(blob, "npx cdk synth --context stage=");
    const total = countOccurrences(blob, "npx cdk synth");
    expect(staged).toBeGreaterThan(0);
    // A bare `npx cdk synth` anywhere means the app's `undefined` stage branch
    // can still emit an empty assembly and exit 0.
    expect(staged).toBe(total);
  });

  it("asserts the assembly, its own template and each stage assembly are non-empty", () => {
    const blob = codeBuildBlob(template());
    expect(blob).toContain("test -f cdk.out/manifest.json");
    expect(blob).toContain("aws:cloudformation:stack");
    expect(blob).toContain("cdk.out/assembly-*Nonprod");
    expect(blob).toContain("cdk.out/assembly-*Prod");
    expect(blob).toContain(".template.json");
  });

  it("forwards the context the stage needs so a missing key fails loudly", () => {
    const blob = codeBuildBlob(template());
    expect(blob).toContain("--context agenticai/githubRepo=");
    expect(blob).toContain("--context agenticai/githubConnectionArn=");
  });

  it("swallows no command failure in any build step", () => {
    const blob = codeBuildBlob(template());
    expect(blob).not.toContain("|| true");
    expect(blob).not.toContain("|| echo");
    expect(blob).not.toContain("2>/dev/null ||");
  });
}

describe("Round 1B — workload promotion order is declared, not implied", () => {
  it("orders evaluation -> approval -> canary deploy -> canary soak", () => {
    const t = workload();
    expect(runOrderOf(t, "EvaluationGate")).toBeLessThan(
      runOrderOf(t, "ProdApproval"),
    );
    expect(runOrderOf(t, "ProdApproval")).toBeLessThan(
      runOrderOf(t, "CanaryDeploy"),
    );
    expect(runOrderOf(t, "CanaryDeploy")).toBeLessThan(
      runOrderOf(t, "CanarySoak"),
    );
  });

  it("places the manual approval before any canary action", () => {
    const t = workload();
    const approval = runOrderOf(t, "ProdApproval");
    for (const action of prodStageActions(t)) {
      if (String(action.Name).includes("Canary")) {
        expect(action.RunOrder).toBeGreaterThan(approval);
      }
    }
  });

  it("deploys Prod only after the soak", () => {
    const t = workload();
    const soak = runOrderOf(t, "CanarySoak");
    const deployActions = prodStageActions(t).filter((a) => {
      const name = String(a.Name);
      // 'CanaryDeploy' is a gate, not the Prod deployment.
      if (name.includes("Canary")) return false;
      return name.includes("Deploy") || name.includes("Prepare");
    });
    expect(deployActions.length).toBeGreaterThan(0);
    for (const action of deployActions) {
      expect(action.RunOrder).toBeGreaterThan(soak);
    }
  });
});

describe("Round 1B — workload canary and evaluation fail closed", () => {
  it("replaces the fake Lambda alias canary with an explicit failing placeholder", () => {
    const blob = codeBuildBlob(workload());
    expect(blob).not.toContain("aws lambda update-alias");
    expect(blob).toContain("NOT IMPLEMENTED");
    expect(blob).toContain("exit 1");
  });

  it("runs a real traffic-shift command when one is supplied", () => {
    const app = new App();
    const stack = new WorkloadPipelineStack(app, "WPCanary", {
      env: { account: "111111111111", region: "us-west-2" },
      githubRepo: "aws-samples/sample-ai-agent-factory",
      githubConnectionArn: GITHUB_CONNECTION,
      tenantId: "demo",
      agentId: "primary",
      costCentre: "engineering",
      workloadNonprodEnv: { account: "444444444444", region: "us-west-2" },
      workloadProdEnv: { account: "555555555555", region: "us-west-2" },
      workloadNonprodAvailabilityZones: [
        "us-west-2a",
        "us-west-2b",
        "us-west-2c",
      ],
      workloadProdAvailabilityZones: ["us-west-2a", "us-west-2b", "us-west-2c"],
      canaryDeployCommands: ["scripts/shift-agentcore-canary.sh 5"],
    });
    const blob = codeBuildBlob(Template.fromStack(stack));
    expect(blob).toContain("scripts/shift-agentcore-canary.sh 5");
    expect(blob).not.toContain("NOT IMPLEMENTED");
  });

  it("treats a missing or unusable online-eval alarm as a failed soak", () => {
    const blob = codeBuildBlob(workload());
    // The soak must prove the composite alarm exists before polling it.
    expect(blob).toContain("--alarm-types CompositeAlarm");
    expect(blob).toContain("length(CompositeAlarms)");
    expect(blob).toContain("not found");
    // …and must end on OK, so INSUFFICIENT_DATA cannot pass as health.
    expect(blob).toContain("INSUFFICIENT_DATA");
    expect(blob).toContain("OK required");
  });

  it("fails the evaluation gate when the harness is absent", () => {
    const blob = codeBuildBlob(workload());
    expect(blob).toContain("scripts/evaluation_gate.py is missing");
  });
});

describe("Round 1 integration — CDK app self-synth contract", () => {
  const appSource = readFileSync(
    resolve(__dirname, "../../bin/agentic-ai-platform.ts"),
    "utf8",
  );
  const synthCommandsSource = readFileSync(
    resolve(__dirname, "../../pipelines/synth-commands.ts"),
    "utf8",
  );
  const platformPipelineSource = readFileSync(
    resolve(__dirname, "../../pipelines/platform-pipeline-stack.ts"),
    "utf8",
  );
  const packageDocument = JSON.parse(
    readFileSync(resolve(__dirname, "../../package.json"), "utf8"),
  ) as { scripts: Record<string, string> };

  it("uses an explicit, meaningful stage for the default npm synth", () => {
    expect(packageDocument.scripts.synth).toContain("--strict");
    expect(packageDocument.scripts.synth).toContain(
      "--context stage=management",
    );
  });

  it("enters the blueprint package for a multi-project source checkout", () => {
    expect(synthCommandsSource).toMatch(
      /const BLUEPRINT_SOURCE_DIRECTORY = ["']enterprise-agentic-ai-platform-blueprint["']/,
    );
    expect(synthCommandsSource).toContain("if [ -f package.json ]; then :;");
    expect(synthCommandsSource).toContain("blueprint package.json not found");
    expect(synthCommandsSource).toContain("exit 1");
  });

  it("emits independently valid POSIX commands and publishes root cdk.out", () => {
    const commands = stageAwareSynthCommands({
      stage: "pipeline",
      context: {},
      expectedStackArtifactId: "AgenticAI-PlatformPipelineStack",
      expectedStageAssemblyGlobs: [
        "cdk.out/assembly-*Nonprod",
        "cdk.out/assembly-*Prod",
      ],
    });

    for (const command of commands) {
      expect(() =>
        execFileSync("/bin/sh", ["-n", "-c", command]),
      ).not.toThrow();
    }

    const assemblyLoop = commands.find((command) =>
      command.startsWith("for asm in"),
    );
    expect(assemblyLoop).toContain("done");
    expect(commands[commands.length - 1]).toContain(
      "CODEBUILD_SRC_DIR/cdk.out",
    );
    expect(commands[commands.length - 1]).toContain("mv cdk.out");
  });

  it("keeps shared governance out of the production Platform stage", () => {
    expect(platformPipelineSource).toMatch(
      /if \(props\.envName === ["']nonprod["']\)/,
    );
    expect(platformPipelineSource).toMatch(
      /retainGovernanceOnDelete: props\.logArchive\.envName === ["']prod["']/,
    );
    expect(platformPipelineSource).toContain("platformAccountIsShared");
    expect(platformPipelineSource).toContain("sharedGuardrailAdminRoleArn");
    expect(platformPipelineSource).toContain(
      "agenticai-guardrail-baseline-prod",
    );
    expect(platformPipelineSource).toContain(
      "existingGuardrailAdminRoleArn: sharedGuardrailAdminRoleArn",
    );
  });

  it("rejects a missing stage instead of emitting an empty assembly", () => {
    expect(appSource).toMatch(/case undefined:\s*throw new Error\(/);
    expect(appSource).toContain("Missing required CDK context 'stage'");
  });

  it("forwards the complete shared context to both pipeline stacks", () => {
    expect(appSource.match(/synthContext: sharedSynthContext/g)).toHaveLength(
      2,
    );
    for (const key of [
      "organizationId",
      "platformNonprodAccountId",
      "platformProdAccountId",
      "auditAccountId",
      "logArchiveAccountId",
      "workloadNonprodAccountId",
      "workloadProdAccountId",
      "workloadNonprodAvailabilityZones",
      "workloadProdAvailabilityZones",
      "workloadAccountIds",
      "applicationId",
      "tenantId",
      "agentId",
      "costCentre",
      "inferenceModelRateLimits",
    ]) {
      expect(appSource).toContain(`agenticai/${key}`);
    }
  });
});
