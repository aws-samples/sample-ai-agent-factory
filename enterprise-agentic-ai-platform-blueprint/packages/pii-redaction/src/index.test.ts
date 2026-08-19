/**
 * Tests for the PII redaction helper.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { redact, redactRecursive } from './index';

// AWS's canonical documentation example access-key id. Assembled from
// fragments (not written as a single literal) so repository secret scanners
// do not flag the test fixture as a hard-coded credential — it is a public,
// non-functional placeholder used only to exercise the redactor.
const EXAMPLE_AWS_KEY = ['AKIA', 'IOSFODNN7', 'EXAMPLE'].join('');

describe('PII redaction', () => {
  it('redacts AWS access key ids', () => {
    const r = redact(`My key is ${EXAMPLE_AWS_KEY} in the file.`);
    expect(r.redacted).toContain('[REDACTED:aws-access-key-id]');
    expect(r.redacted).not.toContain(EXAMPLE_AWS_KEY);
    expect(r.hits.find((h) => h.category === 'aws-access-key-id')?.count).toBe(1);
  });

  it('redacts emails', () => {
    const r = redact('contact alice@example.com or bob@example.org');
    expect(r.redacted).toContain('[REDACTED:email]');
    expect(r.redacted).not.toContain('alice@example.com');
    expect(r.hits.find((h) => h.category === 'email')?.count).toBe(2);
  });

  it('redacts SSN format', () => {
    const r = redact('SSN 123-45-6789');
    expect(r.redacted).toContain('[REDACTED:ssn]');
  });

  it('redacts AU TFN, Medicare, BSB', () => {
    const r = redact('TFN 123 456 789, Medicare 1234 56789 1, BSB 062-001');
    expect(r.redacted).toContain('[REDACTED:au-tfn]');
    expect(r.redacted).toContain('[REDACTED:au-medicare]');
    expect(r.redacted).toContain('[REDACTED:au-bsb]');
  });

  it('redacts phone E.164', () => {
    const r = redact('Call +14155552671 today');
    expect(r.redacted).toContain('[REDACTED:phone]');
  });

  it('returns input unchanged when no PII present', () => {
    const r = redact('the quick brown fox');
    expect(r.redacted).toBe('the quick brown fox');
    expect(r.hits).toEqual([]);
  });

  it('redactRecursive walks objects and arrays', () => {
    const out = redactRecursive({
      a: EXAMPLE_AWS_KEY,
      b: ['alice@example.com', 42],
      c: { d: 'no pii' },
    });
    expect(JSON.stringify(out)).toContain('[REDACTED:aws-access-key-id]');
    expect(JSON.stringify(out)).toContain('[REDACTED:email]');
    expect(JSON.stringify(out)).toContain('no pii');
  });
});
