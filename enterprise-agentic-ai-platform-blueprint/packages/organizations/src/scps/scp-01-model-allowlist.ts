/**
 * SCP-01 — Restrict Bedrock Model Access.
 *
 * Spec §2.2.2 L576-608. Denies Bedrock inference actions for any
 * foundation-model ARN not on the platform allow-list. Applies to every
 * account under AgenticAI-Workloads.
 *
 * The allow-list is sourced from `@agenticai/platform-baselines`, which also
 * feeds Bedrock VPCE policies, LiteLLM router config, and AgentCore
 * execution-role IAM. Conformance tests in Phase 5 assert the four
 * representations agree.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from './index';

export function scp01ModelAllowlist(allowedModelArns: readonly string[]): ScpDefinition {
  if (allowedModelArns.length === 0) {
    throw new Error('SCP-01 allow-list must not be empty; an empty allow-list denies all Bedrock inference.');
  }

  const body = {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'DenyNonAllowListedBedrockModels',
        Effect: 'Deny',
        Action: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
          'bedrock:CreateModelInvocationJob',
        ],
        Resource: 'arn:aws:bedrock:*::foundation-model/*',
        Condition: {
          'ForAllValues:StringNotEquals': {
            'bedrock:FoundationModel': Array.from(allowedModelArns),
          },
        },
      },
    ],
  };

  return toScpDefinition(
    'scp-01',
    'AgenticAI-SCP-01-ModelAllowlist',
    'Deny Bedrock inference for any foundation-model ARN not on the platform allow-list (spec §2.2.2).',
    body,
  );
}
