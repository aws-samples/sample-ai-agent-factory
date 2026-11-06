/**
 * Evaluator catalog identifiers for Amazon Bedrock AgentCore Evaluations.
 *
 * The same catalog backs both evaluation options in this blueprint:
 *   - CloudWatch Omni online/on-demand evaluations (ADR-17 option A), and
 *   - AgentCore Evaluations native online configurations (ADR-17 option B).
 *
 * Identifier format is fixed by the service:
 *   - `Builtin.<Name>` for AWS built-in evaluators (service-managed judge),
 *   - `ThirdParty.<Library>.<Metric>` for DeepEval/AutoEval metrics, and
 *   - `<name>-<10 chars>` for custom evaluators you create.
 *
 * Source: AWS::BedrockAgentCore::OnlineEvaluationConfig EvaluatorReference
 * (CloudFormation reference) and the AWS CDK alpha BuiltinEvaluator catalog.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

/** Built-in evaluators, by evaluation level. Judge runs on service capacity. */
export const BUILTIN_EVALUATORS = {
  /** Trace level. Factual accuracy of the response. */
  correctness: 'Builtin.Correctness',
  /** Trace level. Response is supported by retrieved context. */
  faithfulness: 'Builtin.Faithfulness',
  /** Trace level. Usefulness of the response from the user's perspective. */
  helpfulness: 'Builtin.Helpfulness',
  /** Trace level. Response addresses the user's query. */
  responseRelevance: 'Builtin.ResponseRelevance',
  /** Trace level. Brevity without dropping key information. */
  conciseness: 'Builtin.Conciseness',
  /** Trace level. Logical structure of the response. */
  coherence: 'Builtin.Coherence',
  /** Trace level. Adherence to the system instructions. */
  instructionFollowing: 'Builtin.InstructionFollowing',
  /** Trace level. Detects refusals and evasions. */
  refusal: 'Builtin.Refusal',
  /** Trace level. Harmful content detection. */
  harmfulness: 'Builtin.Harmfulness',
  /** Trace level. Generalisations about individuals or groups. */
  stereotyping: 'Builtin.Stereotyping',
  /** Session level. Whether the conversation met the user's goals. */
  goalSuccessRate: 'Builtin.GoalSuccessRate',
  /** Tool-call level. The agent chose the right tool. */
  toolSelectionAccuracy: 'Builtin.ToolSelectionAccuracy',
  /** Tool-call level. The agent extracted tool parameters accurately. */
  toolParameterAccuracy: 'Builtin.ToolParameterAccuracy',
  /** Tool-call level. The SKILL.md the agent loaded fits the task (one result per skill invocation). */
  skillSelectionAccuracy: 'Builtin.SkillSelectionAccuracy',
  /** Tool-call level. How fully the agent followed the loaded skill's steps (judged on the full session). */
  skillInstructionFollowing: 'Builtin.SkillInstructionFollowing',
} as const;

/**
 * The service's 17 built-in evaluators are the 15 ids above plus the two
 * ground-truth variants (Correctness and Goal success rate "with ground
 * truth"), which reuse `Builtin.Correctness` / `Builtin.GoalSuccessRate` fed
 * with `evaluationReferenceInputs` and are therefore not separate entries.
 */

export type BuiltinEvaluatorId =
  (typeof BUILTIN_EVALUATORS)[keyof typeof BUILTIN_EVALUATORS];

/**
 * The default production baseline for a blueprint cell. Every entry runs on
 * service-managed judge capacity, so this set needs no `bedrock:InvokeModel`
 * grant and does not cross the D-01 inference boundary (ADR-16).
 */
export const DEFAULT_PRODUCTION_EVALUATORS: readonly BuiltinEvaluatorId[] = [
  BUILTIN_EVALUATORS.correctness,
  BUILTIN_EVALUATORS.helpfulness,
  BUILTIN_EVALUATORS.instructionFollowing,
  BUILTIN_EVALUATORS.refusal,
  BUILTIN_EVALUATORS.harmfulness,
  BUILTIN_EVALUATORS.toolSelectionAccuracy,
];

/** Pattern the service enforces on `EvaluatorReference.EvaluatorId`. */
export const EVALUATOR_ID_PATTERN =
  /^(Builtin\.[a-zA-Z0-9._-]+|ThirdParty\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+|[a-zA-Z][a-zA-Z0-9-_]{0,99}-[a-zA-Z0-9]{10})$/;

/** Pattern the service enforces on evaluator and configuration names. */
export const EVALUATION_RESOURCE_NAME_PATTERN = /^[a-zA-Z][a-zA-Z0-9_]{0,47}$/;

export function assertEvaluatorId(id: string): void {
  if (!EVALUATOR_ID_PATTERN.test(id)) {
    throw new Error(
      `AgentCore evaluator id '${id}' is invalid. Expected 'Builtin.<Name>', 'ThirdParty.<Library>.<Metric>', or a custom id '<name>-<10 chars>'.`,
    );
  }
}

export function assertEvaluationResourceName(kind: string, name: string): void {
  if (!EVALUATION_RESOURCE_NAME_PATTERN.test(name)) {
    throw new Error(
      `AgentCore ${kind} name '${name}' is invalid. It must start with a letter and contain only letters, digits and underscores, at most 48 characters.`,
    );
  }
}

/**
 * Build a service-compliant resource name from the blueprint's identity
 * dimensions. Hyphens are not permitted in evaluation resource names, so the
 * allocation dimensions are joined with underscores.
 */
export function evaluationResourceName(
  prefix: string,
  envName: string,
  tenantId: string,
  agentId: string,
): string {
  const raw = `${prefix}_${envName}_${tenantId}_${agentId}`.replace(/[^a-zA-Z0-9_]/g, '_');
  const name = raw.length > 48 ? raw.slice(0, 48) : raw;
  assertEvaluationResourceName('resource', name);
  return name;
}
