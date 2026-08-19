/**
 * Circuit-breaker + retry/fallback configuration SSOT.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Partial-5.
 *
 * Pure values + a tiny pure function `nextRetryDelayMs()` so the LiteLLM
 * pre-warmer + custom-resource Lambdas can use the same backoff curve as
 * the runtime path without re-implementing it.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { PLATFORM_ALLOWED_MODELS } from '@agenticai/platform-baselines';

export interface RetryPolicy {
  /** Number of attempts including the first call. */
  readonly maxAttempts: number;
  /** Backoff schedule (ms) — element 0 is between attempt 1 and 2, etc. */
  readonly backoffMs: readonly number[];
  /** HTTP statuses that trigger a retry (NOT 4xx other than 429). */
  readonly retryableStatuses: readonly number[];
}

export interface CircuitBreakerThresholds {
  /** 5xx rate (%) over `windowMinutes` that opens the circuit. */
  readonly errorRateMaxPct: number;
  /** Rolling window for the error rate. */
  readonly windowMinutes: number;
  /** p99 latency (ms) ceiling — also opens the circuit when exceeded. */
  readonly p99LatencyMaxMs: number;
  /** Consecutive 5xx that triggers immediate fallback. */
  readonly consecutive5xxToFallback: number;
}

export const DEFAULT_RETRY_POLICY: RetryPolicy = {
  maxAttempts: 3,
  backoffMs: [200, 800, 3200],
  retryableStatuses: [429, 502, 503],
};

export const DEFAULT_CIRCUIT_THRESHOLDS: CircuitBreakerThresholds = {
  errorRateMaxPct: 5,
  windowMinutes: 5,
  p99LatencyMaxMs: 8000,
  consecutive5xxToFallback: 5,
};

/** Return the delay before retry attempt N (1-indexed); -1 if no more retries. */
export function nextRetryDelayMs(attemptIndex: number, policy: RetryPolicy = DEFAULT_RETRY_POLICY): number {
  if (attemptIndex < 1 || attemptIndex >= policy.maxAttempts) return -1;
  return policy.backoffMs[attemptIndex - 1] ?? policy.backoffMs[policy.backoffMs.length - 1];
}

/**
 * Default fallback chain. Both entries MUST come from the SSOT
 * allow-list (`PLATFORM_ALLOWED_MODELS`) — no SCP exceptions allowed.
 */
export interface FallbackChain {
  readonly primary: string;
  readonly secondary: string;
}

export function defaultFallbackChain(): FallbackChain {
  const sonnet = PLATFORM_ALLOWED_MODELS.find((m) => m.includes('claude-sonnet-4-5'));
  const haiku = PLATFORM_ALLOWED_MODELS.find((m) => m.includes('claude-haiku-4-5'));
  if (!sonnet || !haiku) {
    throw new Error(
      'Cannot build default fallback chain — Sonnet 4.5 + Haiku 4.5 must both be on PLATFORM_ALLOWED_MODELS.',
    );
  }
  return { primary: sonnet, secondary: haiku };
}

export function validateFallbackChain(chain: FallbackChain): void {
  const allowedIds = new Set<string>(PLATFORM_ALLOWED_MODELS);
  if (!allowedIds.has(chain.primary)) {
    throw new Error(`fallbackChain.primary not on allow-list: ${chain.primary}`);
  }
  if (!allowedIds.has(chain.secondary)) {
    throw new Error(`fallbackChain.secondary not on allow-list: ${chain.secondary}`);
  }
  if (chain.primary === chain.secondary) {
    throw new Error('fallbackChain.primary and .secondary must differ');
  }
}
