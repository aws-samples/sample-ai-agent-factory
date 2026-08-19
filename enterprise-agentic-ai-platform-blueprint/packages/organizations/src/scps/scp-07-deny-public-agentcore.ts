/**
 * SCP-07 — Deny Public AgentCore Resources.
 *
 * Spec §2.2.8 L833-880. AgentCore Runtime, Browser Tool, and Code
 * Interpreter cannot be created in PUBLIC network mode; subnets and
 * security groups MUST be specified. This is the Org-level enforcement of
 * the VPC-only posture (spec §2.3.8 L1427-1444 / R-NET-039 through R-NET-043).
 *
 * The gate uses `Null` condition on the `subnets` and `securityGroups`
 * request parameters for the four create/update actions that can set
 * network mode.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from './index';

export function scp07DenyPublicAgentCore(): ScpDefinition {
  const body = {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'DenyAgentCoreCreationWithoutSubnets',
        Effect: 'Deny',
        Action: [
          'bedrock-agentcore:CreateAgentRuntime',
          'bedrock-agentcore:UpdateAgentRuntime',
          'bedrock-agentcore:CreateBrowser',
          'bedrock-agentcore:CreateCodeInterpreter',
        ],
        Resource: '*',
        Condition: {
          Null: {
            'bedrock-agentcore:subnets': 'true',
          },
        },
      },
      {
        Sid: 'DenyAgentCoreCreationWithoutSecurityGroups',
        Effect: 'Deny',
        Action: [
          'bedrock-agentcore:CreateAgentRuntime',
          'bedrock-agentcore:UpdateAgentRuntime',
          'bedrock-agentcore:CreateBrowser',
          'bedrock-agentcore:CreateCodeInterpreter',
        ],
        Resource: '*',
        Condition: {
          Null: {
            'bedrock-agentcore:securityGroups': 'true',
          },
        },
      },
    ],
  };

  return toScpDefinition(
    'scp-07',
    'AgenticAI-SCP-07-DenyPublicAgentCore',
    'Deny AgentCore resource creation without subnets and security groups (spec §2.2.8).',
    body,
  );
}
