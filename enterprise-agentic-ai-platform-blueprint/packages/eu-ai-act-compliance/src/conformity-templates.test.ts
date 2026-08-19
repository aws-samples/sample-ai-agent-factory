/**
 * Tests for conformity Markdown template generators.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  technicalDocumentation,
  riskAssessment,
  humanOversightProtocol,
  type ConformityInputs,
} from './conformity-templates';

const inputs: ConformityInputs = {
  tenantId: 'demo',
  agentId: 'primary',
  envName: 'prod',
  blueprintId: 'multi-agent',
  riskClass: 'high',
  providerName: 'AWS Solutions',
  contactEmail: 'compliance@example.com',
  modelIds: ['anthropic.claude-sonnet-4-5-20250929-v1:0'],
  humanOversightContact: 'oversight@example.com',
  emittedAt: '2026-05-15T00:00:00Z',
  platformVersion: 'v0.4.0',
};

describe('conformity templates', () => {
  it('technical documentation references Article 11 + Annex IV', () => {
    const md = technicalDocumentation(inputs);
    expect(md).toContain('Article 11');
    expect(md).toContain('Annex IV');
    expect(md).toContain('demo');
    expect(md).toContain('multi-agent');
  });

  it('high-risk template lists Articles 9-17', () => {
    const md = technicalDocumentation(inputs);
    for (const a of ['9', '10', '11', '12', '13', '14', '15', '16', '17']) {
      expect(md).toContain(`Article ${a}`);
    }
  });

  it('risk assessment includes hallucination + prompt-injection mitigations', () => {
    const md = riskAssessment(inputs);
    expect(md).toMatch(/Hallucination/i);
    expect(md).toMatch(/Prompt injection/i);
    expect(md).toMatch(/Cost runaway/i);
  });

  it('human-oversight protocol cites Article 14 and SQS escalation', () => {
    const md = humanOversightProtocol(inputs);
    expect(md).toContain('Article 14');
    expect(md).toMatch(/SQS|Step Function/);
    expect(md).toContain('oversight@example.com');
  });

  it('all templates are markdown headings', () => {
    for (const md of [technicalDocumentation(inputs), riskAssessment(inputs), humanOversightProtocol(inputs)]) {
      expect(md.startsWith('# ')).toBe(true);
    }
  });
});
