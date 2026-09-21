/**
 * WorkloadPipelineStack — per-application pipeline.
 *
 * Deployed in the platform account. Pipeline stages:
 *
 *   Source → Synth → Deploy(workload-nonprod)
 *          → Evaluation gate (CodeBuild) — Strands regression + guardrail-
 *            violation-rate + response-quality + tool-success + p99-latency
 *          → Manual approval → Canary deploy → Canary soak
 *          → Deploy(workload-prod)
 *
 * Spec: §1.3.5 L210-212 mandatory stage sequence (R-DEVX-002).
 *
 * Round 1B fail-closed invariants (tasks/todo.md §Round 1 B):
 *   1. The synth command is stage-aware. `bin/agentic-ai-platform.ts` routes on
 *      the `stage` context value. The app rejects a missing stage, and this
 *      synth step also passes it explicitly before asserting that the assembly
 *      contains this pipeline's own stack plus a non-empty nested assembly per
 *      deployment stage.
 *   2. Promotion order is expressed in the CDK step dependency graph, not by
 *      array position: EvaluationGate → ProdApproval → CanaryDeploy →
 *      CanarySoak. Steps without declared dependencies run in PARALLEL in
 *      CDK Pipelines, so array order alone proves nothing.
 *   3. No step may convert a missing resource or a failed CLI call into
 *      success. There are no `|| true` / `|| echo` fallbacks: a missing alarm,
 *      an unusable alarm state, or an unimplemented canary API all fail the
 *      pipeline.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  Duration,
  Environment,
  Stack,
  StackProps,
  Stage,
  StageProps,
} from "aws-cdk-lib";
import { PipelineType } from "aws-cdk-lib/aws-codepipeline";
import {
  BuildSpec,
  LinuxArmBuildImage,
  LinuxBuildImage,
} from "aws-cdk-lib/aws-codebuild";
import { PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import {
  CodeBuildStep,
  CodePipeline,
  CodePipelineSource,
  ManualApprovalStep,
  ShellStep,
  Step,
} from "aws-cdk-lib/pipelines";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

import { WorkloadNetworkStack } from "../apps/workload-account/lib/workload-network-stack";
import { WorkloadAppStack } from "../apps/workload-account/lib/workload-app-stack";
import {
  D03WorkstreamGatewayStack,
  type GatewayPolicyEngineMode,
} from "../apps/platform-account/lib/d03-workstream-gateway-stack";
import { D03WorkstreamRegistryRolesStack } from "../apps/workload-account/lib/d03-workstream-registry-roles-stack";
import { D03WorkstreamRuntimeMemoryStack } from "../apps/workload-account/lib/d03-workstream-runtime-memory-stack";
import type { GaRegistryConsumerContext } from "@agenticai/agent-registry";
import {
  applyPipelineResourceTags,
  createPipelineArtifactBucket,
  type PipelineResourceTags,
} from "./pipeline-artifacts";
import { stageAwareSynthCommands } from "./synth-commands";

export interface WorkloadGaRegistryConfig {
  readonly nonprod: GaRegistryConsumerContext;
  readonly prod: GaRegistryConsumerContext;
  /** Region where the per-workstream AgentCore tool Gateways are deployed. */
  readonly gatewayRegion: string;
}

export interface WorkloadPolicyEngineConfig {
  readonly mode: Exclude<GatewayPolicyEngineMode, "OFF">;
  readonly nonprodIamRoleArns: readonly string[];
  readonly prodIamRoleArns: readonly string[];
}

export interface WorkloadStageProps extends StageProps {
  readonly envName: "nonprod" | "prod";
  readonly tenantId: string;
  readonly agentId: string;
  readonly applicationId: string;
  readonly costCentre: string;
  readonly availabilityZones: readonly string[];
  readonly gaRegistryContext?: GaRegistryConsumerContext;
  readonly gatewayRegion?: string;
  readonly policyEngineMode?: GatewayPolicyEngineMode;
  readonly policyEngineIamRoleArns?: readonly string[];
  /** Opt-in: also deploy the native Runtime+Memory foundation (default false). */
  readonly enablePipelineRuntimeMemory?: boolean;
  readonly auditOamSinkArn?: string;
  readonly notificationEmail?: string;
}

export class WorkloadDeploymentStage extends Stage {
  readonly networkStack?: WorkloadNetworkStack;
  readonly appStack?: WorkloadAppStack;
  readonly gatewayStack?: D03WorkstreamGatewayStack;
  readonly runtimeMemoryStack?: D03WorkstreamRuntimeMemoryStack;

