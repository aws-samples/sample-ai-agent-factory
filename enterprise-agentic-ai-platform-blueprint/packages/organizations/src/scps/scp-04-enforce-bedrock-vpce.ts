/**
 * SCP-04 — Enforce VPC Endpoints for Bedrock.
 *
 * Spec §2.2.5 L707-747. Denies Bedrock inference + ApplyGuardrail unless the
 * call routes through an approved Bedrock Runtime VPC endpoint.
 *
 * SCOPE: VPC-mode OUs only, like SCP-03. In the reference deployment the
 * inference Gateway's guardrail interceptor (a Lambda outside a VPC) calls
 * `bedrock:ApplyGuardrail` without `aws:SourceVpce`; attaching SCP-04 to its
 * account's OU would deny it (live IAM evaluator, 2026-09-25). `buildScpSet`
 * emits SCP-04 only when the exact approved VPCE ids are supplied.
 *
 * LIVE-FOUND DEFECTS fixed 2026-09-25: the previous revision resolved the id
 * from an SSM parameter in the Workload account while the SCP is deployed
 * from the Management account, and listed `bedrock:Converse` /
 * `bedrock:ConverseStream`, which are not IAM actions (Converse authorizes
 * as `bedrock:InvokeModel*`).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { assertVpceIds } from "./scp-03-enforce-agentcore-vpce";
import { toScpDefinition, type ScpDefinition } from "./index";

export const SCP04_BEDROCK_ACTIONS: readonly string[] = [
  "bedrock:InvokeModel",
  "bedrock:InvokeModelWithResponseStream",
  "bedrock:ApplyGuardrail",
];

const EXEMPT_SERVICES = {
  BoolIfExists: {
    "aws:ViaAWSService": "false",
    "aws:PrincipalIsAWSService": "false",
  },
};

export function scp04EnforceBedrockVpce(
  approvedVpceIds: readonly string[],
): ScpDefinition {
  assertVpceIds("SCP-04", approvedVpceIds);
  const body = {
    Version: "2012-10-17",
    Statement: [
      {
        Sid: "DenyBedrockOutsideApprovedVpce",
        Effect: "Deny",
        Action: Array.from(SCP04_BEDROCK_ACTIONS),
        Resource: "*",
        Condition: {
          StringNotEquals: { "aws:SourceVpce": Array.from(approvedVpceIds) },
          ...EXEMPT_SERVICES,
        },
      },
      {
        Sid: "DenyBedrockWhenNoSourceVpce",
        Effect: "Deny",
        Action: Array.from(SCP04_BEDROCK_ACTIONS),
        Resource: "*",
        Condition: { Null: { "aws:SourceVpce": "true" }, ...EXEMPT_SERVICES },
      },
    ],
  };

  return toScpDefinition(
    "scp-04",
    "AgenticAI-SCP-04-EnforceBedrockVpce",
    "Deny Bedrock inference + ApplyGuardrail unless routed via an approved VPCE (spec §2.2.5; VPC-mode OUs only).",
    body,
  );
}
