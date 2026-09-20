/**
 * PlatformPipelineStack — CDK Pipelines in agenticai-platform-nonprod.
 *
 * The pipeline self-synthesizes and deploys the shared governance stacks plus
 * the native AgentCore inference Gateway into isolated nonproduction and
 * production Platform accounts.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, StackProps, Environment, Stage, StageProps } from "aws-cdk-lib";
import { PipelineType } from "aws-cdk-lib/aws-codepipeline";
import { Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import {
  CodePipeline,
  CodePipelineSource,
  ShellStep,
  ManualApprovalStep,
  CodeBuildStep,
  Wave,
} from "aws-cdk-lib/pipelines";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

import { GuardrailStack } from "../apps/platform-account/lib/guardrail-stack";
import { RegistryStack } from "../apps/platform-account/lib/registry-stack";
import { LogArchiveStack } from "../apps/platform-account/lib/log-archive-stack";
import { AuditStack } from "../apps/platform-account/lib/audit-stack";
import { InferenceGatewayStack } from "../apps/platform-account/lib/inference-gateway-stack";
import type { InferenceModelRateLimit } from "@agenticai/platform-inference-gateway";
import {
  applyPipelineResourceTags,
  createPipelineArtifactBucket,
  type PipelineResourceTags,
} from "./pipeline-artifacts";
import { stageAwareSynthCommands } from "./synth-commands";

/** Per-stage account environment tuple. */
export interface PipelineStageEnv {
  readonly env: Required<Environment>;
  readonly envName: "nonprod" | "prod";
}

export type GaRegistryRecordGenerationsByEnvironment = Readonly<
  Partial<Record<"nonprod" | "prod", Readonly<Record<string, number>>>>
>;

export interface PlatformPipelineStackProps extends StackProps {
  readonly githubRepo: string;
  readonly githubBranch?: string;
  readonly githubConnectionArn: string;
  readonly organizationId: string;
  readonly logArchive: PipelineStageEnv;
  readonly audit: PipelineStageEnv;
  readonly platformNonprod: PipelineStageEnv;
  readonly platformProd: PipelineStageEnv;
  readonly workloadAccountIds: readonly string[];
  readonly applicationId: string;
  readonly agentId: string;
  readonly tenantId: string;
  readonly costCentre: string;
  readonly inferenceModelRateLimits: readonly InferenceModelRateLimit[];
  readonly grantGatewayInvokePermissions?: boolean;
  readonly gatewayServiceRoleArns?: readonly string[];
  readonly gatewayWorkloadAccountIds?: Readonly<
    Record<"nonprod" | "prod", string>
  >;
  readonly gaRegistryRecordGenerations?: GaRegistryRecordGenerationsByEnvironment;

  /** Explicit stage passed to the pipeline's own synth command. */
  readonly synthStage?: string;

  /** Extra `agenticai/*` context merged over values derived from props. */
  readonly synthContext?: Record<string, string>;
}

class PlatformStage extends Stack {
  constructor(
    scope: Construct,
    id: string,
    props: StackProps & {
      envName: "nonprod" | "prod";
      organizationId: string;
      workloadAccountIds: readonly string[];
      pipelineRoleArn: string;
    },
  ) {
    super(scope, id, props);
  }
}

export interface PlatformDeploymentStageProps extends StageProps {
  readonly envName: "nonprod" | "prod";
  readonly organizationId: string;
  readonly workloadAccountIds: readonly string[];
  readonly registrySynthAccountId: string;
  readonly pipelineRoleArn: string;
  readonly auditEnv: Required<Environment>;
  readonly logArchiveEnv: Required<Environment>;
  readonly retainGovernanceOnDelete: boolean;
  readonly existingGuardrailAdminRoleArn?: string;
  readonly baselineGuardrailName?: string;
  readonly applicationId: string;
  readonly agentId: string;
  readonly tenantId: string;
  readonly costCentre: string;
  readonly inferenceModelRateLimits: readonly InferenceModelRateLimit[];
  readonly grantGatewayInvokePermissions?: boolean;
  readonly gatewayServiceRoleArns?: readonly string[];
  readonly gatewayWorkloadAccountId?: string;
  readonly gaRegistryRecordGenerations?: Readonly<Record<string, number>>;
}