  constructor(scope: Construct, id: string, props: WorkloadStageProps) {
    super(scope, id, props);

    if (props.gaRegistryContext) {
      const workloadAccountId = props.env?.account;
      if (!workloadAccountId || !props.gatewayRegion) {
        throw new Error(
          "WorkloadDeploymentStage: GA Registry mode requires an account and gatewayRegion.",
        );
      }
      const rolePrefix = `arn:aws:iam::${workloadAccountId}:role`;
      this.gatewayStack = new D03WorkstreamGatewayStack(this, "ToolGateway", {
        stackName: `AgenticAI-${props.tenantId}-${props.agentId}-${props.envName}-ToolGateway`,
        env: { account: workloadAccountId, region: props.gatewayRegion },
        envName: props.envName,
        tenantId: props.tenantId,
        agentId: props.agentId,
        applicationId: props.applicationId,
        costCentre: props.costCentre,
        workloadAccountId,
        platformAccountId: props.gaRegistryContext.platformAccountId,
        gaRegistryContext: props.gaRegistryContext,
        policyEngineMode: props.policyEngineMode,
        policyEngineIamRoleArns: props.policyEngineIamRoleArns,
        gatewayServiceRoleArnOverride: `${rolePrefix}/AgenticAI-D03-${props.envName}-${props.tenantId}-${props.agentId}-gw-svc`,
        registryValidatorRoleArnOverride: `${rolePrefix}/AgenticAI-D03-${props.envName}-${props.tenantId}-${props.agentId}-RegistryValidator`,
        crExecRoleArnOverride: `${rolePrefix}/AgenticAI-D03-${props.envName}-GatewayAdmin`,
      });

      // Opt-in native Runtime+Memory foundation. The default keeps the exact R2
      // Gateway-only rollback shape (this stack is simply not created). When
      // enabled it depends on the ToolGateway so a stage is a coherent unit.
      if (props.enablePipelineRuntimeMemory) {
        this.runtimeMemoryStack = new D03WorkstreamRuntimeMemoryStack(
          this,
          "RuntimeMemory",
          {
            stackName: `AgenticAI-${props.tenantId}-${props.agentId}-${props.envName}-RuntimeMemory`,
            env: { account: workloadAccountId, region: props.gatewayRegion },
            envName: props.envName,
            applicationId: props.applicationId,
            agentId: props.agentId,
            tenantId: props.tenantId,
            costCentre: props.costCentre,
            runtimeExecutionRoleArnOverride: `${rolePrefix}/AgenticAI-D03-${props.envName}-${props.tenantId}-${props.agentId}-runtime`,
          },
        );
        this.runtimeMemoryStack.addDependency(this.gatewayStack);
      }
      return;
    }

    this.networkStack = new WorkloadNetworkStack(this, "Network", {
      env: props.env,
      availabilityZones: props.availabilityZones,
    });
    this.appStack = new WorkloadAppStack(this, "App", {
      env: props.env,
      vpcId: this.networkStack.vpc.vpc.vpcId,
      workloadSubnetIds: this.networkStack.vpc.vpc.selectSubnets({
        subnetGroupName: "workload",
      }).subnetIds,
      workloadSubnetRouteTableIds: this.networkStack.vpc.vpc
        .selectSubnets({ subnetGroupName: "workload" })
        .subnets.map((subnet) => subnet.routeTable.routeTableId),
      vpcCidr: this.networkStack.vpc.vpc.vpcCidrBlock,
      availabilityZones: this.networkStack.vpc.vpc.availabilityZones,
      bedrockRuntimeVpceId:
        this.networkStack.vpc.endpoints.bedrockRuntime.vpcEndpointId,
      vpceSecurityGroupId: this.networkStack.vpc.vpceEniSg.securityGroupId,
      envName: props.envName,
      tenantId: props.tenantId,
      agentId: props.agentId,
      costCentre: props.costCentre,
      auditOamSinkArn: props.auditOamSinkArn,
      notificationEmail: props.notificationEmail,
    });
    this.appStack.addDependency(this.networkStack);
  }
}

export interface WorkstreamRegistryRolesStageProps extends StageProps {
  readonly tenantId: string;
  readonly agentId: string;
  readonly applicationId: string;
  readonly costCentre: string;
  readonly gatewayRegion: string;
  readonly workloadNonprodAccountId: string;
  readonly workloadProdAccountId: string;
  readonly nonprodContext: GaRegistryConsumerContext;
  readonly prodContext: GaRegistryConsumerContext;
  readonly enablePipelineRuntimeMemory?: boolean;
}

export class WorkstreamRegistryRolesStage extends Stage {
  readonly nonprod: D03WorkstreamRegistryRolesStack;
  readonly prod: D03WorkstreamRegistryRolesStack;

  constructor(
    scope: Construct,
    id: string,
    props: WorkstreamRegistryRolesStageProps,
  ) {
    super(scope, id, props);
    this.nonprod = new D03WorkstreamRegistryRolesStack(this, "NonprodRoles", {
      stackName: `AgenticAI-${props.tenantId}-${props.agentId}-nonprod-RegistryRoles`,
      env: {
        account: props.workloadNonprodAccountId,
        region: props.gatewayRegion,
      },
      envName: "nonprod",
      tenantId: props.tenantId,
      agentId: props.agentId,
      applicationId: props.applicationId,
      costCentre: props.costCentre,
      registryContext: props.nonprodContext,
      enablePipelineRuntimeMemory: props.enablePipelineRuntimeMemory,
    });
    this.prod = new D03WorkstreamRegistryRolesStack(this, "ProdRoles", {
      stackName: `AgenticAI-${props.tenantId}-${props.agentId}-prod-RegistryRoles`,
      env: {
        account: props.workloadProdAccountId,
        region: props.gatewayRegion,
      },
      envName: "prod",
      tenantId: props.tenantId,
      agentId: props.agentId,
      applicationId: props.applicationId,
      costCentre: props.costCentre,
      registryContext: props.prodContext,
      enablePipelineRuntimeMemory: props.enablePipelineRuntimeMemory,
    });
  }
}

