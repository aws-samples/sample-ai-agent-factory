/**
 * @agenticai/agentcore-evaluations
 *
 * Amazon Bedrock AgentCore Evaluations building blocks shared by both
 * production-evaluation options (ADR-17):
 *   - option A, CloudWatch Omni: the hub space runs online evaluations over
 *     centralized spans and shows them in the Omni web UI;
 *   - option B, AgentCore native: each cell deploys an online evaluation
 *     configuration and views results in the CloudWatch GenAI Observability
 *     console (cross-account through the existing OAM links).
 *
 * Both options use the same evaluator catalog, the same execution-role shape,
 * the same code-based policy evaluator and the same result contract
 * (OpenTelemetry GenAI evaluation events + CloudWatch metrics).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export {
  BUILTIN_EVALUATORS,
  DEFAULT_PRODUCTION_EVALUATORS,
  EVALUATOR_ID_PATTERN,
  EVALUATION_RESOURCE_NAME_PATTERN,
  assertEvaluatorId,
  assertEvaluationResourceName,
  evaluationResourceName,
  type BuiltinEvaluatorId,
} from './evaluator-catalog';
export {
  EvaluationExecutionRoleConstruct,
  type EvaluationExecutionRoleConstructProps,
} from './evaluation-execution-role';
export {
  PolicyCodeEvaluatorConstruct,
  type PolicyCodeEvaluatorConstructProps,
} from './policy-code-evaluator';
export {
  AgentCoreOnlineEvaluationConstruct,
  type AgentCoreOnlineEvaluationConstructProps,
  type OnlineEvaluationFilter,
  type OnlineEvaluationScoreAlarm,
} from './online-evaluation-config';
