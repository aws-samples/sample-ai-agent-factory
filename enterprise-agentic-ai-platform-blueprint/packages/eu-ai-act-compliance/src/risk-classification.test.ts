/**
 * Tests for the EU AI Act risk classification SSOT.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  BLUEPRINT_RISK_CLASS,
  RISK_CLASS_ARTICLES,
  riskClassForBlueprint,
  validateRiskClass,
} from './risk-classification';

describe('EU AI Act risk classification SSOT', () => {
  it('declares the four risk classes from Article 6', () => {
    expect(Object.keys(RISK_CLASS_ARTICLES).sort()).toEqual([
      'high',
      'limited',
      'minimal',
      'unacceptable',
    ]);
  });

  it('chatbot + task default to limited per .claude/GAP_CLOSURE_PLAN.md §16-LOCKED', () => {
    expect(BLUEPRINT_RISK_CLASS.chatbot).toBe('limited');
    expect(BLUEPRINT_RISK_CLASS.task).toBe('limited');
  });

  it('multi-agent defaults to high (chain-of-effect risk)', () => {
    expect(BLUEPRINT_RISK_CLASS['multi-agent']).toBe('high');
  });

  it('high risk covers Articles 9-17 (Annex III obligations)', () => {
    const arts = RISK_CLASS_ARTICLES.high;
    for (const required of ['9', '10', '11', '12', '13', '14', '15', '16', '17']) {
      expect(arts).toContain(required);
    }
  });

  it('riskClassForBlueprint throws on unknown ids', () => {
    expect(() => riskClassForBlueprint('not-a-blueprint')).toThrow(/Unknown blueprint/);
  });

  it('validateRiskClass round-trips on valid values', () => {
    for (const v of ['unacceptable', 'high', 'limited', 'minimal'] as const) {
      expect(validateRiskClass(v)).toBe(v);
    }
  });

  it('validateRiskClass rejects garbage', () => {
    expect(() => validateRiskClass('extreme')).toThrow();
  });
});