export interface WorkloadPipelineStackProps extends StackProps {
  readonly githubRepo: string;
  readonly githubBranch?: string;
  readonly githubConnectionArn: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly applicationId?: string;
  readonly costCentre: string;
  readonly gaRegistry?: WorkloadGaRegistryConfig;
  /** Opt-in Gateway PolicyEngine migration; omitted preserves the R2 rollback template. */
  readonly policyEngine?: WorkloadPolicyEngineConfig;
  /**
   * Opt-in native Runtime+Memory foundation. Default false preserves the exact
   * R2 Gateway-only rollback graph. Requires GA Registry mode.
   */
  readonly enablePipelineRuntimeMemory?: boolean;
  readonly workloadNonprodEnv: Required<Environment>;
  readonly workloadProdEnv: Required<Environment>;
  /** Account-specific AZ names produced by read-only preflight. */
  readonly workloadNonprodAvailabilityZones: readonly string[];
  /** Account-specific AZ names produced by read-only preflight. */
  readonly workloadProdAvailabilityZones: readonly string[];
  readonly auditOamSinkArn?: string;
  readonly notificationEmail?: string;

  /**
   * Evaluation gate thresholds (R-DEVX-002).
   * The 5 legacy categories + the 2 added by Phase A
   * (BLUEPRINT_GAP_ANALYSIS Partial-1).
   */
  readonly evalRegressionPassRate?: number; // default 95 (%)
  readonly evalGuardrailViolationRate?: number; // default 1 (%)
  readonly evalQualityScoreMin?: number; // default 85 (%)
  readonly evalToolSuccessRate?: number; // default 98 (%)
  readonly evalFirstTokenP99Ms?: number; // default 1500
  /** Phase A — refusal rate on the adversarial corpus. */
  readonly evalRefusalRateMin?: number; // default 99 (%)
  /** Phase A — per-prompt USD ceiling. */
  readonly evalCostPerPromptMaxUsd?: number; // default 0.05
  /** Z7-B — canary traffic percentage; default 5. */
  readonly canaryPercent?: number;
  /** Z7-B — canary soak duration (minutes); default 30. */
  readonly canarySoakMinutes?: number;

  /**
   * `stage` context value the pipeline's own synth step passes to
   * `bin/agentic-ai-platform.ts`. Defaults to `pipeline`, the stage that
   * instantiates this stack. Never omit it — the app's `undefined` stage branch
   * synthesises an empty assembly and exits 0.
   */
  readonly synthStage?: string;

  /**
   * Extra `agenticai/*` context keys for the synth step, merged over the keys
   * derived from this stack's own props (explicit wins).
   *
   * When the same root also synthesizes Platform stages, pass their organization,
   * account, audit, and Log Archive context here. The Platform pipeline derives
   * its owned service-role ARN itself. If required context is missing the app
   * throws inside the synth step, which fails the pipeline loudly; it never
   * degrades to an empty assembly.
   */
  readonly synthContext?: Record<string, string>;

