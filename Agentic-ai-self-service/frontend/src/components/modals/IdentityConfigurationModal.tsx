/**
 * IdentityConfiguration modal for configuring AgentCore Identity components.
 * Requirements: 5.1, 5.2
 */

import { useState, useCallback, useMemo, useEffect } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, SelectField, FormSection } from './FormFields';
import type { IdentityConfiguration, OAuth2Provider } from '../../types/components';
import { createDefaultIdentityConfig } from '../../utils/identityConfig';

// ============================================================================
// Props Interface
// ============================================================================

export interface IdentityConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: IdentityConfiguration) => void;
  initialConfig?: Partial<IdentityConfiguration>;
}

// ============================================================================
// OAuth2 Provider Options
// ============================================================================

const OAUTH2_PROVIDER_OPTIONS = [
  { value: 'cognito', label: 'Amazon Cognito (Auto-provisioned)' },
  { value: 'okta', label: 'Okta' },
  { value: 'azure_ad', label: 'Azure AD (Entra ID)' },
  { value: 'auth0', label: 'Auth0' },
  { value: 'google', label: 'Google' },
  { value: 'microsoft', label: 'Microsoft' },
  { value: 'github', label: 'GitHub' },
  { value: 'salesforce', label: 'Salesforce' },
  { value: 'slack', label: 'Slack' },
  { value: 'custom', label: 'Custom OIDC' },
];

const PROVIDER_DISCOVERY_HINTS: Record<string, string> = {
  okta: 'https://dev-xxxx.okta.com/.well-known/openid-configuration',
  azure_ad: 'https://login.microsoftonline.com/{tenant-id}/v2.0/.well-known/openid-configuration',
  auth0: 'https://{your-domain}.auth0.com/.well-known/openid-configuration',
  custom: 'https://your-idp.example.com/.well-known/openid-configuration',
};

// ============================================================================
// IdentityConfigurationModal Component
// ============================================================================

