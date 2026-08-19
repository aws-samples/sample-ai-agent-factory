/**
 * @agenticai/eu-ai-act-compliance — risk classification SSOT.
 *
 * Maps each blueprint pattern to its EU AI Act Article 6 risk class. Defaults
 * are a locked platform decision:
 *   - chatbot:     limited
 *   - task:        limited
 *   - multi-agent: high   (multi-agent systems can chain effects;
 *                          per Annex III/Article 6 §2 they default to high
 *                          unless conformity assessment proves otherwise)
 *
 * Risk classes per the Act:
 *   - unacceptable : prohibited (Article 5)
 *   - high         : Annex III + conformity assessment required
 *   - limited      : transparency obligations only
 *   - minimal      : voluntary codes of conduct
 *
 * Customers override via construct prop or context.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export type RiskClass = 'unacceptable' | 'high' | 'limited' | 'minimal';

/** Articles each risk class triggers (concise reference list). */
export const RISK_CLASS_ARTICLES: Readonly<Record<RiskClass, readonly string[]>> = {
  unacceptable: ['5'],
  high: ['9', '10', '11', '12', '13', '14', '15', '16', '17'],
  limited: ['50', '52'],
  minimal: ['95'],
};

/** Default per-blueprint mapping. */
export const BLUEPRINT_RISK_CLASS: Readonly<Record<string, RiskClass>> = {
  chatbot: 'limited',
  task: 'limited',
  'multi-agent': 'high',
};

const ALL_RISKS: ReadonlySet<RiskClass> = new Set(['unacceptable', 'high', 'limited', 'minimal']);

export function validateRiskClass(value: string): RiskClass {
  if (!ALL_RISKS.has(value as RiskClass)) {
    throw new Error(`Invalid EU AI Act risk class '${value}'; expected one of: ${[...ALL_RISKS].join(', ')}`);
  }
  return value as RiskClass;
}

/**
 * Map a CDK pattern id to its risk class. Throws on unknown ids — callers
 * should pass the construct id surface (`chatbot` / `task` / `multi-agent`)
 * or extend `BLUEPRINT_RISK_CLASS` first.
 */
export function riskClassForBlueprint(blueprintId: string): RiskClass {
  const c = BLUEPRINT_RISK_CLASS[blueprintId];
  if (!c) {
    throw new Error(
      `Unknown blueprint '${blueprintId}'. Add to BLUEPRINT_RISK_CLASS first. Known: ${Object.keys(BLUEPRINT_RISK_CLASS).join(', ')}`,
    );
  }
  return c;
}