  /**

   * Commands that actually shift canary traffic to the new agent version.
   *
   * Left unset, `CanaryDeploy` is an explicit fail-closed placeholder that
   * exits non-zero: no AgentCore traffic-shifting call is implemented yet
   * (tasks/todo.md Round 4), and a placeholder must never report success.
   */
  readonly canaryDeployCommands?: readonly string[];
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function registryResolverCommand(
  context: GaRegistryConsumerContext,
  outputVariable: string,
  applicationId: string,
  tenantId: string,
  agentId: string,
  costCentre: string,
): string {
  const expectedTools = context.records
    .map((record) => record.document.toolId)
    .map((toolId) => `--expected-tool-id ${shellQuote(toolId)}`)
    .join(" ");
  const outputName = `.agenticai-ga-registry-${context.environment}.json`;
  const invocation = [
    '"$PWD/.agenticai-ga-resolver-venv/bin/python"',
    "pipelines/resolve_ga_registry_context.py",
    "--assume-reader",
    `--account-id ${shellQuote(context.platformAccountId)}`,
    `--region ${shellQuote(context.region)}`,
    `--environment ${shellQuote(context.environment)}`,
    `--application-id ${shellQuote(applicationId)}`,
    `--agent-id ${shellQuote(agentId)}`,
    `--tenant-id ${shellQuote(tenantId)}`,
    `--cost-centre ${shellQuote(costCentre)}`,
    expectedTools,
    '--source-revision "${CODEBUILD_RESOLVED_SOURCE_VERSION:-}"',
    `--output "$${outputVariable}"`,
  ].join(" ");
  return (
    `${outputVariable}="$PWD/${outputName}"; ` +
    `export ${outputVariable}; ` +
    invocation
  );
}

function registryResolverCommands(
  config: WorkloadGaRegistryConfig,
  applicationId: string,
  tenantId: string,
  agentId: string,
  costCentre: string,
): {
  readonly commands: readonly string[];
  readonly contextFromEnvironment: Readonly<Record<string, string>>;
} {
  return {
    commands: [
      'python3 -m venv "$PWD/.agenticai-ga-resolver-venv"',
      '"$PWD/.agenticai-ga-resolver-venv/bin/pip" install --disable-pip-version-check -r pipelines/requirements-ga-registry-resolver.txt',
      registryResolverCommand(
        config.nonprod,
        "GA_REGISTRY_NONPROD_CONTEXT_FILE",
        applicationId,
        tenantId,
        agentId,
        costCentre,
      ),
      registryResolverCommand(
        config.prod,
        "GA_REGISTRY_PROD_CONTEXT_FILE",
        applicationId,
        tenantId,
        agentId,
        costCentre,
      ),
    ],
    contextFromEnvironment: {
      "agenticai/gaRegistryNonprodContextFile":
        "GA_REGISTRY_NONPROD_CONTEXT_FILE",
      "agenticai/gaRegistryProdContextFile": "GA_REGISTRY_PROD_CONTEXT_FILE",
    },
  };
}

function registrySynthRoleName(tenantId: string, agentId: string): string {
  const name = `AgenticAI-WLP-${tenantId}-${agentId}-RegistrySynth`;
  if (name.length > 64 || !/^[A-Za-z0-9+=,.@_-]+$/.test(name)) {
    throw new Error(
      "WorkloadPipelineStack: tenantId/agentId produce an invalid Registry synth role name.",
    );
  }
  return name;
}

export class WorkloadPipelineStack extends Stack {
  readonly pipeline: CodePipeline;

