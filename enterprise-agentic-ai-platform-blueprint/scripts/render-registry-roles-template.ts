/**
 * Render the nonprod RegistryRoles template WITH generated-agent grants to a
 * JSON file so the offline IAM simulator
 * (`scripts/live-agentcore-generated-agent-spike/simulate_runtime_identity_grant.py`)
 * can evaluate the exact Runtime execution role policy before a pipeline run.
 *
 *   npx ts-node scripts/render-registry-roles-template.ts <out.json> [account]
 *
 * Synth only -- no AWS calls, no deploy. The registry context is a minimal,
 * non-secret fixture; only the Runtime role's policy statements matter here.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { writeFileSync } from "node:fs";

import type { GaRegistryConsumerContext } from "@agenticai/agent-registry";

import { D03WorkstreamRegistryRolesStack } from "../apps/workload-account/lib/d03-workstream-registry-roles-stack";

const REGION = "us-west-2";
const PLATFORM_ACCOUNT = "111111111111";

function gaContext(environment: "nonprod" | "prod"): GaRegistryConsumerContext {
  const registryId = "ABCDEFGHIJKLMNOP";
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
        },
      },
    ],
  } as unknown as GaRegistryConsumerContext;
}

function main(): void {
  const [outPath, account = "444444444444"] = process.argv.slice(2);
  if (!outPath) {
    throw new Error(
      "usage: render-registry-roles-template.ts <out.json> [account]",
    );
  }
  const app = new App();
  const stack = new D03WorkstreamRegistryRolesStack(app, "Roles-render", {
    env: { account, region: REGION },
    envName: "nonprod",
    tenantId: "demo",
    agentId: "primary",
    applicationId: "demo",
    costCentre: "engineering",
    registryContext: gaContext("nonprod"),
    enablePipelineRuntimeMemory: true,
    generatedAgentGrants: {
      m2mSecretArn: `arn:aws:secretsmanager:${REGION}:${PLATFORM_ACCOUNT}:secret:agenticai/inference-m2m/agenticai-inference-nonprod-abc`,
      credentialProviderName: "AgenticAI_D03_nonprod_demo_primary_inference",
      workloadIdentityName: "AgenticAI_D03_nonprod_demo_primary",
    },
  });
  writeFileSync(outPath, JSON.stringify(Template.fromStack(stack).toJSON()));
  process.stdout.write(`rendered ${outPath}\n`);
}

main();
