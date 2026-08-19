/**
 * PlatformPipelineStack — CDK Pipelines in agenticai-platform-nonprod.
 *
 * Self-mutating pipeline that deploys:
 *   1. Source stage  — GitHub (or CodeCommit) webhook on `main`.
 *   2. Synth stage   — npm ci + build + cdk synth (Jest + conformance).
 *   3. SelfMutate    — pipeline updates itself from new synth output.
 *   4. Deploy Platform non-prod (this account).
 *   5. Manual Approval — security review.
 *   6. Deploy Platform prod.
 *
 * Workload-pipeline is a sibling stack (WorkloadPipelineStack).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, StackProps, Environment } from 'aws-cdk-lib';
import {
  CodePipeline,
  CodePipelineSource,
  ShellStep,
  ManualApprovalStep,
  CodeBuildStep,
  Wave,
} from 'aws-cdk-lib/pipelines';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { GuardrailStack } from '../apps/platform-account/lib/guardrail-stack';
import { RegistryStack } from '../apps/platform-account/lib/registry-stack';
import { LogArchiveStack } from '../apps/platform-account/lib/log-archive-stack';
import { AuditStack } from '../apps/platform-account/lib/audit-stack';

/**
 * Per-stage account env tuple.
 */
export interface PipelineStageEnv {
  readonly env: Required<Environment>;
  readonly envName: 'nonprod' | 'prod';
}

export interface PlatformPipelineStackProps extends StackProps {
  /**
   * GitHub "owner/repo" identifier (e.g. `aws-samples/sample-ai-agent-factory`).
   * The pipeline uses GitHub's V2 source connection — the connection ARN is
   * passed in via `githubConnectionArn`.
   */
  readonly githubRepo: string;
  readonly githubBranch?: string;
  readonly githubConnectionArn: string;
  readonly organizationId: string;
  readonly logArchive: PipelineStageEnv;
  readonly audit: PipelineStageEnv;
  readonly platformNonprod: PipelineStageEnv;
  readonly platformProd: PipelineStageEnv;
  readonly workloadAccountIds: readonly string[];
  /** Role ARN of the pipeline's CodeBuild role — referenced by SCP-05. */
  readonly pipelineRoleArn: string;
}

class PlatformStage extends Stack {
  constructor(scope: Construct, id: string, props: StackProps & {
    envName: 'nonprod' | 'prod';
    organizationId: string;
    workloadAccountIds: readonly string[];
    pipelineRoleArn: string;
  }) {
    super(scope, id, props);
  }
}

/**
 * CDK Pipelines `Stage` subclass that groups all the platform-account stacks
 * deployed per target.
 */
import { Stage, StageProps } from 'aws-cdk-lib';

export interface PlatformDeploymentStageProps extends StageProps {
  readonly envName: 'nonprod' | 'prod';
  readonly organizationId: string;
  readonly workloadAccountIds: readonly string[];
  readonly pipelineRoleArn: string;
  readonly auditEnv: Required<Environment>;
  readonly logArchiveEnv: Required<Environment>;
}

export class PlatformDeploymentStage extends Stage {
  constructor(scope: Construct, id: string, props: PlatformDeploymentStageProps) {
    super(scope, id, props);

    new LogArchiveStack(this, 'LogArchive', {
      env: props.logArchiveEnv,
      organizationId: props.organizationId,
      workloadAccountIds: props.workloadAccountIds,
    });
    new AuditStack(this, 'Audit', {
      env: props.auditEnv,
      organizationId: props.organizationId,
    });
    new GuardrailStack(this, 'Guardrail', {
      env: props.env,
      pipelineRoleArn: props.pipelineRoleArn,
    });
    new RegistryStack(this, 'Registry', {
      env: props.env,
      envName: props.envName,
    });
  }
}

export class PlatformPipelineStack extends Stack {
  readonly pipeline: CodePipeline;

