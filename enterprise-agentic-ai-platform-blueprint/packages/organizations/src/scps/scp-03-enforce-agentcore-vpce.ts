/**
 * SCP-03 — Enforce VPC Endpoints for AgentCore.
 *
 * Spec §2.2.4 L670-700. Denies `bedrock-agentcore:*` actions when the call
 * does not originate from an approved VPC endpoint. The list of approved
 * VPCE IDs per workload account is looked up at deploy time via SSM
 * parameter (spec §2.2.4 L704 permits either tag-based or SSM-based
 * resolution; we use SSM).
 *
 * The policy uses a placeholder `{{VPCE_IDS}}` that the runtime resolver
 * replaces with `aws:SourceVpce` condition values from the per-account SSM
 * parameter `/agenticai/network/approved-agentcore-vpce-ids`. Until that
 * parameter is written by Phase 4's AgenticVpcConstruct, the placeholder
 * resolves to a deploy-time template literal that SSM fills.
 *
 * SECURITY NOTE (bypass-regression fix):
 *   The previous revision guarded `aws:SourceVpce` with
 *   `StringNotEqualsIfExists`. The `IfExists` suffix makes the condition
 *   true (i.e. deny-skip) when the key is absent — so a console/public-API
 *   call that never carries `aws:SourceVpce` bypassed the deny. We now:
 *     1. Use plain `StringNotEquals` so a wrong VPCE still trips the deny
 *        (and absence no longer trips the allow).
 *     2. Add a twin Deny statement gated on `Null: aws:SourceVpce = true`
 *        so the absent-key case is denied explicitly.
 *
 * We also add a `PrincipalIsAWSService=false` guard so AWS-owned service
 * principals (Config, GuardDuty, CloudTrail, …) are not self-denied when
 * evaluating AgentCore APIs internally.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from './index';

export function scp03EnforceAgentCoreVpce(): ScpDefinition {
  const body = {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'DenyAgentCoreOutsideApprovedVpce',
        Effect: 'Deny',
        Action: ['bedrock-agentcore:*'],
        Resource: '*',
        Condition: {
          StringNotEquals: {
            // Resolved at SCP attachment time from SSM parameter
            // /agenticai/network/approved-agentcore-vpce-ids written by
            // AgenticVpcConstruct in Phase 4.
            'aws:SourceVpce': '{{resolve:ssm:/agenticai/network/approved-agentcore-vpce-ids}}',
          },
          BoolIfExists: {
            'aws:ViaAWSService': 'false',
            'aws:PrincipalIsAWSService': 'false',
          },
        },
      },
      {
        Sid: 'DenyAgentCoreWhenNoSourceVpce',
        Effect: 'Deny',
        Action: ['bedrock-agentcore:*'],
        Resource: '*',
        Condition: {
          Null: {
            'aws:SourceVpce': 'true',
          },
          BoolIfExists: {
            'aws:ViaAWSService': 'false',
            'aws:PrincipalIsAWSService': 'false',
          },
        },
      },
    ],
  };

  return toScpDefinition(
    'scp-03',
    'AgenticAI-SCP-03-EnforceAgentCoreVpce',
    'Deny bedrock-agentcore:* actions unless routed via an approved VPCE (spec §2.2.4).',
    body,
  );
}