export function IdentityConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: IdentityConfigurationModalProps) {
  const [config, setConfig] = useState<IdentityConfiguration>(() => ({
    ...createDefaultIdentityConfig(),
    ...initialConfig,
  }));

  // Reset config when modal opens with new initial config
  useEffect(() => {
    if (isOpen) {
      setConfig({
        ...createDefaultIdentityConfig(),
        ...initialConfig,
      });
    }
  }, [isOpen, initialConfig]);

  // Validation
  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];

    if (!config.name.trim()) {
      errors.push({ field: 'name', message: 'Name is required' });
    }

    if (config.credentialType === 'oauth2' && config.oauth2Config) {
      const isExternal = config.oauth2Config.provider !== 'cognito';
      // Cognito credentials are auto-provisioned during gateway deployment
      if (isExternal) {
        if (!config.oauth2Config.clientId) {
          errors.push({ field: 'clientId', message: 'Client ID is required' });
        }
        if (!config.oauth2Config.clientSecretRef) {
          errors.push({ field: 'clientSecretRef', message: 'Client secret reference is required' });
        }
        // Discovery URL required for external IDPs (except google/microsoft/github/salesforce/slack which are not gateway-relevant)
        const needsDiscovery = ['okta', 'azure_ad', 'auth0', 'custom'].includes(config.oauth2Config.provider);
        if (needsDiscovery && !config.oauth2Config.discoveryUrl) {
          errors.push({ field: 'discoveryUrl', message: 'OIDC Discovery URL is required' });
        }
      }
    }

    if (config.credentialType === 'api_key' && config.apiKeyConfig) {
      if (!config.apiKeyConfig.keyName) {
        errors.push({ field: 'keyName', message: 'Key name is required' });
      }
      if (!config.apiKeyConfig.keyValueRef) {
        errors.push({ field: 'keyValueRef', message: 'Key value reference is required' });
      }
    }

    return errors;
  }, [config]);

  // Update handlers
  const updateConfig = useCallback(<K extends keyof IdentityConfiguration>(
    key: K,
    value: IdentityConfiguration[K]
  ) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  }, []);

  // Handle credential type change
  const handleCredentialTypeChange = useCallback((credentialType: 'oauth2' | 'api_key') => {
    setConfig((prev) => ({
      ...prev,
      credentialType,
      oauth2Config: credentialType === 'oauth2' ? {
        provider: 'google',
        clientId: '',
        clientSecretRef: '',
        scopes: [],
      } : undefined,
      apiKeyConfig: credentialType === 'api_key' ? {
        keyName: '',
        keyValueRef: '',
        headerName: 'X-API-Key',
      } : undefined,
    }));
  }, []);

  // Handle save
  const handleSave = useCallback(() => {
    onSave(config);
    onClose();
  }, [config, onSave, onClose]);

  // Get field error
  const getFieldError = (field: string) =>
    validationErrors.find((e) => e.field === field)?.message;

  // Build tabs
  const tabs = useMemo(() => [
    {
      id: 'general',
      label: 'General',
      hasError: validationErrors.some((e) => e.field === 'name'),
      content: (
        <div className="space-y-6">
          <FormSection title="Basic Information">
            <TextField
              id="name"
              label="Name"
              value={config.name}
              onChange={(value) => updateConfig('name', value)}
              placeholder="Enter identity name"
              required
              error={getFieldError('name')}
            />

            <SelectField
              id="credentialType"
              label="Credential Type"
              value={config.credentialType}
              onChange={(value) => handleCredentialTypeChange(value as 'oauth2' | 'api_key')}
              options={[
                { value: 'oauth2', label: 'OAuth2 - For services requiring OAuth2 authentication' },
                { value: 'api_key', label: 'API Key - For services using API key authentication' },
              ]}
              required
            />

            <SelectField
              id="identityMode"
              label="Execution Role Isolation"
              value={config.mode ?? 'shared'}
              onChange={(value) => updateConfig('mode', value as 'shared' | 'per_agent')}
              options={[
                { value: 'shared', label: 'Shared execution role (default, fastest deploy)' },
                { value: 'per_agent', label: 'Per-agent least-privilege role (opt-in, slower first deploy)' },
              ]}
              helpText="Per-agent mode mints a least-privilege IAM role scoped to only this agent's wired resources. The first deploy is slower (one-time IAM propagation delay); subsequent deploys are unaffected."
            />
          </FormSection>
        </div>
      ),
    },
    {
      id: 'credentials',
      label: 'Credentials',
      hasError: validationErrors.some((e) => ['clientId', 'clientSecretRef', 'keyName', 'keyValueRef'].includes(e.field)),
      content: (
        <div className="space-y-6">
          {config.credentialType === 'oauth2' && config.oauth2Config && (
            <FormSection
              title="OAuth2 Configuration"
              description="Configure OAuth2 credentials for external service access"
            >
              <SelectField
                id="provider"
                label="Provider"
                value={config.oauth2Config.provider}
                onChange={(value) => setConfig((prev) => ({
                  ...prev,
                  oauth2Config: { ...prev.oauth2Config!, provider: value as OAuth2Provider },
                }))}
                options={OAUTH2_PROVIDER_OPTIONS}
              />

              {config.oauth2Config.provider === 'cognito' && (
                <div className="rounded-md bg-blue-50 border border-blue-200 p-4">
                  <div className="flex">
                    <div className="flex-shrink-0">
                      <svg className="h-5 w-5 text-blue-400" viewBox="0 0 20 20" fill="currentColor">
                        <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clipRule="evenodd" />
                      </svg>
                    </div>
                    <div className="ml-3">
                      <h3 className="text-sm font-medium text-blue-800">Auto-provisioned Credentials</h3>
                      <p className="mt-1 text-sm text-blue-700">
                        Amazon Cognito credentials (Client ID, Client Secret, User Pool) are automatically
                        provisioned during gateway deployment. The agent uses JWT forwarding to pass the
                        caller's token to the gateway — no manual credential configuration needed.
                      </p>
                    </div>
                  </div>
                </div>
              )}

              {/* Discovery URL for external IDPs */}
              {['okta', 'azure_ad', 'auth0', 'custom'].includes(config.oauth2Config.provider) && (
                <TextField
                  id="discoveryUrl"
                  label="OIDC Discovery URL"
                  value={config.oauth2Config.discoveryUrl || ''}
                  onChange={(value) => setConfig((prev) => ({
                    ...prev,
                    oauth2Config: { ...prev.oauth2Config!, discoveryUrl: value },
                  }))}
                  placeholder={PROVIDER_DISCOVERY_HINTS[config.oauth2Config.provider] || 'https://your-idp/.well-known/openid-configuration'}
                  required
                  error={getFieldError('discoveryUrl')}
                  helpText={`The OpenID Connect discovery endpoint for your ${config.oauth2Config.provider === 'custom' ? 'identity provider' : config.oauth2Config.provider === 'azure_ad' ? 'Azure AD tenant' : config.oauth2Config.provider.charAt(0).toUpperCase() + config.oauth2Config.provider.slice(1) + ' organization'}`}
                />
              )}

              <TextField
                id="clientId"
                label="Client ID"
                value={config.oauth2Config.clientId}
                onChange={(value) => setConfig((prev) => ({
                  ...prev,
                  oauth2Config: { ...prev.oauth2Config!, clientId: value },
                }))}
                placeholder={config.oauth2Config.provider === 'cognito' ? 'Auto-provisioned during deployment' : 'Enter OAuth2 client ID'}
                required={config.oauth2Config.provider !== 'cognito'}
                disabled={config.oauth2Config.provider === 'cognito'}
                error={getFieldError('clientId')}
              />

              <TextField
                id="clientSecretRef"
                label={config.oauth2Config.provider === 'cognito' ? 'Client Secret Reference' : 'Client Secret'}
                value={config.oauth2Config.clientSecretRef}
                onChange={(value) => setConfig((prev) => ({
                  ...prev,
                  oauth2Config: { ...prev.oauth2Config!, clientSecretRef: value },
                }))}
                placeholder={config.oauth2Config.provider === 'cognito' ? 'Auto-provisioned during deployment' : 'Enter client secret'}
                required={config.oauth2Config.provider !== 'cognito'}
                disabled={config.oauth2Config.provider === 'cognito'}
                error={getFieldError('clientSecretRef')}
                helpText={config.oauth2Config.provider === 'cognito' ? 'Managed automatically via Cognito User Pool' : 'The client secret from your identity provider'}
              />

              {/* Audience for Auth0/Okta */}
              {['okta', 'azure_ad', 'auth0'].includes(config.oauth2Config.provider) && (
                <TextField
                  id="audience"
                  label="Audience (optional)"
                  value={config.oauth2Config.audience || ''}
                  onChange={(value) => setConfig((prev) => ({
                    ...prev,
                    oauth2Config: { ...prev.oauth2Config!, audience: value },
                  }))}
                  placeholder={config.oauth2Config.provider === 'auth0' ? 'https://your-api.example.com' : 'api://your-app-id'}
                  helpText="The audience identifier for your API (resource server)"
                />
              )}
            </FormSection>
          )}

          {config.credentialType === 'api_key' && config.apiKeyConfig && (
            <FormSection
              title="API Key Configuration"
              description="Configure API key credentials for external service access"
            >
              <TextField
                id="keyName"
                label="Key Name"
                value={config.apiKeyConfig.keyName}
                onChange={(value) => setConfig((prev) => ({
                  ...prev,
                  apiKeyConfig: { ...prev.apiKeyConfig!, keyName: value },
                }))}
                placeholder="Enter a name for this API key"
                required
                error={getFieldError('keyName')}
              />

              <TextField
                id="keyValueRef"
                label="Key Value Reference"
                value={config.apiKeyConfig.keyValueRef}
                onChange={(value) => setConfig((prev) => ({
                  ...prev,
                  apiKeyConfig: { ...prev.apiKeyConfig!, keyValueRef: value },
                }))}
                placeholder="secrets/my-api-key"
                required
                error={getFieldError('keyValueRef')}
                helpText="Reference to the secret in AWS Secrets Manager"
              />

              <TextField
                id="headerName"
                label="Header Name"
                value={config.apiKeyConfig.headerName}
                onChange={(value) => setConfig((prev) => ({
                  ...prev,
                  apiKeyConfig: { ...prev.apiKeyConfig!, headerName: value },
                }))}
                placeholder="X-API-Key"
                helpText="HTTP header name for the API key"
              />
            </FormSection>
          )}
        </div>
      ),
    },
  ], [config, validationErrors, updateConfig, handleCredentialTypeChange, getFieldError]);

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title="Configure AgentCore Identity"
      tabs={tabs}
      validationErrors={validationErrors}
    />
  );
}

export default IdentityConfigurationModal;
