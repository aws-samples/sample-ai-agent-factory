/**
 * Property-based tests for identity configuration utilities.
 * Validates: Requirements 5.6
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { validateCredentialFormat, createDefaultIdentityConfig } from './identityConfig';

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

// Valid client IDs (1-256 chars, must have at least one non-whitespace)
const validClientIdArb = fc.stringMatching(/^[^\s][\s\S]{0,255}$/);

// Valid secret references (simple names or ARNs)
const validSecretRefArb = fc.stringMatching(/^[a-zA-Z0-9/_+=.@-]{1,512}$/);

// Invalid credentials
const emptyOrWhitespaceArb = fc.constantFrom('', '   ', '\t', '\n');

// ============================================================================
// Property 18: Credential Format Validation
// ============================================================================

describe('Property 18: Credential Format Validation', () => {
  describe('Client ID Validation', () => {
    it('accepts valid client IDs', () => {
      fc.assert(
        fc.property(validClientIdArb, (clientId) => {
          const result = validateCredentialFormat(clientId, 'client_id');
          expect(result.isValid).toBe(true);
        }),
        { numRuns: 100 }
      );
    });

    it('rejects empty client IDs', () => {
      fc.assert(
        fc.property(emptyOrWhitespaceArb, (clientId) => {
          const result = validateCredentialFormat(clientId, 'client_id');
          expect(result.isValid).toBe(false);
        }),
        { numRuns: 10 }
      );
    });
  });

  describe('Secret Reference Validation', () => {
    it('accepts valid secret references', () => {
      fc.assert(
        fc.property(validSecretRefArb, (secretRef) => {
          const result = validateCredentialFormat(secretRef, 'secret_ref');
          expect(result.isValid).toBe(true);
        }),
        { numRuns: 100 }
      );
    });

    it('rejects empty secret references', () => {
      fc.assert(
        fc.property(emptyOrWhitespaceArb, (secretRef) => {
          const result = validateCredentialFormat(secretRef, 'secret_ref');
          expect(result.isValid).toBe(false);
        }),
        { numRuns: 10 }
      );
    });
  });

  describe('API Key Validation', () => {
    it('accepts valid API keys', () => {
      fc.assert(
        // Use stringMatching to ensure non-whitespace characters
        fc.property(fc.stringMatching(/^[^\s].*$/), (apiKey) => {
          const result = validateCredentialFormat(apiKey, 'api_key');
          expect(result.isValid).toBe(true);
        }),
        { numRuns: 100 }
      );
    });

    it('rejects empty API keys', () => {
      fc.assert(
        fc.property(emptyOrWhitespaceArb, (apiKey) => {
          const result = validateCredentialFormat(apiKey, 'api_key');
          expect(result.isValid).toBe(false);
        }),
        { numRuns: 10 }
      );
    });
  });
});

// ============================================================================
// Default Configuration Tests
// ============================================================================

describe('createDefaultIdentityConfig', () => {
  it('creates a valid default configuration', () => {
    const config = createDefaultIdentityConfig();

    expect(config.name).toBe('');
    expect(config.credentialType).toBe('api_key');
    expect(config.apiKeyConfig).toBeDefined();
    expect(config.apiKeyConfig?.headerName).toBe('X-API-Key');
  });
});
