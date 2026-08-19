/**
 * @agenticai/agent-lifecycle — canary configuration SSOT.
 *
 * Per .claude/GAP_CLOSURE_PLAN.md §16-LOCKED: default canary 5%,
 * configurable per-tenant via context `agenticai/canaryPercent`.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export interface CanaryConfig {
  readonly canaryPercent: number;       // [1, 50]
  readonly soakDuration: number;        // minutes
  readonly autoPromote: boolean;        // true ⇒ Step Function flips alias on green
  readonly tolerancePctPoints: number;  // regression tolerance (matches Phase B)
}

export const DEFAULT_CANARY_CONFIG: CanaryConfig = {
  canaryPercent: 5,
  soakDuration: 30,
  autoPromote: true,
  tolerancePctPoints: 10,
};

export function validateCanaryConfig(c: CanaryConfig): void {
  if (c.canaryPercent < 1 || c.canaryPercent > 50) {
    throw new Error(`canaryPercent must be in [1, 50]; got ${c.canaryPercent}`);
  }
  if (c.soakDuration < 5 || c.soakDuration > 24 * 60) {
    throw new Error(`soakDuration minutes must be in [5, 1440]; got ${c.soakDuration}`);
  }
  if (c.tolerancePctPoints < 0 || c.tolerancePctPoints > 100) {
    throw new Error(`tolerancePctPoints must be in [0, 100]; got ${c.tolerancePctPoints}`);
  }
}
