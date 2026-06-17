/**
 * Identity configuration utilities.
 * Requirements: 5.1, 5.2
 */

import type { IdentityConfiguration } from '../types/components';

// ============================================================================
// Credential Validation
// ============================================================================

export interface CredentialValidationResult {
  isValid: boolean;
  error?: string;
}

/**
 * Validate credential format based on type.
 */
export function validateCredentialFormat(
  value: string,
  type: 'client_id' | 'secret_ref' | 'api_key'
): CredentialValidationResult {
  if (!value || value.trim().length === 0) {
    return { isValid: false, error: 'Value is required' };
  }

  switch (type) {
    case 'client_id':
      // Client IDs are typically alphanumeric with some special chars
      if (value.length < 1 || value.length > 256) {
        return { isValid: false, error: 'Client ID must be 1-256 characters' };
      }
      return { isValid: true };

    case 'secret_ref':
      // Secret references should follow Secrets Manager naming
      if (!/^[a-zA-Z0-9/_+=.@-]+$/.test(value)) {
        return { isValid: false, error: 'Invalid secret reference format' };
      }
      return { isValid: true };

    case 'api_key':
      if (value.length < 1) {
        return { isValid: false, error: 'API key is required' };
      }
      return { isValid: true };

    default:
      return { isValid: true };
  }
}

// ============================================================================
// Default Configuration
// ============================================================================

export function createDefaultIdentityConfig(): IdentityConfiguration {
  return {
    name: '',
    credentialType: 'api_key',
    apiKeyConfig: {
      keyName: '',
      keyValueRef: '',
      headerName: 'X-API-Key',
    },
  };
}