  constructor(scope: Construct, id: string, props: WorkloadPipelineStackProps) {
    super(scope, id, props);

    const applicationId = props.applicationId ?? props.tenantId;
    if (props.gaRegistry) {
      const { nonprod, prod, gatewayRegion } = props.gaRegistry;
      if (nonprod.environment !== "nonprod" || prod.environment !== "prod") {
        throw new Error(
          "WorkloadPipelineStack: GA Registry contexts must match nonprod/prod stages.",
        );
      }
      if (!/^[a-z]{2}(?:-[a-z0-9]+)+-\d$/.test(gatewayRegion)) {
        throw new Error(
          "WorkloadPipelineStack: gaRegistry.gatewayRegion is invalid.",
        );
      }
      const nonprodTools = nonprod.records.map(
        (record) => record.document.toolId,
      );
      const prodTools = prod.records.map((record) => record.document.toolId);
      if (JSON.stringify(nonprodTools) !== JSON.stringify(prodTools)) {
        throw new Error(
          "WorkloadPipelineStack: nonprod/prod GA Registry tool sets must match.",
        );
      }
    }

    if (props.policyEngine) {
      if (!props.gaRegistry) {
        throw new Error(
          "WorkloadPipelineStack: PolicyEngine requires GA Registry mode.",
        );
      }
      if (
        !(["LOG_ONLY", "ENFORCE"] as const).includes(props.policyEngine.mode)
      ) {
        throw new Error(
          "WorkloadPipelineStack: PolicyEngine mode must be LOG_ONLY or ENFORCE.",
        );
      }
      if (
        props.policyEngine.nonprodIamRoleArns.length === 0 ||
        props.policyEngine.prodIamRoleArns.length === 0
      ) {
        throw new Error(
          "WorkloadPipelineStack: PolicyEngine requires exact IAM role ARNs for both environments.",
        );
      }
    }

    if (props.enablePipelineRuntimeMemory && !props.gaRegistry) {
      throw new Error(
        "WorkloadPipelineStack: Runtime+Memory foundation requires GA Registry mode.",
      );
    }

    const resourceTags: PipelineResourceTags = {
      applicationId,
      agentId: props.agentId,
      tenantId: props.tenantId,
      costCentre: props.costCentre,
      environment: "pipeline",
    };
    applyPipelineResourceTags(this, resourceTags);
    const artifactBucket = createPipelineArtifactBucket(
      this,
      "WorkloadPipelineArtifacts",
      resourceTags,
    );

    const source = CodePipelineSource.connection(
      props.githubRepo,
      props.githubBranch ?? "main",
      {
        connectionArn: props.githubConnectionArn,
      },
    );

    const baseSynthOptions = {
      stage: props.synthStage ?? "pipeline",
      context: this.synthContext(props),
      expectedStackArtifactId: this.stackName,
      expectedStageAssemblyGlobs: props.gaRegistry
        ? [
            "cdk.out/assembly-*RegistryRoles",
            "cdk.out/assembly-*Nonprod",
            "cdk.out/assembly-*Prod",
          ]
        : ["cdk.out/assembly-*Nonprod", "cdk.out/assembly-*Prod"],
    };
    let synthStep: ShellStep;
    if (props.gaRegistry) {
      const resolver = registryResolverCommands(
        props.gaRegistry,
        applicationId,
        props.tenantId,
        props.agentId,
        props.costCentre,
      );
      const synthRole = new Role(this, "RegistrySynthRole", {
        roleName: registrySynthRoleName(props.tenantId, props.agentId),
        assumedBy: new ServicePrincipal("codebuild.amazonaws.com"),
        description:
          "Workload pipeline synth role; may assume only the two environment RegistryReader roles.",
      });
      synthRole.addToPolicy(
        new PolicyStatement({
          actions: ["sts:AssumeRole"],
          resources: [
            props.gaRegistry.nonprod.readerRoleArn,
            props.gaRegistry.prod.readerRoleArn,
          ],
        }),
      );
      synthStep = new CodeBuildStep("Synth", {
        input: source,
        role: synthRole,
        commands: stageAwareSynthCommands({
          ...baseSynthOptions,
          preSynthCommands: resolver.commands,
          contextFromEnvironment: resolver.contextFromEnvironment,
        }),
      });
    } else {
      synthStep = new ShellStep("Synth", {
        input: source,
        commands: stageAwareSynthCommands(baseSynthOptions),
      });
    }

    this.pipeline = new CodePipeline(this, "WorkloadPipeline", {
      artifactBucket,
      pipelineName: `agenticai-workload-${props.tenantId}-${props.agentId}`,
      pipelineType: PipelineType.V2,
      crossAccountKeys: true,
      enableKeyRotation: true,
      synth: synthStep,
      publishAssetsInParallel: false,
      // Native ARM64 Docker image publishing needs an ARM build image. CDK
      // enables privileged mode only on the DockerAssets project; FileAssets
      // stays non-privileged. Only configure the image here so the R2
      // Gateway-only graph remains unchanged when disabled.
      ...(props.enablePipelineRuntimeMemory
        ? {
            assetPublishingCodeBuildDefaults: {
              buildEnvironment: {
                buildImage: LinuxArmBuildImage.fromCodeBuildImageId(
                  "aws/codebuild/amazonlinux-aarch64-standard:4.0",
                ),
              },
            },
          }
        : {}),
    });

    if (props.gaRegistry) {
      const rolesStage = new WorkstreamRegistryRolesStage(
        this,
        "RegistryRoles",
        {
          tenantId: props.tenantId,
          agentId: props.agentId,
          applicationId,
          costCentre: props.costCentre,
          gatewayRegion: props.gaRegistry.gatewayRegion,
          workloadNonprodAccountId: props.workloadNonprodEnv.account,
          workloadProdAccountId: props.workloadProdEnv.account,
          nonprodContext: props.gaRegistry.nonprod,
          prodContext: props.gaRegistry.prod,
          enablePipelineRuntimeMemory: props.enablePipelineRuntimeMemory,
        },
      );
      const gatewayPermissionReady = new ManualApprovalStep(
        "GatewayPermissionReady",
        {
          comment: props.enablePipelineRuntimeMemory
            ? "Stable Gateway and Runtime roles exist. Continue only after Platform tool permissions are exact; the pipeline then enforces the AgentCore propagation window."
            : "Stable Workstream roles exist. Continue only after the Platform pipeline grants both exact Gateway service-role ARNs on every tool alias.",
        },
      );
      const postSteps: Step[] = [gatewayPermissionReady];
      if (props.enablePipelineRuntimeMemory) {
        const runtimeRolePropagation = new CodeBuildStep(
          "RuntimeRolePropagation",
          {
            commands: [
              'echo "Waiting six minutes for the stable Runtime role to propagate to AgentCore"',
              "sleep 360",
            ],
            buildEnvironment: { buildImage: LinuxBuildImage.STANDARD_7_0 },
            timeout: Duration.minutes(10),
          },
        );
        runtimeRolePropagation.addStepDependency(gatewayPermissionReady);
        postSteps.push(runtimeRolePropagation);
      }
      this.pipeline.addStage(rolesStage, { post: postSteps });
    }

    // Non-prod stage.
    const nonprodStage = new WorkloadDeploymentStage(this, "Nonprod", {
      env: props.workloadNonprodEnv,
      envName: "nonprod",
      tenantId: props.tenantId,
      agentId: props.agentId,
      applicationId,
      costCentre: props.costCentre,
      availabilityZones: props.workloadNonprodAvailabilityZones,
      gaRegistryContext: props.gaRegistry?.nonprod,
      gatewayRegion: props.gaRegistry?.gatewayRegion,
      policyEngineMode: props.policyEngine?.mode,
      policyEngineIamRoleArns: props.policyEngine?.nonprodIamRoleArns,
      enablePipelineRuntimeMemory: props.enablePipelineRuntimeMemory,
      auditOamSinkArn: props.auditOamSinkArn,
      notificationEmail: props.notificationEmail,
    });
    this.pipeline.addStage(nonprodStage);

    // Evaluation gate — CodeBuild step running the regression suite against
    // the just-deployed non-prod app. Fails if any threshold is breached.
    const evalStep = new CodeBuildStep("EvaluationGate", {
      commands: [
        "set -eu",
        'echo "Evaluation gate thresholds:"',
        `echo "  regression_pass_rate_min_pct    = ${props.evalRegressionPassRate ?? 95}"`,
        `echo "  guardrail_violation_rate_max_pct = ${props.evalGuardrailViolationRate ?? 1}"`,
        `echo "  quality_score_min_pct           = ${props.evalQualityScoreMin ?? 85}"`,
        `echo "  tool_success_rate_min_pct       = ${props.evalToolSuccessRate ?? 98}"`,
        `echo "  first_token_p99_max_ms          = ${props.evalFirstTokenP99Ms ?? 1500}"`,
        `echo "  refusal_rate_min_pct            = ${props.evalRefusalRateMin ?? 99}"`,
        `echo "  cost_per_prompt_max_usd         = ${props.evalCostPerPromptMaxUsd ?? 0.05}"`,
        // A missing harness is a failed gate, not a skipped one.
        'if [ ! -f scripts/evaluation_gate.py ]; then echo "ERROR: scripts/evaluation_gate.py is missing; evaluation gate cannot pass"; exit 1; fi',
        // Invoke the eval harness (ships under blueprints/*/eval/). The harness
        // reads the thresholds above, invokes the deployed agent against the
        // regression corpus, and exits non-zero if any metric fails.
        "python3 scripts/evaluation_gate.py",
      ],
      partialBuildSpec: BuildSpec.fromObject({
        version: "0.2",
        env: {
          variables: {
            EVAL_REGRESSION_PASS_MIN_PCT: String(
              props.evalRegressionPassRate ?? 95,
            ),
            EVAL_GUARDRAIL_VIOLATION_MAX_PCT: String(
              props.evalGuardrailViolationRate ?? 1,
            ),
            EVAL_QUALITY_MIN_PCT: String(props.evalQualityScoreMin ?? 85),
            EVAL_TOOL_SUCCESS_MIN_PCT: String(props.evalToolSuccessRate ?? 98),
            EVAL_FIRST_TOKEN_P99_MAX_MS: String(
              props.evalFirstTokenP99Ms ?? 1500,
            ),
            EVAL_REFUSAL_RATE_MIN_PCT: String(props.evalRefusalRateMin ?? 99),
            EVAL_COST_PER_PROMPT_MAX_USD: String(
              props.evalCostPerPromptMaxUsd ?? 0.05,
            ),
            EVAL_TENANT: props.tenantId,
            EVAL_AGENT: props.agentId,
            EVAL_ENV: "nonprod",
          },
        },
      }),
      buildEnvironment: {
        buildImage: LinuxBuildImage.STANDARD_7_0,
      },
      timeout: Duration.minutes(30),
    });

    // Z7-B: Canary stage. Shifts a small traffic slice to the new agent
    // version, then soaks while watching the OnlineEval Regressed composite
    // alarm. Both steps are fail-closed: an unimplemented traffic shift, a
    // missing alarm, or an alarm state that is anything other than OK at the
    // end of the soak halts the pipeline before Prod.
    const canaryPercent = props.canaryPercent ?? 5;
    const canarySoakMinutes = props.canarySoakMinutes ?? 30;
    const onlineEvalAlarmName = `agenticai-online-eval-nonprod-${props.tenantId}-${props.agentId}`;
    const describeAlarmState =
      `aws cloudwatch describe-alarms --alarm-names ${onlineEvalAlarmName}` +
      ` --alarm-types CompositeAlarm --query 'CompositeAlarms[0].StateValue' --output text`;

    const canaryDeployStep = new CodeBuildStep("CanaryDeploy", {
      commands: this.canaryDeployCommands(props, canaryPercent),
      partialBuildSpec: BuildSpec.fromObject({
        version: "0.2",
        env: { variables: { CANARY_PERCENT: String(canaryPercent) } },
      }),
      buildEnvironment: { buildImage: LinuxBuildImage.STANDARD_7_0 },
      timeout: Duration.minutes(15),
    });

    const canarySoakStep = new CodeBuildStep("CanarySoak", {
      commands: [
        "set -eu",
        `echo "Soaking canary ${canarySoakMinutes} minutes against composite alarm ${onlineEvalAlarmName}"`,
        // The alarm must exist before the soak starts. A missing alarm means
        // there is no regression signal at all, which is a failed soak.
        `ALARM_COUNT=$(aws cloudwatch describe-alarms --alarm-names ${onlineEvalAlarmName} --alarm-types CompositeAlarm --query 'length(CompositeAlarms)' --output text)`,
        `if [ "$ALARM_COUNT" != "1" ]; then echo "ERROR: composite alarm ${onlineEvalAlarmName} not found (describe-alarms returned '$ALARM_COUNT'); soak FAILED"; exit 1; fi`,
        // Poll every 30s. `set -e` means a failed describe-alarms call aborts
        // the step rather than being read as a healthy state.
        `SOAK_POLLS=$((${canarySoakMinutes} * 2))`,
        "POLL=0",
        'while [ "$POLL" -lt "$SOAK_POLLS" ]; do',
        `  STATE=$(${describeAlarmState})`,
        '  case "$STATE" in',
        '    ALARM) echo "ERROR: canary regressed (alarm state ALARM); soak FAILED"; exit 1 ;;',
        "    OK|INSUFFICIENT_DATA) ;;",
        "    *) echo \"ERROR: unusable alarm state '$STATE'; soak FAILED\"; exit 1 ;;",
        "  esac",
        "  POLL=$((POLL + 1))",
        "  sleep 30",
        "done",
        // Ending on INSUFFICIENT_DATA means the soak gathered no evidence.
        // Absence of evidence is not evidence of health.
        `FINAL_STATE=$(${describeAlarmState})`,
        `if [ "$FINAL_STATE" != "OK" ]; then echo "ERROR: soak ended with alarm state '$FINAL_STATE'; OK required; soak FAILED"; exit 1; fi`,
        'echo "Canary soak passed"',
      ],
      buildEnvironment: { buildImage: LinuxBuildImage.STANDARD_7_0 },
      timeout: Duration.minutes(canarySoakMinutes + 5),
    });

    // Prod stage gated by evaluation + manual approval + canary + soak.
    const prodStage = new WorkloadDeploymentStage(this, "Prod", {
      env: props.workloadProdEnv,
      envName: "prod",
      tenantId: props.tenantId,
      agentId: props.agentId,
      applicationId,
      costCentre: props.costCentre,
      availabilityZones: props.workloadProdAvailabilityZones,
      gaRegistryContext: props.gaRegistry?.prod,
      gatewayRegion: props.gaRegistry?.gatewayRegion,
      policyEngineMode: props.policyEngine?.mode,
      policyEngineIamRoleArns: props.policyEngine?.prodIamRoleArns,
      enablePipelineRuntimeMemory: props.enablePipelineRuntimeMemory,
      auditOamSinkArn: props.auditOamSinkArn,
      notificationEmail: props.notificationEmail,
    });

    if (props.gaRegistry) {
      this.pipeline.addStage(prodStage, {
        pre: [
          new ManualApprovalStep("ProdGatewayApproval", {
            comment: props.policyEngine
              ? `Approve only after nonproduction Registry/MCP denial twins and PolicyEngine ${props.policyEngine.mode} behavior match the retained Lambda wrapper. Production must not lead nonproduction mode evidence.`
              : props.enablePipelineRuntimeMemory
                ? "Approve only after nonproduction Gateway targets pass live Registry, tools/list, tools/call, and denial twins, and the native Runtime+Memory foundation reached Runtime READY / Memory ACTIVE. The Runtime runs the inert proven agent; generated-agent LiteLLMModel/MCPClient integration is NOT yet wired."
                : "Approve only after nonproduction Gateway targets pass live Registry, tools/list, tools/call, and denial twins. No agent runtime/canary exists in the R2 Gateway-only slice.",
          }),
        ],
      });
    } else {
      const approvalStep = new ManualApprovalStep("ProdApproval", {
        comment:
          "Evaluation gate passed. Approving starts the canary deploy and soak; Prod deploys only if the soak passes.",
      });

      // Ordering is declared, not implied. CDK Pipelines runs steps with no
      // declared dependency concurrently, so without these edges the evaluation
      // gate, approval and canary would share a RunOrder and Prod could be
      // reached without any of them having produced a verdict.
      approvalStep.addStepDependency(evalStep);
      canaryDeployStep.addStepDependency(approvalStep);
      canarySoakStep.addStepDependency(canaryDeployStep);

      this.pipeline.addStage(prodStage, {
        pre: [evalStep, approvalStep, canaryDeployStep, canarySoakStep],
      });
    }

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
            "SEC-011: Pipeline roles require wildcards for CDK bootstrap.",
        },
        {
          id: "AwsSolutions-S1",
          reason:
            "SEC-001: Pipeline artifacts expire after 30 days; CodePipeline execution history and CloudTrail provide the audit trail without a recursive access-log bucket.",
        },
        {
          id: "AwsSolutions-L1",
          reason: "SEC-006: CDK Pipelines-generated Lambdas track aws-cdk-lib.",
        },
        {
          id: "NIST.800.53.R5-CodeBuildProjectEnvVarAwsCred",
          reason:
            "SEC-018: CodeBuild reads bootstrap role creds via STS, not env vars.",
        },
        {
          id: "NIST.800.53.R5-CodeBuildProjectKMSEncryptedArtifacts",
          reason:
            "SEC-017: The explicit pipeline artifact bucket uses a customer-managed rotating KMS key shared with cross-account stages.",
        },
        {
          id: "NIST.800.53.R5-CodeBuildProjectPrivilegedModeDisabled",
          reason:
            "SEC-019: Synth/build run in standard non-privileged containers.",
        },
        {
          id: "NIST.800.53.R5-CodeBuildProjectSourceRepoUrl",
          reason:
            "SEC-020: Source via CodeStar Connections (GitHub V2 managed path).",
        },
        {
          id: "NIST.800.53.R5-IAMNoInlinePolicy",
          reason: "SEC-005: CDK Pipelines auto-generates inline policies.",
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
            "SEC-007: Self-mutate and artifact-cleanup Lambdas are provisioning-time only.",
        },
        {
          id: "NIST.800.53.R5-LambdaDLQ",
          reason:
            "SEC-008: CFN custom-resource Lambdas surface failures via stack events.",
        },
        {
          id: "NIST.800.53.R5-LambdaInsideVPC",
          reason:
            "SEC-009: Pipeline Lambdas call AWS control planes via managed endpoints.",
        },
        {
          id: "NIST.800.53.R5-S3BucketVersioningEnabled",
          reason:
            "SEC-022: Pipeline artifacts are immutable per execution, expire after 30 days, and are automatically removed with the stack.",
        },
      ],
      true,
    );
  }

  /**
   * Context for the pipeline's own `cdk synth`: everything derivable from this
   * stack's props, overlaid with any explicit `synthContext` entries.
   */
  private synthContext(
    props: WorkloadPipelineStackProps,
  ): Record<string, string> {
    const derived: Record<string, string> = {
      "agenticai/githubRepo": props.githubRepo,
      "agenticai/githubConnectionArn": props.githubConnectionArn,
      "agenticai/pipelineSelection": "workload",
      "agenticai/tenantId": props.tenantId,
      "agenticai/agentId": props.agentId,
      "agenticai/costCentre": props.costCentre,
      "agenticai/workloadNonprodAccountId": props.workloadNonprodEnv.account,
      "agenticai/workloadProdAccountId": props.workloadProdEnv.account,
      "agenticai/workloadNonprodAvailabilityZones": JSON.stringify(
        props.workloadNonprodAvailabilityZones,
      ),
      "agenticai/workloadProdAvailabilityZones": JSON.stringify(
        props.workloadProdAvailabilityZones,
      ),
    };
    if (props.githubBranch) {
      derived["agenticai/githubBranch"] = props.githubBranch;
    }
    if (props.auditOamSinkArn) {
      derived["agenticai/auditOamSinkArn"] = props.auditOamSinkArn;
    }
    if (props.notificationEmail) {
      derived["agenticai/notificationEmail"] = props.notificationEmail;
    }
    derived["agenticai/applicationId"] = props.applicationId ?? props.tenantId;
    if (props.gaRegistry) {
      derived["agenticai/enableGaRegistryConsumer"] = "true";
      derived["agenticai/platformNonprodAccountId"] =
        props.gaRegistry.nonprod.platformAccountId;
      derived["agenticai/platformProdAccountId"] =
        props.gaRegistry.prod.platformAccountId;
      derived["agenticai/gaRegistryExpectedToolIds"] = JSON.stringify(
        props.gaRegistry.nonprod.records.map(
          (record) => record.document.toolId,
        ),
      );
      derived["agenticai/workstreamGatewayRegion"] =
        props.gaRegistry.gatewayRegion;
    }
    if (props.policyEngine) {
      derived["agenticai/gatewayPolicyEngineMode"] = props.policyEngine.mode;
      derived["agenticai/gatewayPolicyEngineNonprodIamRoleArns"] =
        JSON.stringify(props.policyEngine.nonprodIamRoleArns);
      derived["agenticai/gatewayPolicyEngineProdIamRoleArns"] = JSON.stringify(
        props.policyEngine.prodIamRoleArns,
      );
    }
    if (props.enablePipelineRuntimeMemory) {
      derived["agenticai/enablePipelineRuntimeMemory"] = "true";
    }
    return {
      ...derived,
      ...(props.synthContext ?? {}),
      "agenticai/pipelineSelection": "workload",
    };
  }

  /**
   * Canary traffic-shift commands.
   *
   * With no `canaryDeployCommands` supplied there is nothing that can shift
   * AgentCore traffic, so the step states that plainly and exits 1. The
   * previous implementation called `aws lambda update-alias … || true` against
   * a function this blueprint never creates, which always reported success and
   * let Prod promotion proceed on no evidence.
   */
  private canaryDeployCommands(
    props: WorkloadPipelineStackProps,
    canaryPercent: number,
  ): string[] {
    if (props.canaryDeployCommands && props.canaryDeployCommands.length > 0) {
      return ["set -eu", ...props.canaryDeployCommands];
    }
    return [
      "set -eu",
      `echo "CanaryDeploy: NOT IMPLEMENTED — no AgentCore traffic-shifting call is wired for ${canaryPercent}% canary traffic."`,
      'echo "This step fails closed on purpose: promoting to Prod without a real canary would be an unverified claim."',
      'echo "Supply WorkloadPipelineStackProps.canaryDeployCommands with a real AgentCore traffic-shift call to enable it."',
      "exit 1",
    ];
  }
}
