/**
 * Tests for federated-mesh domain scoping helpers.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  DEFAULT_DOMAIN_IDS,
  buildDomainScopedSk,
  buildHomeDomainCondition,
  validateDomainId,
} from './domain-scoping';

describe('federated-mesh domain scoping', () => {
  it('declares the four default domain ids', () => {
    expect(DEFAULT_DOMAIN_IDS).toEqual(['sales', 'finance', 'legal', 'platform']);
  });

  it('rejects non-lowercase or short ids', () => {
    expect(() => validateDomainId('Sales')).toThrow();
    expect(() => validateDomainId('ab')).toThrow();
    expect(() => validateDomainId('a-very-long-domain-id-that-exceeds-thirty-two-chars')).toThrow();
  });

  it('builds a stable sort-key shape', () => {
    expect(buildDomainScopedSk('finance', 'invoice-bot')).toBe('domain#finance#agent#invoice-bot');
  });

  it('rejects malformed agentId', () => {
    expect(() => buildDomainScopedSk('finance', 'Bot')).toThrow();
  });

  it('builds the SCP home-domain condition', () => {
    const cond = buildHomeDomainCondition('legal');
    expect(cond.StringEquals['aws:PrincipalTag/domainId']).toBe('legal');
  });
});