  constructor(scope: Construct, id: string, props: PlatformPipelineStackProps) {
    super(scope, id, props);

    const source = CodePipelineSource.connection(
      props.githubRepo,
      props.githubBranch ?? 'main',
      {
        connectionArn: props.githubConnectionArn,
      },
    );

    this.pipeline = new CodePipeline(this, 'PlatformPipeline', {
      pipelineName: 'agenticai-platform-pipeline',
      crossAccountKeys: true,
      synth: new ShellStep('Synth', {
        input: source,
        commands: [
          'npm ci',
          'npm run build',
          'npm test',
          'npx cdk synth',
        ],
      }),
      publishAssetsInParallel: false,
    });

    // Non-prod stage
    this.pipeline.addStage(
      new PlatformDeploymentStage(this, 'Nonprod', {
        env: props.platformNonprod.env,
        envName: 'nonprod',
        organizationId: props.organizationId,
        workloadAccountIds: props.workloadAccountIds,
        pipelineRoleArn: props.pipelineRoleArn,
        auditEnv: props.audit.env,
        logArchiveEnv: props.logArchive.env,
      }),
    );

    // Manual approval + prod stage
    this.pipeline.addStage(
      new PlatformDeploymentStage(this, 'Prod', {
        env: props.platformProd.env,
        envName: 'prod',
        organizationId: props.organizationId,
        workloadAccountIds: props.workloadAccountIds,
        pipelineRoleArn: props.pipelineRoleArn,
        auditEnv: props.audit.env,
        logArchiveEnv: props.logArchive.env,
      }),
      {
        pre: [new ManualApprovalStep('SecurityReview')],
      },
    );

    // cdk-nag suppression noise from self-mutate Lambdas and pipeline roles.
    NagSuppressions.addStackSuppressions(
      this,
      [
        { id: 'AwsSolutions-CB4', reason: 'SEC-017: CDK Pipelines CodeBuild uses default KMS key for artifact encryption; crossAccountKeys=true enables cross-account KMS automatically.' },
        { id: 'AwsSolutions-IAM5', reason: 'SEC-011: Pipeline roles require wildcards for CDK bootstrap operations (CloudFormation CreateStack, asset publishing, etc.).' },
        { id: 'AwsSolutions-S1', reason: 'SEC-001: Pipeline artifact bucket logging is managed by CDK Pipelines itself.' },
        { id: 'AwsSolutions-L1', reason: 'SEC-006: CDK Pipelines Lambda runtimes track aws-cdk-lib bumps.' },
        { id: 'NIST.800.53.R5-CodeBuildProjectEnvVarAwsCred', reason: 'SEC-018: CDK Pipelines CodeBuild reads CDK bootstrap role credentials via STS at runtime, not env vars.' },
        { id: 'NIST.800.53.R5-CodeBuildProjectKMSEncryptedArtifacts', reason: 'SEC-017: CDK Pipelines manages artifact encryption keys; cross-account sharing requires the default managed key behaviour.' },
        { id: 'NIST.800.53.R5-CodeBuildProjectPrivilegedModeDisabled', reason: 'SEC-019: Synth/build steps run in standard (non-privileged) containers.' },
        { id: 'NIST.800.53.R5-CodeBuildProjectSourceRepoUrl', reason: 'SEC-020: Source comes from CodeStar Connections (GitHub V2), which is the recommended managed path.' },
        { id: 'NIST.800.53.R5-IAMNoInlinePolicy', reason: 'SEC-005: CDK Pipelines auto-generated roles use inline policies.' },
        { id: 'NIST.800.53.R5-S3BucketLoggingEnabled', reason: 'SEC-001: Pipeline artifact bucket.' },
        { id: 'NIST.800.53.R5-S3BucketReplicationEnabled', reason: 'SEC-002: CRR deferred to v2 DR roadmap.' },
        { id: 'NIST.800.53.R5-S3DefaultEncryptionKMS', reason: 'SEC-003: Pipeline artifact buckets use CDK-managed encryption.' },
        { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: CDK self-mutate Lambdas.' },
        { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: CFN custom-resource Lambdas surface failures via stack events.' },
        { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: Pipeline Lambdas call CodePipeline/CloudFormation control plane.' },
        { id: 'AwsSolutions-KMS5', reason: 'SEC-021: CDK Pipelines-generated artifact-bucket KMS key does not expose rotation toggle; tracks CDK defaults.' },
        { id: 'NIST.800.53.R5-KMSBackingKeyRotationEnabled', reason: 'SEC-021: Same as SEC-021 above — CDK-managed key.' },
        { id: 'NIST.800.53.R5-S3BucketVersioningEnabled', reason: 'SEC-022: CDK-Pipelines artifact bucket is ephemeral + lifecycle-managed; versioning adds cost without recovery value for pipeline artifacts.' },
      ],
      true,
    );

    // Silence unused imports for future extension hooks.
    void CodeBuildStep;
    void Wave;
    void PlatformStage;
  }
}