export class PlatformDeploymentStage extends Stage {
  constructor(
    scope: Construct,
    id: string,
    props: PlatformDeploymentStageProps,
  ) {
    super(scope, id, props);

    // Management/Governance is shared across Platform environments. Keep these
    // stacks under the first stage so existing deployed stack identities remain
    // stable, and never create conflicting copies in the production stage.
    if (props.envName === "nonprod") {
      new LogArchiveStack(this, "LogArchive", {
        env: props.logArchiveEnv,
        organizationId: props.organizationId,
        workloadAccountIds: props.workloadAccountIds,
        retainOnDelete: props.retainGovernanceOnDelete,
      });
      new AuditStack(this, "Audit", {
        env: props.auditEnv,
        organizationId: props.organizationId,
      });
    }
    new GuardrailStack(this, "Guardrail", {
      env: props.env,
      pipelineRoleArn: props.pipelineRoleArn,
      existingAdminRoleArn: props.existingGuardrailAdminRoleArn,
      baselineGuardrailName: props.baselineGuardrailName,
    });
    new RegistryStack(this, "Registry", {
      env: props.env,
      envName: props.envName,
      workloadAccountIds: props.workloadAccountIds,
      registrySynthAccountId: props.registrySynthAccountId,
      grantGatewayInvokePermissions: props.grantGatewayInvokePermissions,
      gatewayServiceRoleArns: props.gatewayServiceRoleArns,
      gatewayWorkloadAccountId: props.gatewayWorkloadAccountId,
      gaRegistryRecordGenerations: props.gaRegistryRecordGenerations,
      applicationId: props.applicationId,
      agentId: props.agentId,
      tenantId: props.tenantId,
      costCentre: props.costCentre,
    });
    new InferenceGatewayStack(this, "InferenceGateway", {
      env: props.env,
      envName: props.envName,
      applicationId: props.applicationId,
      agentId: props.agentId,
      tenantId: props.tenantId,
      costCentre: props.costCentre,
      modelRateLimits: props.inferenceModelRateLimits,
    });
  }
}

export class PlatformPipelineStack extends Stack {
  readonly pipeline: CodePipeline;

