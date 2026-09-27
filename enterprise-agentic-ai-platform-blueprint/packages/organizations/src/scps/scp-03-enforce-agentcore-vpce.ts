/**
 * SCP-03 — Enforce VPC Endpoints for AgentCore.
 *
 * Spec §2.2.4 L670-700. Denies `bedrock-agentcore:*` actions when the call
 * does not originate from an approved VPC endpoint.
 *
 * SCOPE: this control is for OUs whose AgentCore workloads run in VPC mode
 * behind interface endpoints. The reference deployment's Runtime runs
 * `networkMode: PUBLIC`; attaching SCP-03 to its OU would deny its own
 * Memory, Identity and Gateway calls (proven on the live IAM evaluator,
 * 2026-09-25). `buildScpSet` therefore emits SCP-03 only when the caller
 * supplies the exact approved VPCE ids.
 *
 * LIVE-FOUND DEFECT (2026-09-25): the previous revision embedded
 * `{{resolve:ssm:/agenticai/network/approved-agentcore-vpce-ids}}`. That
 * parameter is a StringList of three ids, which resolves to ONE
 * comma-joined string — `StringNotEquals` against it never equals a real
 * endpoint id, so every AgentCore call would have been denied. It was also
 * written in the Workload account while the SCP is deployed from the
 * Management account. The ids are now explicit synth-time inputs, rendered
 * as a real JSON array (any of them satisfies the condition).
 *
 * Twin statements close the absent-key bypass: `StringNotEquals` denies a
 * wrong VPCE, `Null: aws:SourceVpce = true` denies public calls. AWS
 * service principals and calls made on the caller's behalf by an AWS
 * service are exempt.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from "./index";

const VPCE_ID = /^vpce-[0-9a-f]{8,17}$/;

export function assertVpceIds(label: string, ids: readonly string[]): void {
  if (ids.length === 0) {
    throw new Error(
      `${label}: at least one approved VPC endpoint id is required`,
    );
  }
  for (const id of ids) {
    if (!VPCE_ID.test(id)) {
      throw new Error(`${label}: '${id}' is not a VPC endpoint id (vpce-…)`);
    }
  }
}

const EXEMPT_SERVICES = {
  BoolIfExists: {
    "aws:ViaAWSService": "false",
    "aws:PrincipalIsAWSService": "false",
  },
};

export function scp03EnforceAgentCoreVpce(
  approvedVpceIds: readonly string[],
): ScpDefinition {
  assertVpceIds("SCP-03", approvedVpceIds);
  const body = {
    Version: "2012-10-17",
    Statement: [
      {
        Sid: "DenyAgentCoreOutsideApprovedVpce",
        Effect: "Deny",
        Action: ["bedrock-agentcore:*"],
        Resource: "*",
        Condition: {
          StringNotEquals: { "aws:SourceVpce": Array.from(approvedVpceIds) },
          ...EXEMPT_SERVICES,
        },
      },
      {
        Sid: "DenyAgentCoreWhenNoSourceVpce",
        Effect: "Deny",
        Action: ["bedrock-agentcore:*"],
        Resource: "*",
        Condition: { Null: { "aws:SourceVpce": "true" }, ...EXEMPT_SERVICES },
      },
    ],
  };

  return toScpDefinition(
    "scp-03",
    "AgenticAI-SCP-03-EnforceAgentCoreVpce",
    "Deny bedrock-agentcore:* actions unless routed via an approved VPCE (spec §2.2.4; VPC-mode OUs only).",
    body,
  );
}
