/**
 * SCP-11 — Agent Registry Mutation Lockdown (D-03 v3 / v0.5.0+).
 *
 * Mirrors SCP-09 for the tool Registry control plane: registry-level
 * mutations and record approval-status transitions are allowed only for the
 * Platform account's pipeline (its CloudFormation execution role) and the
 * `AgenticAI-RegistryAdmin` break-glass role. Every other principal —
 * workstream developer permission sets, runtime roles, a workload account's
 * root — is denied at the Organization boundary.
 *
 * Both Registry control planes are covered:
 *   - GA AWS Agent Registry (`agent-registry:*`, ARNs
 *     `arn:aws:agent-registry:*:*:registry/*`) — the Registry this platform
 *     deploys (native `AWS::AgentRegistry::*` resources);
 *   - the earlier AgentCore Registry (`bedrock-agentcore:*Registry*`).
 *
 * LIVE-FOUND DEFECT (2026-09-25): the previous revision denied only the
 * `bedrock-agentcore:` actions. The deployed GA Registry signs requests as
 * `agent-registry` (botocore signing name), so the SCP governed nothing that
 * was deployed. It also exempted only `AgenticAI-RegistryAdmin`, a role the
 * reference deployment does not create — extending it to the GA namespace
 * without exempting the pipeline would have blocked CloudFormation from
 * deploying the Registry.
 *
 * Publisher/consumer surfaces (record create/update, submit, search,
 * discovery) are intentionally not denied here; IAM scopes them per
 * principal.
 *
 * Both the role ARN and the `assumed-role/<name>/*` session form are matched
 * (bypass-regression, same shape as SCP-05/SCP-09); AWS service principals
 * are exempt.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from "./index";

export interface Scp11Options {
  /** Platform account id hosting the Registry. Required. */
  readonly platformAccountId: string;
  /** Break-glass admin role name. Default `AgenticAI-RegistryAdmin`. */
  readonly registryAdminRoleName?: string;
  /**
   * Role-name patterns of the Platform pipeline principals that deploy the
   * Registry. Default: the CDK bootstrap CloudFormation execution role for
   * any qualifier and region.
   */
  readonly pipelineRoleNamePatterns?: readonly string[];
}

export const SCP11_REGISTRY_ACTIONS: readonly string[] = [
  "agent-registry:DeleteRegistry",
  "agent-registry:UpdateRegistry",
  "agent-registry:UpdateRegistryRecordStatus",
  "bedrock-agentcore:DeleteRegistry",
  "bedrock-agentcore:UpdateRegistry",
  "bedrock-agentcore:UpdateRegistryRecordStatus",
];

/**
 * Registry creation has no registry ARN yet and authorizes against "*"
 * (live IAM evaluator, 2026-09-25), so it needs its own "*"-scoped
 * statement; both actions are Registry-specific.
 */
export const SCP11_REGISTRY_CREATE_ACTIONS: readonly string[] = [
  "agent-registry:CreateRegistry",
  "bedrock-agentcore:CreateRegistry",
];

export function scp11RegistryMutationLockdown(
  opts: Scp11Options,
): ScpDefinition {
  if (!/^[0-9]{12}$/.test(opts.platformAccountId)) {
    throw new Error(
      `SCP-11: platformAccountId must be a 12-digit AWS account id; got '${opts.platformAccountId}'.`,
    );
  }
  const account = opts.platformAccountId;
  const roleNames = [
    opts.registryAdminRoleName ?? "AgenticAI-RegistryAdmin",
    ...(opts.pipelineRoleNamePatterns ?? [`cdk-*-cfn-exec-role-${account}-*`]),
  ];
  const exempt = roleNames.flatMap((name) => [
    `arn:aws:iam::${account}:role/${name}`,
    `arn:aws:sts::${account}:assumed-role/${name}/*`,
  ]);

  const exemptCondition = {
    ArnNotLike: { "aws:PrincipalArn": exempt },
    BoolIfExists: { "aws:PrincipalIsAWSService": "false" },
  };

  const body = {
    Version: "2012-10-17",
    Statement: [
      {
        Sid: "DenyRegistryMutationExceptPlatformPipeline",
        Effect: "Deny",
        Action: Array.from(SCP11_REGISTRY_ACTIONS),
        Resource: [
          "arn:aws:agent-registry:*:*:registry/*",
          "arn:aws:bedrock-agentcore:*:*:registry/*",
        ],
        Condition: exemptCondition,
      },
      {
        Sid: "DenyRegistryCreationExceptPlatformPipeline",
        Effect: "Deny",
        Action: Array.from(SCP11_REGISTRY_CREATE_ACTIONS),
        Resource: "*",
        Condition: exemptCondition,
      },
    ],
  };

  return toScpDefinition(
    "scp-11",
    "AgenticAI-SCP-11-RegistryMutationLockdown",
    "Only the Platform pipeline and the RegistryAdmin break-glass role may mutate the tool Registry or change record approval status (D-03 v3).",
    body,
  );
}