  constructor(scope: Construct, id: string, props: PlatformPipelineStackProps) {
    super(scope, id, props);

    const resourceTags: PipelineResourceTags = {
      applicationId: props.applicationId,
      agentId: props.agentId,
      tenantId: props.tenantId,
      costCentre: props.costCentre,
      environment: "pipeline",
    };
    applyPipelineResourceTags(this, resourceTags);
    const artifactBucket = createPipelineArtifactBucket(
      this,
      "PlatformPipelineArtifacts",
      resourceTags,
    );
    const pipelineServiceRoleName = "AgenticAI-PlatformPipelineRole";
    const pipelineServiceRole = new Role(this, "PlatformPipelineServiceRole", {
      roleName: pipelineServiceRoleName,
      assumedBy: new ServicePrincipal("codepipeline.amazonaws.com"),
      description:
        "Stable service role for the platform pipeline and Guardrail administration trust.",
    });
    const pipelineServiceRoleArn = `arn:${this.partition}:iam::${this.account}:role/${pipelineServiceRoleName}`;

    const source = CodePipelineSource.connection(
      props.githubRepo,
      props.githubBranch ?? "main",
      { connectionArn: props.githubConnectionArn },
    );

    this.pipeline = new CodePipeline(this, "PlatformPipeline", {
      artifactBucket,
      role: pipelineServiceRole,
      pipelineName: "agenticai-platform-pipeline",
      pipelineType: PipelineType.V2,
      crossAccountKeys: true,
      synth: new ShellStep("Synth", {
        input: source,
        commands: stageAwareSynthCommands({
          stage: props.synthStage ?? "pipeline",
          context: this.synthContext(props, pipelineServiceRoleArn),
          expectedStackArtifactId: this.stackName,
          expectedStageAssemblyGlobs: [
            "cdk.out/assembly-*Nonprod",
            "cdk.out/assembly-*Prod",
          ],
        }),
      }),
      publishAssetsInParallel: false,
    });

    const sharedGatewayProps = {
      applicationId: props.applicationId,
      agentId: props.agentId,
      tenantId: props.tenantId,
      costCentre: props.costCentre,
      inferenceModelRateLimits: props.inferenceModelRateLimits,
      registrySynthAccountId: props.platformNonprod.env.account,
      grantGatewayInvokePermissions: props.grantGatewayInvokePermissions,
      gatewayServiceRoleArns: props.gatewayServiceRoleArns,
    };
    const platformAccountIsShared =
      props.platformNonprod.env.account === props.platformProd.env.account;
    const platformRegionIsShared =
      props.platformNonprod.env.region === props.platformProd.env.region;
    const sharedGuardrailAdminRoleArn = platformAccountIsShared
      ? `arn:${this.partition}:iam::${props.platformNonprod.env.account}:role/AgenticAI-GuardrailAdmin`
      : undefined;

    this.pipeline.addStage(
      new PlatformDeploymentStage(this, "Nonprod", {
        env: props.platformNonprod.env,
        envName: "nonprod",
        organizationId: props.organizationId,
        workloadAccountIds: props.workloadAccountIds,
        gatewayWorkloadAccountId: props.gatewayWorkloadAccountIds?.nonprod,
        gaRegistryRecordGenerations: props.gaRegistryRecordGenerations?.nonprod,
        pipelineRoleArn: pipelineServiceRoleArn,
        auditEnv: props.audit.env,
        logArchiveEnv: props.logArchive.env,
        retainGovernanceOnDelete: props.logArchive.envName === "prod",
        ...sharedGatewayProps,
      }),
    );

    this.pipeline.addStage(
      new PlatformDeploymentStage(this, "Prod", {
        env: props.platformProd.env,
        envName: "prod",
        organizationId: props.organizationId,
        workloadAccountIds: props.workloadAccountIds,
        gatewayWorkloadAccountId: props.gatewayWorkloadAccountIds?.prod,
        gaRegistryRecordGenerations: props.gaRegistryRecordGenerations?.prod,
        pipelineRoleArn: pipelineServiceRoleArn,
        auditEnv: props.audit.env,
        logArchiveEnv: props.logArchive.env,
        retainGovernanceOnDelete: props.logArchive.envName === "prod",
        existingGuardrailAdminRoleArn: sharedGuardrailAdminRoleArn,
        baselineGuardrailName:
          platformAccountIsShared && platformRegionIsShared
            ? "agenticai-guardrail-baseline-prod"
            : undefined,
        ...sharedGatewayProps,
      }),
      { pre: [new ManualApprovalStep("SecurityReview")] },
    );

    NagSuppressions.addStackSuppressions(
      this,
      [
        {
          id: "AwsSolutions-CB4",
          reason:
            "SEC-017: CodeBuild artifacts flow through the explicit customer-managed, rotating pipeline artifact CMK.",
        },
        {
          id: "AwsSolutions-IAM5",
          reason:
            "SEC-011: Pipeline roles require wildcards for CDK bootstrap operations (CloudFormation CreateStack, asset publishing, etc.).",
        },
        {
          id: "AwsSolutions-S1",
          reason:
            "SEC-001: Pipeline artifacts expire after 30 days; CodePipeline execution history and CloudTrail provide the audit trail without a recursive access-log bucket.",
        },
        {
          id: "AwsSolutions-L1",
          reason:
            "SEC-006: CDK Pipelines Lambda runtimes track aws-cdk-lib bumps.",
        },
        {
          id: "NIST.800.53.R5-CodeBuildProjectEnvVarAwsCred",
          reason:
            "SEC-018: CDK Pipelines CodeBuild reads CDK bootstrap role credentials via STS at runtime, not env vars.",
        },
        {
          id: "NIST.800.53.R5-CodeBuildProjectKMSEncryptedArtifacts",
          reason:
            "SEC-017: The explicit pipeline artifact bucket uses a customer-managed rotating KMS key shared with cross-account stages.",
        },
        {
          id: "NIST.800.53.R5-CodeBuildProjectPrivilegedModeDisabled",
          reason:
            "SEC-019: Synth/build steps run in standard (non-privileged) containers.",
        },
        {
          id: "NIST.800.53.R5-CodeBuildProjectSourceRepoUrl",
          reason:
            "SEC-020: Source comes from CodeStar Connections (GitHub V2), which is the recommended managed path.",
        },
        {
          id: "NIST.800.53.R5-IAMNoInlinePolicy",
          reason:
            "SEC-005: CDK Pipelines auto-generated roles use inline policies.",
        },
        {
          id: "NIST.800.53.R5-S3BucketLoggingEnabled",
          reason:
            "SEC-001: The short-lived artifact bucket uses a 30-day lifecycle; pipeline execution history and CloudTrail retain access evidence.",
        },
        {
          id: "NIST.800.53.R5-S3BucketReplicationEnabled",
          reason: "SEC-002: CRR deferred to v2 DR roadmap.",
        },
        {
          id: "NIST.800.53.R5-S3DefaultEncryptionKMS",
          reason:
            "SEC-003: The artifact bucket uses an explicit customer-managed rotating KMS key.",
        },
        {
          id: "NIST.800.53.R5-LambdaConcurrency",
          reason:
            "SEC-007: CDK self-mutate and artifact-cleanup Lambdas are provisioning-time only.",
        },
        {
          id: "NIST.800.53.R5-LambdaDLQ",
          reason:
            "SEC-008: CFN custom-resource Lambdas surface failures via stack events.",
        },
        {
          id: "NIST.800.53.R5-LambdaInsideVPC",
          reason:
            "SEC-009: Pipeline Lambdas call CodePipeline/CloudFormation/S3 control planes.",
        },
        {
          id: "NIST.800.53.R5-S3BucketVersioningEnabled",
          reason:
            "SEC-022: Pipeline artifacts are immutable per execution, expire after 30 days, and are automatically removed with the stack.",
        },
      ],
      true,
    );

    void CodeBuildStep;
    void Wave;
    void PlatformStage;
  }

