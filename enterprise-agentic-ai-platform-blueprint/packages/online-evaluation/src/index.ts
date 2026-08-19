/**
 * @agenticai/online-evaluation
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-1 (continuous evaluation /
 * online monitoring). Samples production prompt/response pairs, scores via
 * judge models, alarms on regression vs. golden baseline.
 *
 * Same scoring SSOT as the offline gate (`@agenticai/evaluation-gates/scoring`)
 * — drift between offline thresholds and online alarms is impossible by
 * construction.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export {
  OnlineEvaluationConstruct,
  type OnlineEvaluationConstructProps,
} from './online-evaluation-construct';
export {
  computeRegression,
  type ScoreSample,
  type RegressionResult,
} from './regression';
