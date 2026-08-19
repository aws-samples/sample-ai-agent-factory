/**
 * SCP-04 — Enforce VPC Endpoints for Bedrock.
 *
 * Spec §2.2.5 L707-747. Denies Bedrock inference + ApplyGuardrail actions
 * unless the call routes through the approved Bedrock Runtime VPCE. Same
 * SSM-parameter resolution model as SCP-03.
 *
 * SECURITY NOTE (bypass-regression fix):
 *   The previous revision guarded `aws:SourceVpce` with
 *   `StringNotEqualsIfExists`. `IfExists` treats absence as satisfied so a
 *   public/console call that never carries `aws:SourceVpce` skipped the
 *   deny. We now pair a plain `StringNotEquals` with a twin Null-deny so
 *   both the wrong-VPCE and absent-key cases are blocked. A
 *   `PrincipalIsAWSService=false` guard keeps AWS-owned service principals
 *   from being self-denied.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from './index';

export function scp04EnforceBedrockVpce(): ScpDefinition {
  const body = {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'DenyBedrockOutsideApprovedVpce',
        Effect: 'Deny',
        Action: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
          'bedrock:ApplyGuardrail',
        ],
        Resource: '*',
        Condition: {
          StringNotEquals: {
            'aws:SourceVpce': '{{resolve:ssm:/agenticai/network/approved-bedrock-vpce-id}}',
          },
          BoolIfExists: {
            'aws:ViaAWSService': 'false',
            'aws:PrincipalIsAWSService': 'false',
          },
        },
      },
      {
        Sid: 'DenyBedrockWhenNoSourceVpce',
        Effect: 'Deny',
        Action: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
          'bedrock:ApplyGuardrail',
        ],
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
    'scp-04',
    'AgenticAI-SCP-04-EnforceBedrockVpce',
    'Deny Bedrock inference + ApplyGuardrail unless routed via the approved Bedrock VPCE (spec §2.2.5).',
    body,
  );
}
