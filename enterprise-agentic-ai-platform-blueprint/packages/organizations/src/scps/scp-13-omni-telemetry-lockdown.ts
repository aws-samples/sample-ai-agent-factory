/**
 * SCP-13 — CloudWatch Omni telemetry lockdown (ADR-15, ADR-13 amended).
 *
 * The hub model works because the organization centralization rule pulls a
 * cell's telemetry from above: the cell cannot read, change or stop the rule.
 * What a cell principal CAN do is starve the rule at the source. This SCP
 * closes those switches for every principal in the Workstream OUs except the
 * Workload pipeline (and an optional named break-glass role):
 *
 *   1. Omni domains. One organization domain is created from the Management
 *      account; an account-level domain in a cell fragments sign-in and
 *      identity and is never wanted. Denied to everyone, no exemption.
 *   2. Omni spaces and their grants. Per-cell spaces exist only as the
 *      classification-driven fallback of ADR-15 and are deployed by the
 *      pipeline. Hand-made spaces, grants and access profiles are denied.
 *   3. Transaction Search. Spans reach `aws/spans` (and therefore the hub)
 *      only while the account's trace destination is CloudWatch Logs, the
 *      indexing rule is above zero and the X-Ray CloudWatch Logs resource
 *      policy exists. All three are pipeline-owned (TransactionSearchConstruct).
 *   4. AgentCore Runtime observability. Runtime logs and spans flow through
 *      CloudWatch Logs vended deliveries created by the Runtime stack;
 *      deleting or re-pointing a delivery silences the agent.
 *
 * IAM action prefix: CloudWatch Omni authorizes as `cloudwatch:*` (verified
 * against the AWS managed policies CloudWatchOmniSpaceAccessPolicy and
 * CloudWatchOmniDomainAccessPolicy, 2026-11-06).
 *
 * Exemption shape mirrors SCP-09/11: both the role ARN and the
 * `assumed-role/<name>/*` session form are matched, and AWS service
 * principals are never self-denied. The pipeline role is matched by name
 * pattern across accounts because the policy attaches to an OU, not to one
 * account; the default pattern is the CDK bootstrap CloudFormation execution
 * role every cell is bootstrapped with.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from "./index";

export interface Scp13Options {
  /**
   * Role-name patterns (IAM wildcards) of the pipeline principals allowed to
   * manage spaces, Transaction Search and Runtime deliveries in a cell.
   * Default: the CDK bootstrap CloudFormation execution role.
   */
  readonly pipelineRoleNamePatterns?: readonly string[];
  /**
   * Exact break-glass role names (for example `AgenticAI-ObservabilityAdmin`)
   * that may also operate these controls. Default none.
   */
  readonly breakGlassRoleNames?: readonly string[];
}

/** Account-level Omni domain lifecycle: never in a cell. */
export const SCP13_DOMAIN_ACTIONS: readonly string[] = [
  "cloudwatch:CreateDomain",
  "cloudwatch:UpdateDomain",
  "cloudwatch:DeleteDomain",
  "cloudwatch:CreateDomainForOrganization",
  "cloudwatch:UpdateDomainForOrganization",
  "cloudwatch:DeleteDomainForOrganization",
];

/** Space, grant and access-profile lifecycle: pipeline only. */
export const SCP13_SPACE_ACTIONS: readonly string[] = [
  "cloudwatch:CreateSpace",
  "cloudwatch:UpdateSpace",
  "cloudwatch:DeleteSpace",
  "cloudwatch:CreateAccessGrant",
  "cloudwatch:DeleteAccessGrant",
  "cloudwatch:CreateAccessProfile",
  "cloudwatch:UpdateAccessProfile",
  "cloudwatch:DeleteAccessProfile",
];

/** The three switches that stop spans reaching aws/spans. */
export const SCP13_TRANSACTION_SEARCH_ACTIONS: readonly string[] = [
  "xray:UpdateTraceSegmentDestination",
  "xray:UpdateIndexingRule",
  "logs:PutResourcePolicy",
  "logs:DeleteResourcePolicy",
];

/** CloudWatch Logs vended deliveries that carry AgentCore Runtime telemetry. */
export const SCP13_DELIVERY_ACTIONS: readonly string[] = [
  "logs:DeleteDelivery",
  "logs:DeleteDeliverySource",
  "logs:DeleteDeliveryDestination",
  "logs:DeleteDeliveryDestinationPolicy",
  "logs:UpdateDeliveryConfiguration",
];

const ROLE_NAME_PATTERN = /^[\w+=,.@*?-]{1,64}$/;

export function scp13OmniTelemetryLockdown(opts: Scp13Options = {}): ScpDefinition {
  const pipelinePatterns = opts.pipelineRoleNamePatterns ?? ["cdk-*-cfn-exec-role-*"];
  const breakGlass = opts.breakGlassRoleNames ?? [];
  for (const name of [...pipelinePatterns, ...breakGlass]) {
    if (!ROLE_NAME_PATTERN.test(name)) {
      throw new Error(
        `SCP-13: role name or pattern '${name}' is not a valid IAM role name pattern (1-64 chars of [\\w+=,.@*?-]).`,
      );
    }
  }
  if (pipelinePatterns.length === 0) {
    throw new Error("SCP-13: at least one pipeline role-name pattern is required, or the pipeline locks itself out.");
  }
  const exemptNames = [...new Set([...pipelinePatterns, ...breakGlass])];
  // OU-wide policy: the account component is a wildcard, the role name is the discriminator.
  const exemptArns = exemptNames.flatMap((name) => [
    `arn:aws:iam::*:role/${name}`,
    `arn:aws:sts::*:assumed-role/${name}/*`,
  ]);
  const pipelineOnly = {
    ArnNotLike: { "aws:PrincipalArn": exemptArns },
    BoolIfExists: { "aws:PrincipalIsAWSService": "false" },
  };
  const everyone = {
    BoolIfExists: { "aws:PrincipalIsAWSService": "false" },
  };

  const body = {
    Version: "2012-10-17",
    Statement: [
      {
        Sid: "DenyAccountLevelOmniDomainInCells",
        Effect: "Deny",
        Action: Array.from(SCP13_DOMAIN_ACTIONS),
        Resource: "*",
        Condition: everyone,
      },
      {
        Sid: "DenyOmniSpaceLifecycleExceptPipeline",
        Effect: "Deny",
        Action: Array.from(SCP13_SPACE_ACTIONS),
        Resource: "*",
        Condition: pipelineOnly,
      },
      {
        Sid: "DenyTransactionSearchTamperingExceptPipeline",
        Effect: "Deny",
        Action: Array.from(SCP13_TRANSACTION_SEARCH_ACTIONS),
        Resource: "*",
        Condition: pipelineOnly,
      },
      {
        Sid: "DenyRuntimeDeliveryTamperingExceptPipeline",
        Effect: "Deny",
        Action: Array.from(SCP13_DELIVERY_ACTIONS),
        Resource: "*",
        Condition: pipelineOnly,
      },
    ],
  };

  return toScpDefinition(
    "scp-13",
    "AgenticAI-SCP-13-OmniTelemetryLockdown",
    "Cells cannot create Omni domains, hand-make spaces or grants, or switch off Transaction Search and Runtime log deliveries; only the Workload pipeline (and a named break-glass role) may (ADR-15).",
    body,
  );
}
