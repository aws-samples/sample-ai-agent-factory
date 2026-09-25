/**
 * SCP-09 — AgentCore Gateway Mutation Lockdown.
 *
 * Under D-03 v3, AgentCore Gateway is deployed into the workstream account by
 * the platform pipeline but is PLATFORM-GOVERNED. Any mutation of the Gateway
 * (changing targets, Cedar, the Lambda interceptor, or tags) must only be
 * possible only from environment-qualified `AgenticAI-D03-*-GatewayAdmin`
 * roles in configured Workstream accounts. Those roles are created by the
 * Workload pipeline before Gateway deployment and trust only Lambda, so the
 * AgentCore API call executes in the account that owns the Gateway.
 * Every other principal — including the workload account's root / admin IAM
 * user / any runtime role — must be denied.
 *
 * `CreateGateway` is also denied so a workstream admin cannot sidestep the
 * lockdown by creating a rogue unmanaged Gateway alongside the platform one.
 *
 * SECURITY NOTE (bypass-regression fix — mirrors SCP-05):
 *   When the admin role is assumed, IAM evaluates `aws:PrincipalArn` as the
 *   assumed-role session ARN (`arn:aws:sts::<acct>:assumed-role/<name>/<sess>`),
 *   not the role ARN. We therefore ArnNotLike against BOTH the role ARN and
 *   the `assumed-role/<name>/*` session form. A `PrincipalIsAWSService=false`
 *   guard keeps AWS-owned service principals from being self-denied.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from "./index";

export interface Scp09Options {
  /** Workstream accounts hosting pipeline-created environment GatewayAdmin roles. */
  readonly workloadAccountIds: readonly string[];
}

export function scp09GatewayMutationLockdown(
  opts: Scp09Options,
): ScpDefinition {
  const workloadAccountIds = [...new Set(opts.workloadAccountIds)].sort();
  if (
    workloadAccountIds.length === 0 ||
    workloadAccountIds.some((accountId) => !/^[0-9]{12}$/.test(accountId))
  ) {
    throw new Error(
      "SCP-09: workloadAccountIds must be a non-empty list of 12-digit account IDs.",
    );
  }

  const adminRoleArns = workloadAccountIds.map(
    (accountId) =>
      `arn:aws:iam::${accountId}:role/AgenticAI-D03-*-GatewayAdmin`,
  );
  const adminRoleSessionArns = workloadAccountIds.map(
    (accountId) =>
      `arn:aws:sts::${accountId}:assumed-role/AgenticAI-D03-*-GatewayAdmin/*`,
  );

  const exemptCondition = {
    ArnNotLike: {
      "aws:PrincipalArn": [...adminRoleArns, ...adminRoleSessionArns],
    },
    BoolIfExists: {
      "aws:PrincipalIsAWSService": "false",
    },
  };

  const body = {
    Version: "2012-10-17",
    Statement: [
      {
        Sid: "DenyGatewayMutationExceptPlatformAdmin",
        Effect: "Deny",
        Action: [
          "bedrock-agentcore:UpdateGateway",
          "bedrock-agentcore:DeleteGateway",
          "bedrock-agentcore:CreateGatewayTarget",
          "bedrock-agentcore:UpdateGatewayTarget",
          "bedrock-agentcore:DeleteGatewayTarget",
          "bedrock-agentcore:SynchronizeGatewayTargets",
          "bedrock-agentcore:TagResource",
          "bedrock-agentcore:UntagResource",
        ],
        Resource: "arn:aws:bedrock-agentcore:*:*:gateway/*",
        Condition: exemptCondition,
      },
      {
        // LIVE-FOUND GAP (IAM evaluator, 2026-09-25): a create call has no
        // Gateway ARN yet and authorizes against "*", so CreateGateway inside
        // the gateway-scoped statement above never matched — any principal
        // could create a rogue Gateway. It gets its own "*" statement; the
        // action is Gateway-specific, so "*" widens nothing else.
        Sid: "DenyGatewayCreationExceptPlatformAdmin",
        Effect: "Deny",
        Action: ["bedrock-agentcore:CreateGateway"],
        Resource: "*",
        Condition: exemptCondition,
      },
    ],
  };

  return toScpDefinition(
    "scp-09",
    "AgenticAI-SCP-09-GatewayMutationLockdown",
    "Only pipeline-created, environment-qualified Workstream GatewayAdmin roles may mutate AgentCore Gateways.",
    body,
  );
}
