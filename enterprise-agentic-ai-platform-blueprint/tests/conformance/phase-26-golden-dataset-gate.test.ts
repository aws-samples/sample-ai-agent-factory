/**
 * Phase 26 conformance — golden-dataset promotion gate in the workload pipeline
 * (docs/CLOUDWATCH_OMNI_INTEGRATION.md section 3.1).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";

import type { GaRegistryConsumerContext } from "@agenticai/agent-registry";
import {
  WorkloadPipelineStack,
  type WorkloadPipelineStackProps,
} from "../../pipelines/workload-pipeline-stack";

const PLATFORM_ACCOUNT = "111111111111";
const NONPROD_ACCOUNT = "444444444444";
const PROD_ACCOUNT = "555555555555";
const HUB_ACCOUNT = "333333333333";
const REGION = "us-west-2";

// Same fixture as phase-23: the pipeline validates the GA Registry contexts at synth.
function gaContext(environment: "nonprod" | "prod"): GaRegistryConsumerContext {
  const registryId = environment === "nonprod" ? "ABCDEFGHIJKLMNOP" : "QRSTUVWXYZABCDEF";
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
          mcp: { toolName: "tool-echo", description: "Echo tool", inputSchema: { type: "object" } },
          authorization: {
            defaultDecision: "DENY",
            cedarPolicy: 'permit(principal, action, resource == Tool::"tool-echo");',
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

function synth(overrides: Partial<WorkloadPipelineStackProps> = {}, generated = true): Template {
  const app = new App();
  const stack = new WorkloadPipelineStack(app, "Pipeline", {
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
    gaRegistry: { nonprod: gaContext("nonprod"), prod: gaContext("prod"), gatewayRegion: REGION },
    enablePipelineRuntimeMemory: generated,
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
    ...overrides,
  });
  return Template.fromStack(stack);
}

const GOLDEN = {
  datasetId: "golden_demo_primary-AbCdEfGhIj",
  datasetVersion: "3",
  hubAccountId: HUB_ACCOUNT,
};

type Project = { Properties: { Name?: string; Environment: { EnvironmentVariables: Array<{ Name: string; Value: string }> }; Source: { BuildSpec: string } } };

function projects(template: Template): Project[] {
  return Object.values(template.findResources("AWS::CodeBuild::Project")) as Project[];
}

/** The build commands of a CodeBuild project, as the shell will see them (the BuildSpec is a JSON string). */
function commandsOf(project: Project): string {
  const raw = project.Properties.Source.BuildSpec;
  const text = typeof raw === "string" ? raw : JSON.stringify(raw);
  try {
    const spec = JSON.parse(text) as { phases?: { build?: { commands?: string[] } } };
    return (spec.phases?.build?.commands ?? []).join("\n");
  } catch {
    return text;
  }
}

function goldenProject(template: Template): Project | undefined {
  return projects(template).find((p) => commandsOf(p).includes("golden_dataset_gate.py"));
}

/** The `env.variables` of the project's BuildSpec (partialBuildSpec lands there, not in the project environment). */
function envOf(project: Project): Record<string, string> {
  const raw = project.Properties.Source.BuildSpec;
  const spec = JSON.parse(typeof raw === "string" ? raw : JSON.stringify(raw)) as { env?: { variables?: Record<string, string> } };
  return spec.env?.variables ?? {};
}

/** RunOrder of a Prod-stage action by name, searching every stage so a renamed stage fails loudly rather than silently. */
function runOrder(template: Template, actionName: string): number {
  const pipelineRes = Object.values(template.findResources("AWS::CodePipeline::Pipeline"))[0] as {
    Properties: { Stages: Array<{ Name: string; Actions: Array<{ Name: string; RunOrder: number }> }> };
  };
  const actions = pipelineRes.Properties.Stages.flatMap((s) => s.Actions.map((a) => ({ ...a, stage: s.Name })));
  const match = actions.find((a) => a.Name === actionName);
  if (!match) {
    throw new Error(`action ${actionName} not found; actions: ${actions.map((a) => `${a.stage}/${a.Name}`).join(", ")}`);
  }
  return match.RunOrder;
}

describe("Phase 26 — golden-dataset gate is absent unless configured", () => {
  it("emits no golden gate project and no hub runner assume grant by default", () => {
    const template = synth();
    expect(goldenProject(template)).toBeUndefined();
    expect(JSON.stringify(template.toJSON())).not.toContain("AgenticAIGoldenGateRunner");
  });
});

