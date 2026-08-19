/**
 * Unit tests for @agenticai/platform-baselines — guards the single source of
 * truth used by SCPs, VPCE policies, LiteLLM config, and IAM resource scopes.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  PLATFORM_ALLOWED_MODELS,
  PLATFORM_APPROVED_REGIONS,
  GUARDRAIL_PROFILES,
  allowedModelArns,
} from './index';

describe('PLATFORM_ALLOWED_MODELS', () => {
  it('contains exactly Claude Sonnet 4.5 and Claude Haiku 4.5 per spec §2.2.2 L599-600', () => {
    expect(PLATFORM_ALLOWED_MODELS).toEqual([
      'anthropic.claude-sonnet-4-5-20250929-v1:0',
      'anthropic.claude-haiku-4-5-20251001-v1:0',
    ]);
  });

  it('is non-empty (SCP-01 cannot allow zero models)', () => {
    expect(PLATFORM_ALLOWED_MODELS.length).toBeGreaterThan(0);
  });

  it('contains only anthropic-prefixed model identifiers (v1 scope)', () => {
    for (const id of PLATFORM_ALLOWED_MODELS) {
      expect(id).toMatch(/^anthropic\./);
    }
  });
});

describe('allowedModelArns', () => {
  it('prefixes every model with the Bedrock foundation-model ARN scheme for the given region', () => {
    const arns = allowedModelArns('us-west-2');
    expect(arns).toEqual([
      'arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0',
      'arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0',
    ]);
  });

  it('uses the empty account segment per spec §2.2.2 example', () => {
    const arns = allowedModelArns('us-east-1');
    for (const arn of arns) {
      expect(arn).toMatch(/^arn:aws:bedrock:[^:]+::foundation-model\//);
    }
  });
});

describe('PLATFORM_APPROVED_REGIONS', () => {
  it('includes us-west-2 as the baseline per DECISIONS.md Q-REF-DEPLOY', () => {
    expect(PLATFORM_APPROVED_REGIONS).toContain('us-west-2');
  });

  it('includes us-east-1 for global-from-us-east-1 service access', () => {
    expect(PLATFORM_APPROVED_REGIONS).toContain('us-east-1');
  });

  it('is non-empty (SCP-06 cannot allow zero regions)', () => {
    expect(PLATFORM_APPROVED_REGIONS.length).toBeGreaterThan(0);
  });
});

describe('GUARDRAIL_PROFILES', () => {
  it('defines exactly the three profiles required by spec §2.4.4 L1786-1792', () => {
    expect(Object.keys(GUARDRAIL_PROFILES).sort()).toEqual([
      'baseline',
      'customer-facing',
      'internal-tool',
    ]);
  });

  it('maps each profile to a resource name with the agenticai- prefix', () => {
    for (const id of Object.values(GUARDRAIL_PROFILES)) {
      expect(id).toMatch(/^agenticai-guardrail-/);
    }
  });
});
