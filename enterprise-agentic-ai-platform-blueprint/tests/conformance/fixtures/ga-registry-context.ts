/**
 * Shared GA Registry consumer-context fixture for conformance suites.
 *
 * Builds the pipeline-resolved `GaRegistryConsumerContext` a Workstream
 * Gateway stack consumes, one APPROVED governance record per tool, derived
 * from the in-process catalogue so per-test catalogue overrides (for example
 * `allowedGroups`) flow into the records. Test data only.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import type { GaRegistryConsumerContext } from "@agenticai/agent-registry";
import {
  PLATFORM_TOOL_CATALOGUE,
  type ToolId,
} from "@agenticai/platform-tool-catalogue";

export const FIXTURE_PLATFORM_ACCOUNT_ID = "222222222222";
export const GA_REGISTRY_ID = "ABCDEFGHIJKLMNOP";
export const GA_REGISTRY_ARN = `arn:aws:agent-registry:us-west-2:${FIXTURE_PLATFORM_ACCOUNT_ID}:registry/${GA_REGISTRY_ID}`;
export const GA_RECORD_IDS: Record<string, string> = {
  "tool-echo": "ABCDEFGHIJKL",
  "tool-ping": "MNOPQRSTUVWX",
};

export function gaRegistryContext(
  toolIds: readonly ToolId[] = ["tool-echo", "tool-ping"],
): GaRegistryConsumerContext {
  const platformAccountId = FIXTURE_PLATFORM_ACCOUNT_ID;
  return {
    schemaVersion: "agenticai.ga-registry-consumer-context/1.0",
    environment: "nonprod",
    region: "us-west-2",
    platformAccountId,
    sourceRevision: "a".repeat(40),
    registryId: GA_REGISTRY_ID,
    registryArn: GA_REGISTRY_ARN,
    readerRoleArn: `arn:aws:iam::${platformAccountId}:role/AgenticAI-RegistryReader-nonprod`,
    readerExternalId: `agenticai-registry-v1-nonprod-${platformAccountId}`,
    records: [...toolIds].sort().map((toolId, index) => {
      const source = PLATFORM_TOOL_CATALOGUE[toolId];
      const recordId = GA_RECORD_IDS[toolId];
      return {
        recordId,
        recordArn: `${GA_REGISTRY_ARN}/record/${recordId}`,
        descriptorSha256: String(index + 1).repeat(64),
        document: {
          schemaVersion: "agenticai.tool-governance/1.0",
          catalogueVersion: "2",
          toolId,
          description: source.description,
          desiredApprovalStatus: "approved",
          target: {
            type: source.toolType ?? "lambda",
            arn:
              `arn:aws:lambda:us-west-2:${platformAccountId}:function:` +
              `agenticai-platform-nonprod-${toolId}:PROD`,
          },
          mcp: {
            toolName: toolId,
            description: source.description,
            inputSchema: source.inputSchema ?? { type: "object" },
          },
          authorization: {
            defaultDecision: "DENY",
            cedarPolicy: source.cedarPolicy,
            allowedSubjects: [],
            allowedGroups: source.allowedGroups ?? [],
            combination:
              source.allowedGroups && source.allowedGroups.length > 0
                ? "GROUP_ONLY"
                : "AUTHENTICATED",
          },
          ownership: {
            ownerTeam: source.ownerTeam,
            costCentre: source.costCentre,
          },
        },
      };
    }),
  };
}
