/**
 * SCP-10 — Tool-Invoke Allow-list.
 *
 * Defence-in-depth at the tool-invocation layer. Even if AgentCore Gateway
 * Cedar authorization and the Lambda target resource policy both fail open,
 * workstream runtime roles must only be able to invoke Lambdas that are
 * catalogued in PLATFORM_TOOL_CATALOGUE (see `packages/platform-tool-catalogue/`).
 *
 * The allow-list is passed in at synth from the catalogue: callers run
 * `resolveSubscribedTools()` which yields `ToolSpec[]`, then flatten to a
 * `string[]` of `.targetArn` values before passing them here.
 *
 * PRINCIPAL NARROWING:
 *   The Deny fires ONLY when the principal ARN matches the D-03 runtime-role
 *   naming convention (`AgenticAI-D03-*-runtime`). This avoids self-denying:
 *     - Platform pipelines that invoke catalogued Lambdas during deploy
 *     - AWS service principals (CloudWatch Events, Step Functions, etc.)
 *     - Break-glass admin principals
 *   A `PrincipalIsAWSService=false` guard is also included belt-and-braces.
 *
 * If the catalogue resolves to an empty list the SCP is omitted entirely
 * (NotResource: [] would deny EVERY Lambda:Invoke from runtime roles, which
 * would lock the platform out). `buildScpSet` emits a synth-time warning in
 * that case — same pattern as SCP-02's `approvedGuardrailIds` fallback.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from './index';

export interface Scp10Options {
  /** Fully-resolved Lambda target ARNs from PLATFORM_TOOL_CATALOGUE. */
  readonly allowedToolTargetArns: readonly string[];
}

export function scp10ToolInvokeAllowlist(opts: Scp10Options): ScpDefinition {
  if (opts.allowedToolTargetArns.length === 0) {
    throw new Error(
      'SCP-10: allowedToolTargetArns must not be empty; an empty list denies every Lambda invocation from runtime roles.',
    );
  }

  const body = {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'DenyLambdaInvokeFromRuntimeRolesExceptCataloguedTargets',
        Effect: 'Deny',
        Action: [
          'lambda:InvokeFunction',
          'lambda:InvokeAsync',
        ],
        NotResource: [...opts.allowedToolTargetArns],
        Condition: {
          ArnLike: {
            'aws:PrincipalArn': [
              'arn:aws:iam::*:role/AgenticAI-D03-*-runtime',
              'arn:aws:sts::*:assumed-role/AgenticAI-D03-*-runtime/*',
            ],
          },
          BoolIfExists: {
            'aws:PrincipalIsAWSService': 'false',
          },
        },
      },
    ],
  };

  return toScpDefinition(
    'scp-10',
    'AgenticAI-SCP-10-ToolInvokeAllowlist',
    'Deny Lambda invocations from D-03 runtime roles to any target not catalogued in PLATFORM_TOOL_CATALOGUE.',
    body,
  );
}
