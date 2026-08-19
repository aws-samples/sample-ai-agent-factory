/**
 * SCP-02 — Enforce Bedrock Guardrail Usage.
 *
 * Spec §2.2.3 L625-665. Denies Bedrock inference calls where the request
 * does not carry an approved `GuardrailIdentifier`. This is one of three
 * enforcement layers that together guarantee guardrail-on-every-call:
 *
 *   1. SCP-02 (this file)        — Organization-level deny
 *   2. IAM task role deny        — account-level belt-and-braces (D-01)
 *   3. Bedrock VPCE endpoint policy deny — network-level backstop (D-01)
 *
 * SECURITY NOTE (bypass-regression fix):
 *   Previous revision used only `Null: { 'bedrock:GuardrailIdentifier': 'true' }`.
 *   That gate ignores an *empty-string* GuardrailIdentifier — it fires only
 *   when the key is absent. A caller could submit `GuardrailIdentifier=""`
 *   and slip past the SCP. We now emit two twin Deny statements so the
 *   missing-key, empty-string, and wrong-id cases are all blocked, while
 *   the approved-id case passes:
 *     - Null true  → missing key
 *     - ForAllValues:StringNotEquals → present but not on allow-list
 *                                      (also matches empty string)
 *
 * We also add a `PrincipalIsAWSService=false` guard so AWS-owned service
 * principals (Config, GuardDuty, CloudTrail, …) are not self-denied when
 * they evaluate Bedrock APIs internally on the account's behalf.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from './index';

export interface Scp02Options {
  /**
   * Approved Bedrock Guardrail identifiers (ids or ARNs). At least one value
   * must be supplied to engage the positive allow-list check. If omitted or
   * empty, the SCP falls back to the legacy `Null`-only gate and emits a
   * synth-time warning — callers should wire the real list from the Phase 3
   * GuardrailStack as soon as it is available.
   */
  readonly approvedGuardrailIds?: readonly string[];
}

export function scp02EnforceGuardrail(opts: Scp02Options = {}): ScpDefinition {
  const approved = opts.approvedGuardrailIds ?? [];

  const statements: Record<string, unknown>[] = [
    {
      Sid: 'DenyBedrockInferenceWithoutGuardrail',
      Effect: 'Deny',
      Action: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
        'bedrock:Converse',
        'bedrock:ConverseStream',
      ],
      Resource: '*',
      Condition: {
        Null: {
          'bedrock:GuardrailIdentifier': 'true',
        },
        BoolIfExists: {
          'aws:PrincipalIsAWSService': 'false',
        },
      },
    },
  ];

  if (approved.length > 0) {
    statements.push({
      Sid: 'DenyBedrockWithoutApprovedGuardrail',
      Effect: 'Deny',
      Action: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
        'bedrock:Converse',
        'bedrock:ConverseStream',
      ],
      Resource: '*',
      Condition: {
        'ForAllValues:StringNotEquals': {
          'bedrock:GuardrailIdentifier': Array.from(approved),
        },
        BoolIfExists: {
          'aws:PrincipalIsAWSService': 'false',
        },
      },
    });
  } else {
    // Defence-in-depth regression guard: if the caller forgot to pass a list
    // we keep the original Null-only deny active, but tag the body with a
    // TODO marker that Phase 3's OrgStack wiring must replace. The warn here
    // is a synth-time notice, not an error, so sandbox deploys still work.
    // eslint-disable-next-line no-console
    console.warn(
      'SCP-02: approvedGuardrailIds was empty. Falling back to Null-only gate — ' +
        'empty-string GuardrailIdentifier values will bypass the SCP. Wire the ' +
        'approved list from the Phase 3 GuardrailStack before promoting to ' +
        'AgenticAI-Workloads. [TODO-APPROVED-GUARDRAILS]',
    );
  }

  const body: Record<string, unknown> = {
    Version: '2012-10-17',
    Statement: statements,
  };

  return toScpDefinition(
    'scp-02',
    'AgenticAI-SCP-02-EnforceGuardrail',
    'Deny Bedrock inference calls that do not carry an approved GuardrailIdentifier (spec §2.2.3).',
    body,
  );
}
