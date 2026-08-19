/**
 * @agenticai/pii-redaction
 *
 * Defence-in-depth PII filter applied BEFORE memory writes, log emission,
 * and prompt rendering. Bedrock Guardrails handle the prompt + completion
 * surface; this package handles every other surface (memory, logs, audit
 * tables, eval corpus).
 *
 * Pure-fn module. No CDK construct — callers wire it into Lambda handlers
 * via `redact(input)`. The patterns are conservative (no unsupervised ML);
 * they err toward over-redaction.
 *
 * Bonus shippable component (Z7-L). Closes a security gap that the gap
 * analysis didn't enumerate but the bug-bash agent flagged in passing
 * (workload root could write arbitrary objects to AI Act bucket).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export type PiiCategory =
  | 'aws-access-key-id'
  | 'aws-secret-access-key'
  | 'credit-card'
  | 'email'
  | 'phone-e164'
  | 'ssn'
  | 'au-tfn'
  | 'au-medicare'
  | 'au-bsb';

const PATTERNS: ReadonlyArray<{ category: PiiCategory; re: RegExp; replacement: string }> = [
  // AWS access key ID — AKIA + 16 base32 chars
  { category: 'aws-access-key-id', re: /\bAKIA[0-9A-Z]{16}\b/g, replacement: '[REDACTED:aws-access-key-id]' },
  // AWS secret access key — 40 base64 chars after `aws_secret` or `secret`
  { category: 'aws-secret-access-key', re: /\b[A-Za-z0-9+/]{40}\b/g, replacement: '[REDACTED:aws-secret]' },
  // Credit card (loose; Luhn-check is for callers who care)
  { category: 'credit-card', re: /\b(?:\d[ -]*?){13,16}\b/g, replacement: '[REDACTED:credit-card]' },
  // Email
  { category: 'email', re: /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g, replacement: '[REDACTED:email]' },
  // Phone E.164
  { category: 'phone-e164', re: /\+\d{8,15}\b/g, replacement: '[REDACTED:phone]' },
  // US SSN
  { category: 'ssn', re: /\b\d{3}-\d{2}-\d{4}\b/g, replacement: '[REDACTED:ssn]' },
  // Australia TFN (8–9 digits, optional spacing)
  { category: 'au-tfn', re: /\b\d{3}\s?\d{3}\s?\d{2,3}\b/g, replacement: '[REDACTED:au-tfn]' },
  // Australia Medicare
  { category: 'au-medicare', re: /\b\d{4}\s?\d{5}\s?\d\b/g, replacement: '[REDACTED:au-medicare]' },
  // Australia BSB
  { category: 'au-bsb', re: /\b\d{3}-\d{3}\b/g, replacement: '[REDACTED:au-bsb]' },
];

export interface RedactionResult {
  readonly redacted: string;
  readonly hits: ReadonlyArray<{ category: PiiCategory; count: number }>;
}

export function redact(input: string): RedactionResult {
  let s = input;
  const hitMap = new Map<PiiCategory, number>();
  for (const p of PATTERNS) {
    const matches = s.match(p.re) || [];
    if (matches.length) {
      hitMap.set(p.category, (hitMap.get(p.category) || 0) + matches.length);
      s = s.replace(p.re, p.replacement);
    }
  }
  return {
    redacted: s,
    hits: [...hitMap.entries()].map(([category, count]) => ({ category, count })),
  };
}

export function redactRecursive(value: unknown): unknown {
  if (typeof value === 'string') return redact(value).redacted;
  if (Array.isArray(value)) return value.map((v) => redactRecursive(v));
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = redactRecursive(v);
    }
    return out;
  }
  return value;
}
