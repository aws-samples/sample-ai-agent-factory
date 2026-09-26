/*
 * Unit guards for the GA Registry consumer context contract.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  GA_REGISTRY_CONSUMER_CONTEXT_SCHEMA,
  parseGaRegistryConsumerContext,
} from "./ga-registry-consumer-context";

const PLATFORM = "222222222222";
const REGION = "us-west-2";
const REGISTRY_ID = "ABCDEFGHIJKLMNOP";
const REGISTRY_ARN = `arn:aws:agent-registry:${REGION}:${PLATFORM}:registry/${REGISTRY_ID}`;
const EXPECTATION = {
  environment: "nonprod" as const,
  platformAccountId: PLATFORM,
  expectedToolIds: ["tool-echo", "tool-ping"],
};

function governance(toolId: string) {
  return {
    schemaVersion: "agenticai.tool-governance/1.0",
    catalogueVersion: "1",
    toolId,
    description: `${toolId} description`,
    desiredApprovalStatus: "approved",
    target: {
      type: "lambda",
      arn: `arn:aws:lambda:${REGION}:${PLATFORM}:function:${toolId}:PROD`,
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
  };
}

function validContext(): any {
  const records = [
    ["tool-echo", "ABCDEFGHIJKL"],
    ["tool-ping", "MNOPQRSTUVWX"],
  ].map(([toolId, recordId], index) => ({
    recordId,
    recordArn: `${REGISTRY_ARN}/record/${recordId}`,
    descriptorSha256: String(index + 1).repeat(64),
    document: governance(toolId),
  }));
  return {
    schemaVersion: GA_REGISTRY_CONSUMER_CONTEXT_SCHEMA,
    environment: "nonprod",
    region: REGION,
    platformAccountId: PLATFORM,
    sourceRevision: "a".repeat(40),
    registryId: REGISTRY_ID,
    registryArn: REGISTRY_ARN,
    readerRoleArn: `arn:aws:iam::${PLATFORM}:role/AgenticAI-RegistryReader-nonprod`,
    readerExternalId: `agenticai-registry-v1-nonprod-${PLATFORM}`,
    records,
  };
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

describe("parseGaRegistryConsumerContext", () => {
  it("accepts a sorted, exact, template-bound context", () => {
    const parsed = parseGaRegistryConsumerContext(validContext(), EXPECTATION);
    expect(parsed.records.map((record) => record.document.toolId)).toEqual([
      "tool-echo",
      "tool-ping",
    ]);
    expect(parsed.registryArn).toBe(REGISTRY_ARN);
  });

  it.each([
    ["schemaVersion", "wrong"],
    ["environment", "prod"],
    ["platformAccountId", "333333333333"],
    ["sourceRevision", "short"],
    ["registryId", "short"],
    ["registryArn", `${REGISTRY_ARN}-wrong`],
    ["readerRoleArn", `arn:aws:iam::${PLATFORM}:role/Admin`],
    ["readerExternalId", "wrong"],
  ])("rejects a tampered top-level %s", (key, value) => {
    const context = validContext();
    context[key] = value;
    expect(() =>
      parseGaRegistryConsumerContext(context, EXPECTATION),
    ).toThrow();
  });

  it("rejects extra top-level keys", () => {
    const context = validContext();
    context.unreviewed = true;
    expect(() => parseGaRegistryConsumerContext(context, EXPECTATION)).toThrow(
      /keys/,
    );
  });

  it("rejects missing, reordered, and duplicate records", () => {
    const missing = validContext();
    missing.records.pop();
    expect(() => parseGaRegistryConsumerContext(missing, EXPECTATION)).toThrow(
      /exactly/,
    );

    const reordered = validContext();
    reordered.records.reverse();
    expect(() =>
      parseGaRegistryConsumerContext(reordered, EXPECTATION),
    ).toThrow(/sorted/);

    const duplicate = validContext();
    duplicate.records[1].recordId = duplicate.records[0].recordId;
    duplicate.records[1].recordArn = duplicate.records[0].recordArn;
    expect(() =>
      parseGaRegistryConsumerContext(duplicate, EXPECTATION),
    ).toThrow(/duplicate/);
  });

  it.each([
    ["desiredApprovalStatus", "deprecated"],
    ["schemaVersion", "agenticai.tool-governance/2.0"],
  ])("rejects a tampered governance %s", (key, value) => {
    const context = validContext();
    context.records[0].document[key] = value;
    expect(() =>
      parseGaRegistryConsumerContext(context, EXPECTATION),
    ).toThrow();
  });

  it("rejects target, MCP, authorization, and ownership drift", () => {
    const mutations: Array<(context: any) => void> = [
      (context) => {
        context.records[0].document.target.arn = `arn:aws:lambda:eu-west-1:${PLATFORM}:function:rogue:PROD`;
      },
      (context) => {
        context.records[0].document.target.arn =
          "arn:aws:lambda:us-east-1:333333333333:function:rogue:PROD";
      },
      (context) => {
        context.records[0].document.mcp.toolName = "tool-other";
      },
      (context) => {
        context.records[0].document.authorization.defaultDecision = "ALLOW";
      },
      (context) => {
        context.records[0].document.authorization.allowedSubjects = [
          "developer-a",
        ];
      },
      (context) => {
        context.records[0].document.authorization.combination = "GROUP_ONLY";
      },
      (context) => {
        context.records[0].document.ownership.ownerTeam = "";
      },
    ];
    for (const mutate of mutations) {
      const context = clone(validContext());
      mutate(context);
      expect(() =>
        parseGaRegistryConsumerContext(context, EXPECTATION),
      ).toThrow();
    }
  });

  it("accepts exact group-bound semantics and rejects duplicates", () => {
    const context = validContext();
    context.records[0].document.authorization.allowedGroups = ["developers"];
    context.records[0].document.authorization.combination = "GROUP_ONLY";
    expect(() =>
      parseGaRegistryConsumerContext(context, EXPECTATION),
    ).not.toThrow();

    context.records[0].document.authorization.allowedGroups = [
      "developers",
      "developers",
    ];
    expect(() => parseGaRegistryConsumerContext(context, EXPECTATION)).toThrow(
      /duplicates/,
    );
  });
});
