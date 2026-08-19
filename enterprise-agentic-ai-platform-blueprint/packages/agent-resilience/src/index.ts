/**
 * @agenticai/agent-resilience
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Partial-4 + Partial-5.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export {
  DEFAULT_RETRY_POLICY,
  DEFAULT_CIRCUIT_THRESHOLDS,
  defaultFallbackChain,
  validateFallbackChain,
  nextRetryDelayMs,
  type RetryPolicy,
  type CircuitBreakerThresholds,
  type FallbackChain,
} from './circuit-breaker';
export {
  KillSwitchConstruct,
  type KillSwitchConstructProps,
} from './kill-switch-construct';
export {
  InferenceCircuitBreakerConstruct,
  INFERENCE_CB_METRIC_NAMESPACE,
  type InferenceCircuitBreakerConstructProps,
} from './inference-circuit-breaker-construct';
