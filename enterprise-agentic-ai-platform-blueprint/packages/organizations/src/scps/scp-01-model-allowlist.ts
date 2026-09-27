/**
 * SCP-01 — Restrict Bedrock Model Access.
 *
 * Spec §2.2.2 L576-608. Denies Bedrock inference actions on any model
 * resource that is not on the platform allow-list. Applies to every account
 * under the OUs it is attached to.
 *
 * The allow-list is expressed on the RESOURCE (`NotResource`): the exact
 * foundation-model ARNs plus the matching inference-profile ARNs (any
 * account, any geo prefix), because a profile invocation is authorized
 * against the profile ARN as well as each backing foundation model.
 *
 * LIVE-FOUND DEFECT (2026-09-25, IAM Access Analyzer + SimulateCustomPolicy):
 *   The previous revision conditioned on `bedrock:FoundationModel`, which is
 *   NOT a Bedrock condition key (the Service Authorization Reference lists
 *   GuardrailIdentifier, InferenceProfileArn, ModelArn, PromptRouterArn, …).
 *   Under `ForAllValues:StringNotEquals` an absent key evaluates to TRUE, so
 *   the statement denied EVERY model — including the allow-listed ones — in
 *   any OU it was attached to. It also listed `bedrock:Converse` and
 *   `bedrock:ConverseStream`, which are not IAM actions (the Converse APIs
 *   authorize as `bedrock:InvokeModel` / `InvokeModelWithResponseStream`).
 *   The replacement shape was chosen on the live evaluators: Access Analyzer
 *   clean; unlisted model and unlisted-model profile denied; both listed
 *   models and a listed-model profile allowed.
 *
 * The allow-list is sourced from `@agenticai/platform-baselines`, which also
 * feeds Bedrock VPCE policies and execution-role IAM.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from "./index";

/** Bedrock inference actions subject to the model allow-list (all valid IAM actions). */
export const SCP01_MODEL_ACTIONS: readonly string[] = [
  "bedrock:InvokeModel",
  "bedrock:InvokeModelWithResponseStream",
  "bedrock:CreateModelInvocationJob",
];

const FOUNDATION_MODEL_ARN =
  /^arn:aws:bedrock:([a-z0-9-]+)::foundation-model\/([^*/]+)$/;

/**
 * Inference-profile ARN patterns that route to exactly the allow-listed
 * models: any account (system profiles are account-scoped), any geo prefix
 * (`us.`, `eu.`, `global.`, …) in front of the exact model id.
 */
export function allowedInferenceProfilePatterns(
  allowedModelArns: readonly string[],
): string[] {
  return allowedModelArns.map((arn) => {
    const match = FOUNDATION_MODEL_ARN.exec(arn);
    if (!match) {
      throw new Error(
        `SCP-01 allow-list entries must be exact foundation-model ARNs, got: ${arn}`,
      );
    }
    const [, region, modelId] = match;
    return `arn:aws:bedrock:${region}:*:inference-profile/*${modelId}`;
  });
}

export function scp01ModelAllowlist(
  allowedModelArns: readonly string[],
): ScpDefinition {
  if (allowedModelArns.length === 0) {
    throw new Error(
      "SCP-01 allow-list must not be empty; an empty allow-list denies all Bedrock inference.",
    );
  }
  const profilePatterns = allowedInferenceProfilePatterns(allowedModelArns);

  const body = {
    Version: "2012-10-17",
    Statement: [
      {
        Sid: "DenyNonAllowListedBedrockModels",
        Effect: "Deny",
        Action: Array.from(SCP01_MODEL_ACTIONS),
        NotResource: [...allowedModelArns, ...profilePatterns],
      },
    ],
  };

  return toScpDefinition(
    "scp-01",
    "AgenticAI-SCP-01-ModelAllowlist",
    "Deny Bedrock inference on any model resource not on the platform allow-list (spec §2.2.2).",
    body,
  );
}
