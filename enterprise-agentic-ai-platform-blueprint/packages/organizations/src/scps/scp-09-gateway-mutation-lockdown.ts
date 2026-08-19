/**
 * SCP-09 — AgentCore Gateway Mutation Lockdown.
 *
 * Under D-03 v3, AgentCore Gateway is deployed into the workstream account by
 * the platform pipeline but is PLATFORM-GOVERNED. Any mutation of the Gateway
 * (changing targets, Cedar, the Lambda interceptor, or tags) must only be
 * possible from the `AgenticAI-D03-GatewayAdmin` role in the platform account.
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
import { toScpDefinition, type ScpDefinition } from './index';

export interface Scp09Options {
  /** Platform account id hosting `AgenticAI-D03-GatewayAdmin`. Required. */
  readonly platformAccountId: string;
}

export function scp09GatewayMutationLockdown(opts: Scp09Options): ScpDefinition {
  if (!/^[0-9]{12}$/.test(opts.platformAccountId)) {
    throw new Error(
      `SCP-09: platformAccountId must be a 12-digit AWS account id; got '${opts.platformAccountId}'.`,
    );
  }

  const adminRoleArn = `arn:aws:iam::${opts.platformAccountId}:role/AgenticAI-D03-GatewayAdmin`;
  const adminRoleSessionArn = `arn:aws:sts::${opts.platformAccountId}:assumed-role/AgenticAI-D03-GatewayAdmin/*`;

  const body = {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'DenyGatewayMutationExceptPlatformAdmin',
        Effect: 'Deny',
        Action: [
          'bedrock-agentcore:CreateGateway',
          'bedrock-agentcore:UpdateGateway',
          'bedrock-agentcore:DeleteGateway',
          'bedrock-agentcore:CreateGatewayTarget',
          'bedrock-agentcore:UpdateGatewayTarget',
          'bedrock-agentcore:DeleteGatewayTarget',
          'bedrock-agentcore:SynchronizeGatewayTargets',
          'bedrock-agentcore:TagResource',
          'bedrock-agentcore:UntagResource',
        ],
        Resource: 'arn:aws:bedrock-agentcore:*:*:gateway/*',
        Condition: {
          ArnNotLike: {
            'aws:PrincipalArn': [adminRoleArn, adminRoleSessionArn],
          },
          BoolIfExists: {
            'aws:PrincipalIsAWSService': 'false',
          },
        },
      },
    ],
  };

  return toScpDefinition(
    'scp-09',
    'AgenticAI-SCP-09-GatewayMutationLockdown',
    'Only the platform AgenticAI-D03-GatewayAdmin role may create or mutate AgentCore Gateways (D-03 v3).',
    body,
  );
}
