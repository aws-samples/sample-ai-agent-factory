/**
 * SCP-02 — Enforce Bedrock Guardrail Usage.
 *
 * Spec §2.2.3 L625-665. Denies Bedrock inference calls where the request
 * does not carry an approved `GuardrailIdentifier`. One of three layers that
 * together guarantee guardrail-on-every-call on the direct Bedrock path:
 *
 *   1. SCP-02 (this file)        — Organization-level deny
 *   2. IAM task role deny        — account-level belt-and-braces (D-01)
 *   3. Bedrock VPCE endpoint policy deny — network-level backstop (D-01)
 *
 * (The Gateway inference path is governed by the Gateway's guardrail
 * interceptor instead: the Gateway calls Bedrock Mantle, where this key is
 * not evaluated.)
 *
 * Statements (each proven on the live IAM evaluator, 2026-09-25):
 *   - Null true            → the key is absent                       → deny
 *   - ArnNotLike approved  → present but not an approved guardrail  → deny
 *     (`<arn>` and `<arn>:<version>`, which is how the key carries a
 *     numeric version)
 *   - StringEquals ""      → empty string; ARN operators do not match an
 *     empty value, so without this twin `GuardrailIdentifier=""` passed
 *
 * LIVE-FOUND DEFECTS fixed 2026-09-25: the previous revision listed
 * `bedrock:Converse` / `bedrock:ConverseStream` (not IAM actions — Converse
 * authorizes as InvokeModel*), and used `ForAllValues:` on this
 * single-valued key, which Access Analyzer flags as overly permissive.
 *
 * A `PrincipalIsAWSService=false` guard keeps AWS-owned service principals
 * from being self-denied.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from "./index";

export interface Scp02Options {
  /**
   * Approved Bedrock Guardrail ARNs. At least one value engages the positive
   * allow-list check. If omitted or empty, the SCP falls back to the
   * `Null`-only gate and emits a synth-time warning.
   */
  readonly approvedGuardrailIds?: readonly string[];
}

/** Bedrock inference actions the guardrail requirement applies to (valid IAM actions only). */
export const SCP02_GUARDRAIL_ACTIONS: readonly string[] = [
  "bedrock:InvokeModel",
  "bedrock:InvokeModelWithResponseStream",
];

const NOT_AWS_SERVICE = {
  BoolIfExists: { "aws:PrincipalIsAWSService": "false" },
};

/**
 * Role-name prefix of the Platform inference Gateway's Mantle role
 * (`AgenticAI-InferenceGateway-<env>`, platform-inference-gateway construct).
 */
export const INFERENCE_GATEWAY_ROLE_PREFIX = "AgenticAI-InferenceGateway-";

export function scp02EnforceGuardrail(opts: Scp02Options = {}): ScpDefinition {
  const approved = opts.approvedGuardrailIds ?? [];
  for (const id of approved) {
    if (!/^arn:aws:bedrock:[a-z0-9-]+:\d{12}:guardrail\/[a-z0-9]+$/.test(id)) {
      throw new Error(
        `SCP-02 approvedGuardrailIds must be unversioned guardrail ARNs (the key also matches <arn>:<version>), got: ${id}`,
      );
    }
  }

  const statements: Record<string, unknown>[] = [
    {
      Sid: "DenyBedrockInferenceWithoutGuardrail",
      Effect: "Deny",
      Action: Array.from(SCP02_GUARDRAIL_ACTIONS),
      Resource: "*",
      Condition: {
        Null: { "bedrock:GuardrailIdentifier": "true" },
        ...NOT_AWS_SERVICE,
      },
    },
    {
      // Bedrock Mantle has no guardrail condition key, so a direct
      // `bedrock-mantle:CreateInference` from a workload principal would skip
      // this SCP and the model allow-list. Guardrail enforcement on the
      // Mantle path exists only in the Platform inference Gateway's REQUEST
      // interceptor, so only that Gateway's role may call Mantle
      // (live-found gap, 2026-09-25).
      Sid: "DenyDirectMantleInference",
      Effect: "Deny",
      Action: ["bedrock-mantle:CreateInference"],
      Resource: "*",
      Condition: {
        ArnNotLike: {
          "aws:PrincipalArn": [
            `arn:aws:iam::*:role/${INFERENCE_GATEWAY_ROLE_PREFIX}*`,
            `arn:aws:sts::*:assumed-role/${INFERENCE_GATEWAY_ROLE_PREFIX}*/*`,
          ],
        },
        ...NOT_AWS_SERVICE,
      },
    },
  ];

  if (approved.length > 0) {
    statements.push(
      {
        Sid: "DenyBedrockWithoutApprovedGuardrail",
        Effect: "Deny",
        Action: Array.from(SCP02_GUARDRAIL_ACTIONS),
        Resource: "*",
        Condition: {
          ArnNotLike: {
            "bedrock:GuardrailIdentifier": approved.flatMap((arn) => [
              arn,
              `${arn}:*`,
            ]),
          },
          ...NOT_AWS_SERVICE,
        },
      },
      {
        Sid: "DenyBedrockWithEmptyGuardrail",
        Effect: "Deny",
        Action: Array.from(SCP02_GUARDRAIL_ACTIONS),
        Resource: "*",
        Condition: {
          StringEquals: { "bedrock:GuardrailIdentifier": "" },
          ...NOT_AWS_SERVICE,
        },
      },
    );
  } else {
    // eslint-disable-next-line no-console
    console.warn(
      "SCP-02: approvedGuardrailIds was empty. Falling back to Null-only gate — " +
        "empty-string and unapproved GuardrailIdentifier values will bypass the SCP. Wire the " +
        "approved list from the GuardrailStack before promoting to " +
        "AgenticAI-Workloads. [TODO-APPROVED-GUARDRAILS]",
    );
  }

  return toScpDefinition(
    "scp-02",
    "AgenticAI-SCP-02-EnforceGuardrail",
    "Deny Bedrock inference calls that do not carry an approved GuardrailIdentifier (spec §2.2.3).",
    { Version: "2012-10-17", Statement: statements },
  );
}
