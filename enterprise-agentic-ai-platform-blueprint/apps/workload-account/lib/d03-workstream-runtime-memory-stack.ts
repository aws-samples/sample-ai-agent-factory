/*
 * D03WorkstreamRuntimeMemoryStack — opt-in, pipeline-owned native AgentCore
 * Runtime + Memory foundation for a workstream.
 *
 * This stack stands up the two native GA resources whose API contracts were
 * live-proven on exact commit `88d5381` (see
 * `evidence/live/2026-09-21-agentcore-runtime-memory-compatibility-spike.md`):
 *
 *   - `AWS::BedrockAgentCore::Memory`  — short/long-term memory store, CMK-
 *     encrypted, event-expiry bounded.
 *   - `AWS::BedrockAgentCore::Runtime` — the ARM64 container the inert proven
 *     agent runs under, addressed by an exact `@sha256` image digest.
 *
 * It is deliberately a FOUNDATION, not the generated-agent integration: the
 * Runtime carries only `AGENTCORE_MEMORY_ID` and nothing that wires LLM
 * inference or MCP tools. Generated-agent `LiteLLMModel`/`MCPClient` remains
 * the NEXT gate and is intentionally left unwired here — faking it would report
 * success for behaviour never exercised.
 *
 * Network posture: `networkMode = PUBLIC`, matching the live commit. A VPC
 * (`VPC` network mode) network configuration remains a documented next gate;
 * see the `networkMode` note below.
 *
 * The Runtime execution role is NOT created here. It is a stable, prior-stage
 * resource emitted by `D03WorkstreamRegistryRolesStack` (avoids the
 * fresh-IAM-role → AgentCore control-plane propagation race). This stack
 * imports it by its deterministic ARN (or an explicit override).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  CfnDeletionPolicy,
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  Tags,
} from "aws-cdk-lib";
import { CfnMemory, CfnRuntime } from "aws-cdk-lib/aws-bedrockagentcore";
import { Platform } from "aws-cdk-lib/aws-ecr-assets";
import { DockerImageAsset } from "aws-cdk-lib/aws-ecr-assets";
import { Effect, PolicyStatement, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { Key } from "aws-cdk-lib/aws-kms";
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
} from "aws-cdk-lib/custom-resources";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";
import { join } from "node:path";

/** Environment-qualified allocation tags, applied to every emitted resource. */
export interface AllocationTagInputs {
  readonly applicationId: string;
  readonly agentId: string;
  readonly tenantId: string;
  readonly costCentre: string;
}

export interface D03WorkstreamRuntimeMemoryStackProps
  extends StackProps, AllocationTagInputs {
  readonly envName: "nonprod" | "prod";
  /**
   * Days short-term Memory events are retained. AgentCore requires 3–365.
   * Default 30.
   */
  readonly eventExpiryDays?: number;
  /**
   * Deterministic ARN of the stable, prior-stage Runtime execution role from
   * `D03WorkstreamRegistryRolesStack`. When omitted the ARN is derived from the
   * exact `AgenticAI-D03-<env>-<tenant>-<agent>-runtime` name family in this
   * stack's own account.
   */
  readonly runtimeExecutionRoleArnOverride?: string;
  /**
   * Production always retains its CMK. Nonproduction defaults to `DESTROY`
   * with a seven-day pending-deletion window; set true only for an explicit
   * state-retention test.
   */
  readonly retainMemoryKey?: boolean;
}

/** Native AgentCore network modes modeled by CfnRuntime. */
const NETWORK_MODE_PUBLIC = "PUBLIC";

/** Runtime is usable at this status; Memory at ACTIVE. */
export class D03WorkstreamRuntimeMemoryStack extends Stack {
  readonly memory: CfnMemory;
  readonly runtime: CfnRuntime;
  readonly memoryKey: Key;
  /** Exact digest-pinned image URI (`<repositoryUri>@sha256:<digest>`). */
  readonly containerDigestUri: string;

