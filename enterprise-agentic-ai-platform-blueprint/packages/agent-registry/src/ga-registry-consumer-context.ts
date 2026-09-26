/*
 * Strict contract shared by the Platform-side Workload pipeline synth and the
 * Workstream-side GA Registry consumer.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import type { GaToolGovernanceDocument } from "./ga-platform-registry-construct";

export const GA_REGISTRY_CONSUMER_CONTEXT_SCHEMA =
  "agenticai.ga-registry-consumer-context/1.0" as const;

export interface GaResolvedRegistryRecord {
  readonly recordId: string;
  readonly recordArn: string;
  readonly descriptorSha256: string;
  readonly document: GaToolGovernanceDocument;
}

export interface GaRegistryConsumerContext {
  readonly schemaVersion: typeof GA_REGISTRY_CONSUMER_CONTEXT_SCHEMA;
  readonly environment: "nonprod" | "prod";
  readonly region: string;
  readonly platformAccountId: string;
  readonly sourceRevision: string;
  readonly registryId: string;
  readonly registryArn: string;
  readonly readerRoleArn: string;
  readonly readerExternalId: string;
  readonly records: readonly GaResolvedRegistryRecord[];
}

export interface GaRegistryConsumerExpectation {
  readonly environment: "nonprod" | "prod";
  readonly platformAccountId: string;
  readonly expectedToolIds: readonly string[];
}

function object(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${label} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
  label: string,
): void {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (JSON.stringify(actual) !== JSON.stringify(wanted)) {
    throw new Error(
      `${label} keys must be exactly [${wanted.join(", ")}]; got [${actual.join(", ")}].`,
    );
  }
}

function string(value: unknown, label: string): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.trim() !== value
  ) {
    throw new Error(
      `${label} must be a non-empty string without surrounding whitespace.`,
    );
  }
  return value;
}

function stringArray(value: unknown, label: string): readonly string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`${label} must be an array of strings.`);
  }
  const result = value as string[];
  if (new Set(result).size !== result.length) {
    throw new Error(`${label} must not contain duplicates.`);
  }
  return result;
}

function plainObject(value: unknown, label: string): Record<string, unknown> {
  return object(value, label);
}

function validateGovernanceDocument(
  raw: unknown,
  expectedToolId: string,
  platformAccountId: string,
  region: string,
): GaToolGovernanceDocument {
  const document = object(raw, `record ${expectedToolId} document`);
  exactKeys(
    document,
    [
      "schemaVersion",
      "catalogueVersion",
      "toolId",
      "description",
      "desiredApprovalStatus",
      "target",
      "mcp",
      "authorization",
      "ownership",
    ],
    `record ${expectedToolId} document`,
  );
  if (document.schemaVersion !== "agenticai.tool-governance/1.0") {
    throw new Error(
      `record ${expectedToolId} has an unsupported governance schema.`,
    );
  }
  string(
    document.catalogueVersion,
    `record ${expectedToolId} catalogueVersion`,
  );
  if (document.toolId !== expectedToolId) {
    throw new Error(`record ${expectedToolId} document toolId does not match.`);
  }
  const description = string(
    document.description,
    `record ${expectedToolId} description`,
  );
  if (document.desiredApprovalStatus !== "approved") {
    throw new Error(
      `record ${expectedToolId} desiredApprovalStatus must be approved.`,
    );
  }

  const target = object(document.target, `record ${expectedToolId} target`);
  exactKeys(target, ["type", "arn"], `record ${expectedToolId} target`);
  if (target.type !== "lambda") {
    throw new Error(
      `record ${expectedToolId} target.type must be lambda in R2.`,
    );
  }
  const targetArn = string(target.arn, `record ${expectedToolId} target.arn`);
  const lambdaArn = new RegExp(
    `^arn:(?:aws|aws-us-gov|aws-cn):lambda:${region}:${platformAccountId}:function:[A-Za-z0-9-_]+:[A-Za-z0-9-_$]+$`,
  );
  if (!lambdaArn.test(targetArn)) {
    throw new Error(
      `record ${expectedToolId} target.arn must be a same-Region Platform Lambda alias ARN.`,
    );
  }

  const mcp = object(document.mcp, `record ${expectedToolId} mcp`);
  exactKeys(
    mcp,
    ["toolName", "description", "inputSchema"],
    `record ${expectedToolId} mcp`,
  );
  if (mcp.toolName !== expectedToolId || mcp.description !== description) {
    throw new Error(
      `record ${expectedToolId} MCP identity differs from the governance document.`,
    );
  }
  plainObject(mcp.inputSchema, `record ${expectedToolId} mcp.inputSchema`);

  const authorization = object(
    document.authorization,
    `record ${expectedToolId} authorization`,
  );
  exactKeys(
    authorization,
    [
      "defaultDecision",
      "cedarPolicy",
      "allowedSubjects",
      "allowedGroups",
      "combination",
    ],
    `record ${expectedToolId} authorization`,
  );
  if (authorization.defaultDecision !== "DENY") {
    throw new Error(`record ${expectedToolId} defaultDecision must be DENY.`);
  }
  const cedarPolicy = string(
    authorization.cedarPolicy,
    `record ${expectedToolId} cedarPolicy`,
  );
  if (!cedarPolicy.includes("permit")) {
    throw new Error(
      `record ${expectedToolId} cedarPolicy must contain permit.`,
    );
  }
  const allowedSubjects = stringArray(
    authorization.allowedSubjects,
    `record ${expectedToolId} allowedSubjects`,
  );
  if (allowedSubjects.length !== 0) {
    throw new Error(
      `record ${expectedToolId} allowedSubjects is reserved for the later per-sub migration.`,
    );
  }
  const allowedGroups = stringArray(
    authorization.allowedGroups,
    `record ${expectedToolId} allowedGroups`,
  );
  const expectedCombination =
    allowedGroups.length > 0 ? "GROUP_ONLY" : "AUTHENTICATED";
  if (authorization.combination !== expectedCombination) {
    throw new Error(
      `record ${expectedToolId} combination must be ${expectedCombination}.`,
    );
  }

  const ownership = object(
    document.ownership,
    `record ${expectedToolId} ownership`,
  );
  exactKeys(
    ownership,
    ["ownerTeam", "costCentre"],
    `record ${expectedToolId} ownership`,
  );
  string(ownership.ownerTeam, `record ${expectedToolId} ownership.ownerTeam`);
  string(ownership.costCentre, `record ${expectedToolId} ownership.costCentre`);

  return document as unknown as GaToolGovernanceDocument;
}

export function parseGaRegistryConsumerContext(
  raw: unknown,
  expected: GaRegistryConsumerExpectation,
): GaRegistryConsumerContext {
  const context = object(raw, "GA Registry consumer context");
  exactKeys(
    context,
    [
      "schemaVersion",
      "environment",
      "region",
      "platformAccountId",
      "sourceRevision",
      "registryId",
      "registryArn",
      "readerRoleArn",
      "readerExternalId",
      "records",
    ],
    "GA Registry consumer context",
  );
  if (context.schemaVersion !== GA_REGISTRY_CONSUMER_CONTEXT_SCHEMA) {
    throw new Error(
      "GA Registry consumer context schemaVersion is unsupported.",
    );
  }
  if (context.environment !== expected.environment) {
    throw new Error(
      "GA Registry consumer context environment does not match the stage.",
    );
  }
  if (context.platformAccountId !== expected.platformAccountId) {
    throw new Error(
      "GA Registry consumer context platformAccountId does not match.",
    );
  }
  const region = string(context.region, "GA Registry consumer context region");
  if (!/^[a-z]{2}(?:-[a-z0-9]+)+-\d$/.test(region)) {
    throw new Error("GA Registry consumer context region is invalid.");
  }
  const sourceRevision = string(
    context.sourceRevision,
    "GA Registry consumer context sourceRevision",
  );
  if (!/^[0-9a-f]{40}$/.test(sourceRevision)) {
    throw new Error(
      "GA Registry consumer context sourceRevision must be a full Git SHA.",
    );
  }
  const registryId = string(
    context.registryId,
    "GA Registry consumer context registryId",
  );
  if (!/^[A-Za-z0-9]{16}$/.test(registryId)) {
    throw new Error(
      "GA Registry consumer context registryId must be 16 alphanumeric characters.",
    );
  }
  const partition = "(?:aws|aws-us-gov|aws-cn)";
  const registryArn = string(
    context.registryArn,
    "GA Registry consumer context registryArn",
  );
  if (
    !new RegExp(
      `^arn:${partition}:agent-registry:${region}:${expected.platformAccountId}:registry/${registryId}$`,
    ).test(registryArn)
  ) {
    throw new Error(
      "GA Registry consumer context registryArn does not match its identifiers.",
    );
  }
  const readerRoleArn = string(
    context.readerRoleArn,
    "GA Registry consumer context readerRoleArn",
  );
  if (
    !new RegExp(
      `^arn:${partition}:iam::${expected.platformAccountId}:role/AgenticAI-RegistryReader-${expected.environment}$`,
    ).test(readerRoleArn)
  ) {
    throw new Error(
      "GA Registry consumer context readerRoleArn is not the expected R1 role.",
    );
  }
  const readerExternalId = string(
    context.readerExternalId,
    "GA Registry consumer context readerExternalId",
  );
  if (
    readerExternalId !==
    `agenticai-registry-v1-${expected.environment}-${expected.platformAccountId}`
  ) {
    throw new Error(
      "GA Registry consumer context readerExternalId does not match R1.",
    );
  }
  if (!Array.isArray(context.records)) {
    throw new Error("GA Registry consumer context records must be an array.");
  }
  const expectedToolIds = [...expected.expectedToolIds].sort();
  if (
    expectedToolIds.length === 0 ||
    new Set(expectedToolIds).size !== expectedToolIds.length ||
    expectedToolIds.some(
      (toolId) => !/^[a-z][a-z0-9-]{1,62}[a-z0-9]$/.test(toolId),
    )
  ) {
    throw new Error(
      "expectedToolIds must be a non-empty unique kebab-case list.",
    );
  }

  const records = context.records.map((item, index) => {
    const record = object(
      item,
      `GA Registry consumer context records[${index}]`,
    );
    exactKeys(
      record,
      ["recordId", "recordArn", "descriptorSha256", "document"],
      `GA Registry consumer context records[${index}]`,
    );
    const documentObject = object(
      record.document,
      `records[${index}].document`,
    );
    const toolId = string(
      documentObject.toolId,
      `records[${index}].document.toolId`,
    );
    const recordId = string(record.recordId, `record ${toolId} recordId`);
    if (!/^[A-Za-z0-9]{12}$/.test(recordId)) {
      throw new Error(
        `record ${toolId} recordId must be 12 alphanumeric characters.`,
      );
    }
    const recordArn = string(record.recordArn, `record ${toolId} recordArn`);
    if (recordArn !== `${registryArn}/record/${recordId}`) {
      throw new Error(
        `record ${toolId} recordArn does not match its Registry and ID.`,
      );
    }
    const descriptorSha256 = string(
      record.descriptorSha256,
      `record ${toolId} descriptorSha256`,
    );
    if (!/^[0-9a-f]{64}$/.test(descriptorSha256)) {
      throw new Error(
        `record ${toolId} descriptorSha256 must be lower-case SHA-256.`,
      );
    }
    return {
      recordId,
      recordArn,
      descriptorSha256,
      document: validateGovernanceDocument(
        record.document,
        toolId,
        expected.platformAccountId,
        region,
      ),
    } satisfies GaResolvedRegistryRecord;
  });
  const actualToolIds = records.map((record) => record.document.toolId);
  if (JSON.stringify(actualToolIds) !== JSON.stringify(expectedToolIds)) {
    throw new Error(
      `GA Registry records must be sorted and exactly [${expectedToolIds.join(", ")}]; ` +
        `got [${actualToolIds.join(", ")}].`,
    );
  }
  if (
    new Set(records.map((record) => record.recordId)).size !== records.length
  ) {
    throw new Error(
      "GA Registry consumer context contains duplicate record IDs.",
    );
  }

  return {
    schemaVersion: GA_REGISTRY_CONSUMER_CONTEXT_SCHEMA,
    environment: expected.environment,
    region,
    platformAccountId: expected.platformAccountId,
    sourceRevision,
    registryId,
    registryArn,
    readerRoleArn,
    readerExternalId,
    records,
  };
}