describe("Phase 26 — golden-dataset gate wiring", () => {
  const template = synth({ goldenDataset: GOLDEN });
  const project = goldenProject(template)!;

  it("adds one CodeBuild project that exports, replays and gates, each under its own role", () => {
    expect(project).toBeDefined();
    const spec = commandsOf(project);
    const exportIdx = spec.indexOf('--assume-role-arn "$GOLDEN_RUNNER_ROLE_ARN" export');
    const replayIdx = spec.indexOf('--assume-role-arn "$EVAL_INVOKER_ROLE_ARN" replay --scenarios golden-scenarios.json --runtime-arn "$EVAL_RUNTIME_ARN"');
    const gateIdx = spec.indexOf('--assume-role-arn "$GOLDEN_RUNNER_ROLE_ARN" gate');
    expect(exportIdx).toBeGreaterThan(-1);
    expect(replayIdx).toBeGreaterThan(exportIdx);
    expect(gateIdx).toBeGreaterThan(replayIdx);
    // A missing harness is a failed gate, not a skipped one.
    expect(spec).toContain("golden_dataset_gate.py is missing; golden-dataset gate cannot pass");
    expect(spec).toContain("set -eu");
  });

  it("reads the candidate's telemetry from the hub's centralized copies, never from the cell", () => {
    const spec = commandsOf(project);
    expect(spec).toContain('HUB_SPANS="/centralized/${EVAL_NONPROD_ACCOUNT}/${EVAL_REGION}aws/spans"');
    expect(spec).toContain('HUB_RUNTIME_LOGS="/centralized/${EVAL_NONPROD_ACCOUNT}/${EVAL_REGION}/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT"');
    expect(spec).toContain('--service-name "${RUNTIME_ID}.DEFAULT"');
    expect(spec).toContain('--results-log-group "/agenticai/golden-gate/${EVAL_TENANT}-${EVAL_AGENT}"');
    expect(spec).toContain("--metrics-namespace agenticai/golden-gate");
    expect(spec).not.toContain('--log-group "aws/spans"');
  });

  it("carries the dataset version, hub Region, runner role and the SAME EVAL_* thresholds as the evaluation gate", () => {
    const env = envOf(project);
    expect(env).toMatchObject({
      GOLDEN_DATASET_ID: GOLDEN.datasetId,
      GOLDEN_DATASET_VERSION: "3",
      GOLDEN_HUB_REGION: REGION,
      GOLDEN_RUNNER_ROLE_ARN: `arn:aws:iam::${HUB_ACCOUNT}:role/AgenticAIGoldenGateRunner-${REGION}`,
      GOLDEN_TRAJECTORY_EVALUATOR: "Builtin.TrajectoryAnyOrderMatch",
      EVAL_NONPROD_ACCOUNT: NONPROD_ACCOUNT,
      EVAL_REGION: REGION,
      EVAL_TENANT: "demo",
      EVAL_AGENT: "primary",
    });
    const evalProject = projects(template).find((p) => commandsOf(p).includes("python3 scripts/evaluation_gate.py"))!;
    const evalEnv = envOf(evalProject);
    for (const key of [
      "EVAL_REGRESSION_PASS_MIN_PCT", "EVAL_GUARDRAIL_VIOLATION_MAX_PCT", "EVAL_QUALITY_MIN_PCT", "EVAL_TOOL_SUCCESS_MIN_PCT",
      "EVAL_FIRST_TOKEN_P99_MAX_MS", "EVAL_REFUSAL_RATE_MIN_PCT", "EVAL_COST_PER_PROMPT_MAX_USD", "EVAL_INVOKER_ROLE_ARN",
    ]) {
      expect(env[key]).toBe(evalEnv[key]);
    }
    expect(env.EVAL_QUALITY_MIN_PCT).toBe("85");
  });

  it("lets the pipeline evaluation role assume exactly the invoker and the hub runner", () => {
    const policies = Object.values(template.findResources("AWS::IAM::Policy")) as Array<{ Properties: { PolicyDocument: { Statement: Array<{ Action: unknown; Resource: unknown }> } } }>;
    const assume = policies.flatMap((p) => p.Properties.PolicyDocument.Statement).find((s) => JSON.stringify(s.Action) === '"sts:AssumeRole"');
    expect(assume).toBeDefined();
    const resources = JSON.stringify(assume!.Resource);
    expect(resources).toContain(`arn:aws:iam::${HUB_ACCOUNT}:role/AgenticAIGoldenGateRunner-${REGION}`);
    expect(resources).toContain(`arn:aws:iam::${NONPROD_ACCOUNT}:role/`);
    expect(resources).not.toContain('"*"');
  });

  it("orders the gates: evaluation gate -> golden gate -> approval -> canary, in the pipeline definition", () => {
    // GA Registry pipelines gate Prod behind ProdGatewayApproval; the golden gate sits between the
    // evaluation gate and that approval, and every Prod deploy action runs after the approval.
    const order = (name: string) => runOrder(template, name);
    expect(order("GoldenDatasetGate")).toBeGreaterThan(order("EvaluationGate"));
    expect(order("ProdGatewayApproval")).toBeGreaterThan(order("GoldenDatasetGate"));
    expect(order("AgenticAI-demo-primary-prod-RuntimeMemory.Prepare")).toBeGreaterThan(order("ProdGatewayApproval"));
  });

  it("honours an explicit hub Region, runner role name and trajectory matcher", () => {
    const custom = synth({
      goldenDataset: { ...GOLDEN, hubRegion: "eu-west-1", runnerRoleName: "HubGoldenRunner", trajectoryEvaluator: "Builtin.TrajectoryExactOrderMatch" },
    });
    const env = envOf(goldenProject(custom)!);
    expect(env.GOLDEN_HUB_REGION).toBe("eu-west-1");
    expect(env.GOLDEN_RUNNER_ROLE_ARN).toBe(`arn:aws:iam::${HUB_ACCOUNT}:role/HubGoldenRunner`);
    expect(env.GOLDEN_TRAJECTORY_EVALUATOR).toBe("Builtin.TrajectoryExactOrderMatch");
  });
});

describe("Phase 26 — golden-dataset gate validation", () => {
  it("refuses the gate without the generated-agent evaluation", () => {
    expect(() => synth({ goldenDataset: GOLDEN }, false)).toThrow(/goldenDataset requires the generated-agent evaluation/);
  });

  it("refuses DRAFT and malformed inputs", () => {
    expect(() => synth({ goldenDataset: { ...GOLDEN, datasetVersion: "DRAFT" } })).toThrow(/DRAFT is mutable/);
    expect(() => synth({ goldenDataset: { ...GOLDEN, datasetId: "bad id!" } })).toThrow(/is not a dataset id/);
    expect(() => synth({ goldenDataset: { ...GOLDEN, hubAccountId: "123" } })).toThrow(/12-digit account id/);
  });
});
