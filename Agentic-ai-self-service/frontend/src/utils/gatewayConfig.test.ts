/**
 * Property-based tests for gateway configuration utilities.
 * Validates: Requirements 4.7
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { isValidLambdaArn } from './gatewayConfig';

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

// Valid AWS regions
const validRegions = [
  'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
  'eu-west-1', 'eu-west-2', 'eu-central-1',
  'ap-northeast-1', 'ap-southeast-1', 'ap-southeast-2',
  'sa-east-1', 'ca-central-1',
  'us-gov-west-1', 'us-gov-east-1',
];

const regionArb = fc.constantFrom(...validRegions);

// Valid 12-digit AWS account IDs
const accountIdArb = fc.stringMatching(/^\d{12}$/);

// Valid Lambda function names (1-64 chars, alphanumeric, hyphens, underscores)
const functionNameArb = fc.stringMatching(/^[a-zA-Z0-9_-]{1,64}$/);

// Optional qualifier (version or alias)
const qualifierArb = fc.oneof(
  fc.constant(''),
  fc.constant(':$LATEST'),
  fc.stringMatching(/^:[a-zA-Z0-9_-]+$/).filter((q) => q.length <= 65)
);

// Generator for valid Lambda ARNs
const validLambdaArnArb = fc.tuple(regionArb, accountIdArb, functionNameArb, qualifierArb)
  .map(([region, accountId, functionName, qualifier]) =>
    `arn:aws:lambda:${region}:${accountId}:function:${functionName}${qualifier}`
  );

// Generator for invalid ARNs (various malformed patterns)
const invalidArnArb = fc.oneof(
  // Empty or whitespace
  fc.constant(''),
  fc.constant('   '),
  // Missing parts
  fc.constant('arn:aws:lambda'),
  fc.constant('arn:aws:lambda:us-east-1'),
  fc.constant('arn:aws:lambda:us-east-1:123456789012'),
  fc.constant('arn:aws:lambda:us-east-1:123456789012:function'),
  // Wrong service
  fc.constant('arn:aws:s3:us-east-1:123456789012:function:my-function'),
  fc.constant('arn:aws:ec2:us-east-1:123456789012:function:my-function'),
  // Invalid account ID (not 12 digits)
  fc.constant('arn:aws:lambda:us-east-1:12345:function:my-function'),
  fc.constant('arn:aws:lambda:us-east-1:1234567890123:function:my-function'),
  fc.constant('arn:aws:lambda:us-east-1:abcdefghijkl:function:my-function'),
  // Invalid region format
  fc.constant('arn:aws:lambda:invalid:123456789012:function:my-function'),
  fc.constant('arn:aws:lambda:US-EAST-1:123456789012:function:my-function'),
  // Invalid function name
  fc.constant('arn:aws:lambda:us-east-1:123456789012:function:'),
  fc.constant('arn:aws:lambda:us-east-1:123456789012:function:my function'),
  fc.constant('arn:aws:lambda:us-east-1:123456789012:function:my.function'),
  // Random strings
  fc.string({ minLength: 1, maxLength: 100 }).filter((s) => !s.startsWith('arn:aws:lambda:')),
);

// ============================================================================
// Property 17: Lambda ARN Format Validation
// ============================================================================

describe('Property 17: Lambda ARN Format Validation', () => {
  /**
   * **Validates: Requirements 4.7**
   *
   * For any Lambda ARN input, the validation shall verify the ARN matches
   * the pattern `arn:aws:lambda:<region>:<account>:function:<name>` and
   * display an error for invalid formats.
   */
  it('accepts valid Lambda ARNs', () => {
    fc.assert(
      fc.property(validLambdaArnArb, (arn) => {
        expect(isValidLambdaArn(arn)).toBe(true);
      }),
      { numRuns: 100 }
    );
  });

  it('rejects invalid ARNs', () => {
    fc.assert(
      fc.property(invalidArnArb, (arn) => {
        expect(isValidLambdaArn(arn)).toBe(false);
      }),
      { numRuns: 100 }
    );
  });

  it('validates region format correctly', () => {
    // Valid regions should pass
    for (const region of validRegions) {
      const arn = `arn:aws:lambda:${region}:123456789012:function:my-function`;
      expect(isValidLambdaArn(arn)).toBe(true);
    }

    // Invalid regions should fail
    const invalidRegions = ['invalid', 'US-EAST-1', 'us_east_1', '123', 'us-east'];
    for (const region of invalidRegions) {
      const arn = `arn:aws:lambda:${region}:123456789012:function:my-function`;
      expect(isValidLambdaArn(arn)).toBe(false);
    }
  });

  it('validates account ID format correctly', () => {
    fc.assert(
      fc.property(accountIdArb, (accountId) => {
        const arn = `arn:aws:lambda:us-east-1:${accountId}:function:my-function`;
        expect(isValidLambdaArn(arn)).toBe(true);
      }),
      { numRuns: 100 }
    );

    // Invalid account IDs
    const invalidAccountIds = ['12345', '1234567890123', 'abcdefghijkl', '12345678901a'];
    for (const accountId of invalidAccountIds) {
      const arn = `arn:aws:lambda:us-east-1:${accountId}:function:my-function`;
      expect(isValidLambdaArn(arn)).toBe(false);
    }
  });

  it('validates function name format correctly', () => {
    fc.assert(
      fc.property(functionNameArb, (functionName) => {
        const arn = `arn:aws:lambda:us-east-1:123456789012:function:${functionName}`;
        expect(isValidLambdaArn(arn)).toBe(true);
      }),
      { numRuns: 100 }
    );

    // Invalid function names
    const invalidNames = ['', 'my function', 'my.function', 'my@function'];
    for (const name of invalidNames) {
      const arn = `arn:aws:lambda:us-east-1:123456789012:function:${name}`;
      expect(isValidLambdaArn(arn)).toBe(false);
    }
  });

  it('handles null and undefined inputs', () => {
    expect(isValidLambdaArn(null as unknown as string)).toBe(false);
    expect(isValidLambdaArn(undefined as unknown as string)).toBe(false);
  });

  it('handles non-string inputs', () => {
    expect(isValidLambdaArn(123 as unknown as string)).toBe(false);
    expect(isValidLambdaArn({} as unknown as string)).toBe(false);
    expect(isValidLambdaArn([] as unknown as string)).toBe(false);
  });
});