  constructor(
    scope: Construct,
    id: string,
    props: D03WorkstreamRuntimeMemoryStackProps,
  ) {
    super(scope, id, props);

    const eventExpiryDays = props.eventExpiryDays ?? 30;
    if (
      !Number.isSafeInteger(eventExpiryDays) ||
      eventExpiryDays < 3 ||
      eventExpiryDays > 365
    ) {
      throw new Error(
        "D03WorkstreamRuntimeMemoryStack: eventExpiryDays must be an integer from 3 through 365.",
      );
    }

    const base = `${props.envName}-${props.tenantId}-${props.agentId}`;
    // AgentCore Runtime/Memory names use the [A-Za-z][A-Za-z0-9_]* charset —
    // hyphens are invalid — so the hyphenated base is translated to underscores.
    const underscoreBase = base.replace(/-/g, "_");
    const runtimeName = `AgenticAI_D03_${underscoreBase}_runtime`;
    const memoryName = `AgenticAI_D03_${underscoreBase}_memory`;
    for (const [kind, name] of Object.entries({
      runtime: runtimeName,
      memory: memoryName,
    })) {
      if (name.length > 48 || !/^[A-Za-z][A-Za-z0-9_]*$/.test(name)) {
        throw new Error(
          `D03WorkstreamRuntimeMemoryStack: ${kind} name '${name}' is invalid for AgentCore.`,
        );
      }
    }

    // Production is never reversible; nonprod may opt into DESTROY + 7d.
    const retainKey =
      props.envName === "prod" ? true : (props.retainMemoryKey ?? false);
    const keyRemovalPolicy = retainKey
      ? RemovalPolicy.RETAIN
      : RemovalPolicy.DESTROY;
    const keyPendingWindow = retainKey ? Duration.days(30) : Duration.days(7);

    // ---- Memory CMK (rotating, service-scoped) ----
    this.memoryKey = this.buildMemoryKey(
      base,
      memoryName,
      keyRemovalPolicy,
      keyPendingWindow,
    );
    for (const [key, value] of Object.entries(
      this.allocationTagRecord(props),
    )) {
      Tags.of(this.memoryKey).add(key, value);
    }

    // ---- Memory ----
    this.memory = new CfnMemory(this, "Memory", {
      name: memoryName,
      description: `AgentCore Memory for ${props.tenantId}/${props.agentId} (${props.envName}).`,
      eventExpiryDuration: eventExpiryDays,
      encryptionKeyArn: this.memoryKey.keyArn,
      tags: this.allocationTagRecord(props),
    });
    this.memory.applyRemovalPolicy(RemovalPolicy.DESTROY);
    if (props.envName === "prod") {
      this.memory.cfnOptions.updateReplacePolicy = CfnDeletionPolicy.RETAIN;
    }

    // ---- Container image (ARM64) resolved to an exact digest ----
    this.containerDigestUri = this.resolveContainerDigestUri();

    // ---- Runtime ----
    const runtimeRoleArn = this.runtimeExecutionRoleArn(props);
    this.runtime = new CfnRuntime(this, "Runtime", {
      agentRuntimeName: runtimeName,
      description: `AgentCore Runtime for ${props.tenantId}/${props.agentId} (${props.envName}). Inert proven agent; LLM/MCP unwired.`,
      agentRuntimeArtifact: {
        containerConfiguration: {
          containerUri: this.containerDigestUri,
        },
      },
      networkConfiguration: {
        // PUBLIC matches live commit 88d5381. VPC network mode is a documented
        // next gate: it requires AgentCore-compatible subnets/SG plumbed
        // through networkModeConfig and independent live proof.
        networkMode: NETWORK_MODE_PUBLIC,
      },
      roleArn: runtimeRoleArn,
      // The inert proven agent reads ONLY this. No LLM Gateway / MCP wiring.
      environmentVariables: {
        AGENTCORE_MEMORY_ID: this.memory.attrMemoryId,
      },
      tags: this.allocationTagRecord(props),
    });
    this.runtime.applyRemovalPolicy(RemovalPolicy.DESTROY);
    // Runtime depends on Memory: the memory id is injected into its env.
    this.runtime.addDependency(this.memory);

    this.emitOutputs(runtimeRoleArn);
  }

