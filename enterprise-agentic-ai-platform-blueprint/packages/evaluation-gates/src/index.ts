/**
 * @agenticai/evaluation-gates
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Partial-1: evaluation-based CI/CD gates.
 *
 * Exports:
 *   - `EvaluationGatesConstruct` — S3 corpus bucket (Object Lock GOVERNANCE 90d
 *     + KMS + versioning), DDB run-history table, judge-runner role, SNS topic.
 *   - Scoring constants used by both offline (CodeBuildStep) and online
 *     (Phase B watchdog Lambda) paths so the two never drift.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export {
  EvaluationGatesConstruct,
  type EvaluationGatesConstructProps,
} from './evaluation-gates-construct';
export {
  DEFAULT_EVAL_THRESHOLDS,
  JUDGE_MODELS,
  validateThresholds,
  type EvalThresholds,
  type JudgeCategory,
} from './scoring';
export {
  buildAgentManifest,
  type AgentManifest,
  type AgentManifestInput,
} from './agent-manifest';
