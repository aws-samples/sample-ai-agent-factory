/**
 * Render the D03 Workstream RuntimeMemory stack template for one environment so
 * its IAM policies can be simulated (`simulate_idprov_policy.py`) or compared
 * against the deployed roles, without a full `cdk synth` of the pipeline.
 *
 * Mirrors the phase-23 conformance harness's `runtimeMemoryTemplate()` shape;
 * only the account and the placeholder Platform inputs differ, and none of
 * those affect the provider role's policy resources (which are derived from
 * partition/region/account and the deterministic names).
 *
 * Usage:
 *   npx ts-node scripts/render-runtime-memory-template.ts <out.json> <account> [nonprod|prod]
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { writeFileSync } from "node:fs";

import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";

import { D03WorkstreamRuntimeMemoryStack } from "../apps/workload-account/lib/d03-workstream-runtime-memory-stack";

const [outPath, account, envArg = "nonprod"] = process.argv.slice(2);
if (!outPath || !/^\d{12}$/.test(account ?? "")) {
  console.error(
    "usage: render-runtime-memory-template.ts <out.json> <12-digit account> [nonprod|prod]",
  );
  process.exit(2);
}
if (envArg !== "nonprod" && envArg !== "prod") {
  console.error("environment must be 'nonprod' or 'prod'");
  process.exit(2);
}
const envName: "nonprod" | "prod" = envArg;
const region = "us-west-2";
const platformAccount = "111111111111"; // placeholder: not part of any provider-role resource ARN

const app = new App();
const stack = new D03WorkstreamRuntimeMemoryStack(
  app,
  `RuntimeMemory-${envName}`,
  {
    env: { account, region },
    envName,
    applicationId: "demo",
    agentId: "primary",
    tenantId: "demo",
    costCentre: "engineering",
    runtimeExecutionRoleArnOverride: `arn:aws:iam::${account}:role/AgenticAI-D03-${envName}-demo-primary-runtime`,
    agentImageVariant: "generated-agent",
    generatedAgentRuntimeConfig: {
      mcpGatewayUrl: `https://gw-${envName}.gateway.bedrock-agentcore.${region}.amazonaws.com/mcp`,
      inferenceGatewayUrl: `https://inf-${envName}.gateway.bedrock-agentcore.${region}.amazonaws.com/mcp`,
      modelId: `agenticai-inference-${envName}-bedrock/openai.gpt-oss-120b`,
      guardrailId: `arn:aws:bedrock:${region}:${platformAccount}:guardrail/example`,
      subscribedTools: ["target-tool-echo___echo", "target-tool-ping___ping"],
      inferenceScope: `agenticai-inference-${envName}-api/invoke`,
      m2mSecretArn: `arn:aws:secretsmanager:${region}:${platformAccount}:secret:agenticai/inference-m2m/agenticai-inference-${envName}-abc`,
    },
  },
);

writeFileSync(
  outPath,
  JSON.stringify(Template.fromStack(stack).toJSON(), null, 2),
);
console.log(
  `rendered ${envName} RuntimeMemory template for ${account} -> ${outPath}`,
);