  /** The five allocation tags, as a plain record for the native tags prop. */
  private allocationTagRecord(
    props: D03WorkstreamRuntimeMemoryStackProps,
  ): Record<string, string> {
    return {
      "application-id": props.applicationId,
      "agent-id": props.agentId,
      "tenant-id": props.tenantId,
      "cost-centre": props.costCentre,
      environment: props.envName,
    };
  }

  private buildMemoryKey(
    base: string,
    memoryName: string,
    removalPolicy: RemovalPolicy,
    pendingWindow: Duration,
  ): Key {
    const key = new Key(this, "MemoryKey", {
      alias: `alias/agenticai/d03-runtime-memory-${base}`,
      description: `Workstream-local AgentCore Memory CMK for ${base}.`,
      enableKeyRotation: true,
      pendingWindow,
      removalPolicy,
    });
    // Exact Memory name family + SourceAccount close the confused-deputy gap.
    const memoryArnLike = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:memory/${memoryName}-*`;
    key.addToResourcePolicy(
      new PolicyStatement({
        sid: "AllowAgentCoreMemoryCrypto",
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal("bedrock-agentcore.amazonaws.com")],
        actions: [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ],
        resources: ["*"],
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: { "aws:SourceArn": memoryArnLike },
        },
      }),
    );
    key.addToResourcePolicy(
      new PolicyStatement({
        sid: "AllowAgentCoreMemoryCreateGrant",
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal("bedrock-agentcore.amazonaws.com")],
        actions: ["kms:CreateGrant"],
        resources: ["*"],
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: { "aws:SourceArn": memoryArnLike },
          Bool: { "kms:GrantIsForAWSResource": "true" },
          "ForAllValues:StringEquals": {
            "kms:GrantOperations": [
              "CreateGrant",
              "Decrypt",
              "DescribeKey",
              "GenerateDataKey",
              "GenerateDataKeyWithoutPlaintext",
              "ReEncryptFrom",
              "ReEncryptTo",
            ],
          },
        },
      }),
    );
    return key;
  }

  /**
   * Build the ARM64 image from the spike agent and resolve its content-
   * addressed ECR tag to an exact `@sha256` digest via an ECR `DescribeImages`
   * custom resource. Only `<repositoryUri>@sha256:<digest>` is passed into the
   * Runtime — never a mutable tag. Asset publishing is pipeline-owned.
   */
  private resolveContainerDigestUri(): string {
    const asset = new DockerImageAsset(this, "AgentImage", {
      directory: join(
        __dirname,
        "..",
        "..",
        "..",
        "scripts",
        "live-agentcore-runtime-memory-spike",
        "agent",
      ),
      platform: Platform.LINUX_ARM64,
    });

    // The asset's content hash is its stable physical identity; a content
    // change produces a new tag and forces a fresh digest lookup.
    const digestLookup = new AwsCustomResource(this, "AgentImageDigest", {
      resourceType: "Custom::EcrImageDigest",
      onCreate: {
        service: "ECR",
        action: "describeImages",
        parameters: {
          repositoryName: asset.repository.repositoryName,
          imageIds: [{ imageTag: asset.imageTag }],
        },
        physicalResourceId: PhysicalResourceId.of(
          `EcrImageDigest-${asset.assetHash}`,
        ),
      },
      onUpdate: {
        service: "ECR",
        action: "describeImages",
        parameters: {
          repositoryName: asset.repository.repositoryName,
          imageIds: [{ imageTag: asset.imageTag }],
        },
        physicalResourceId: PhysicalResourceId.of(
          `EcrImageDigest-${asset.assetHash}`,
        ),
      },
      policy: AwsCustomResourcePolicy.fromStatements([
        new PolicyStatement({
          effect: Effect.ALLOW,
          actions: ["ecr:DescribeImages"],
          resources: [asset.repository.repositoryArn],
        }),
      ]),
    });
    const digest = digestLookup.getResponseField("imageDetails.0.imageDigest");
    NagSuppressions.addResourceSuppressions(
      digestLookup,
      [
        {
          id: "AwsSolutions-L1",
          reason: "SEC-006: CDK-managed AwsCustomResource Lambda runtime.",
        },
        {
          id: "NIST.800.53.R5-LambdaConcurrency",
          reason: "SEC-007: CFN-only invocation.",
        },
        {
          id: "NIST.800.53.R5-LambdaDLQ",
          reason: "SEC-008: CFN surfaces failures via stack events.",
        },
        {
          id: "NIST.800.53.R5-LambdaInsideVPC",
          reason: "SEC-009: ECR control-plane public IAM-auth endpoint.",
        },
        {
          id: "AwsSolutions-IAM4",
          appliesTo: [
            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
          ],
          reason: "SEC-010: CDK custom-resource default managed role.",
        },
        {
          id: "AwsSolutions-IAM5",
          reason:
            "SEC-011: The digest-lookup role is scoped to the exact container-assets repository ARN.",
        },
      ],
      true,
    );

    // repositoryUri has no tag/digest; append the resolved digest by reference.
    return `${asset.repository.repositoryUri}@${digest}`;
  }

  /**
   * The stable, prior-stage Runtime execution role ARN. Derived from the exact
   * name family unless an override is supplied.
   */
  private runtimeExecutionRoleArn(
    props: D03WorkstreamRuntimeMemoryStackProps,
  ): string {
    const roleName = `AgenticAI-D03-${props.envName}-${props.tenantId}-${props.agentId}-runtime`;
    if (props.runtimeExecutionRoleArnOverride) {
      const match =
        /^arn:(?:aws|aws-cn|aws-us-gov):iam::(\d{12}):role\/(.+)$/.exec(
          props.runtimeExecutionRoleArnOverride,
        );
      if (!match || match[1] !== this.account || match[2] !== roleName) {
        throw new Error(
          "D03WorkstreamRuntimeMemoryStack: Runtime execution role override must match the exact prior-stage role ARN.",
        );
      }
      return props.runtimeExecutionRoleArnOverride;
    }
    return `arn:${this.partition}:iam::${this.account}:role/${roleName}`;
  }

  private emitOutputs(runtimeRoleArn: string): void {
    // Non-secret identity/status outputs only — never a secret.
    new CfnOutput(this, "RuntimeArn", {
      description: "AgentCore Runtime ARN (non-secret).",
      value: this.runtime.attrAgentRuntimeArn,
    });
    new CfnOutput(this, "RuntimeId", {
      description: "AgentCore Runtime id (non-secret).",
      value: this.runtime.attrAgentRuntimeId,
    });
    new CfnOutput(this, "RuntimeStatus", {
      description: "AgentCore Runtime status (non-secret).",
      value: this.runtime.attrStatus,
    });
    new CfnOutput(this, "MemoryId", {
      description: "AgentCore Memory id (non-secret).",
      value: this.memory.attrMemoryId,
    });
    new CfnOutput(this, "MemoryStatus", {
      description: "AgentCore Memory status (non-secret).",
      value: this.memory.attrStatus,
    });
    new CfnOutput(this, "ContainerDigestUri", {
      description: "Exact digest-pinned Runtime container image URI.",
      value: this.containerDigestUri,
    });
    new CfnOutput(this, "RuntimeExecutionRoleArn", {
      description: "Imported stable Runtime execution role ARN.",
      value: runtimeRoleArn,
    });
  }
}