  private synthContext(
    props: PlatformPipelineStackProps,
    pipelineServiceRoleArn: string,
  ): Record<string, string> {
    const derived: Record<string, string> = {
      "agenticai/githubRepo": props.githubRepo,
      "agenticai/githubConnectionArn": props.githubConnectionArn,
      "agenticai/pipelineSelection": "platform",
      "agenticai/organizationId": props.organizationId,
      "agenticai/platformNonprodAccountId": props.platformNonprod.env.account,
      "agenticai/platformProdAccountId": props.platformProd.env.account,
      "agenticai/auditAccountId": props.audit.env.account,
      "agenticai/logArchiveAccountId": props.logArchive.env.account,
      "agenticai/pipelineRoleArn": pipelineServiceRoleArn,
      "agenticai/workloadAccountIds": JSON.stringify(props.workloadAccountIds),
      "agenticai/applicationId": props.applicationId,
      "agenticai/agentId": props.agentId,
      "agenticai/tenantId": props.tenantId,
      "agenticai/costCentre": props.costCentre,
      "agenticai/inferenceModelRateLimits": JSON.stringify(
        props.inferenceModelRateLimits,
      ),
    };
    if (props.githubBranch) {
      derived["agenticai/githubBranch"] = props.githubBranch;
    }
    if (props.gatewayWorkloadAccountIds) {
      derived["agenticai/workloadNonprodAccountId"] =
        props.gatewayWorkloadAccountIds.nonprod;
      derived["agenticai/workloadProdAccountId"] =
        props.gatewayWorkloadAccountIds.prod;
    }
    if (props.grantGatewayInvokePermissions) {
      derived["agenticai/enableGaGatewayInvokePermissions"] = "true";
      derived["agenticai/gaGatewayServiceRoleArns"] = JSON.stringify(
        props.gatewayServiceRoleArns ?? [],
      );
    }
    if (
      props.gaRegistryRecordGenerations &&
      Object.keys(props.gaRegistryRecordGenerations).length > 0
    ) {
      derived["agenticai/gaRegistryRecordGenerations"] = JSON.stringify(
        props.gaRegistryRecordGenerations,
      );
    }
    return {
      ...derived,
      ...(props.synthContext ?? {}),
      "agenticai/pipelineSelection": "platform",
      "agenticai/pipelineRoleArn": pipelineServiceRoleArn,
    };
  }
}
