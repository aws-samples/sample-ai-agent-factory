/**
 * GatewayConfiguration modal for configuring AgentCore Gateway components.
 * Requirements: 4.1, 4.2, 4.3, 4.4
 */

import { useState, useCallback, useMemo, useEffect } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, TextArea, SelectField, CheckboxField, FormSection } from './FormFields';
import { listMcpServersApi, type McpServerSummary } from '../../services/api';
import type {
  GatewayConfiguration,
  GatewayTargetType,
  OpenAPITargetConfig,
  LambdaTargetConfig,
  SmithyTargetConfig,
  MCPServerTargetConfig,
} from '../../types/components';
import {
  TARGET_TYPE_OPTIONS,
  isValidLambdaArn,
  createDefaultGatewayConfig,
  createDefaultTargetConfig,
} from '../../utils/gatewayConfig';

// ============================================================================
// Props Interface
// ============================================================================

export interface GatewayConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: GatewayConfiguration) => void;
  initialConfig?: Partial<GatewayConfiguration>;
}

// ============================================================================
// GatewayConfigurationModal Component
// ============================================================================

export function GatewayConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: GatewayConfigurationModalProps) {
  const [config, setConfig] = useState<GatewayConfiguration>(() => ({
    ...createDefaultGatewayConfig(),
    ...initialConfig,
  }));

  // Reset config when modal opens with new initial config (adjust state during render pattern)
  const [lastInitial, setLastInitial] = useState<typeof initialConfig | symbol>(Symbol('unset'));
  if (isOpen && initialConfig !== lastInitial) {
    setLastInitial(initialConfig);
    setConfig({ ...createDefaultGatewayConfig(), ...initialConfig });
  }

  // External MCP catalog — loaded lazily when the MCP Server target is chosen.
  // Only `direct-*` tiers are wireable as a Gateway target; `adapter-*` need a
  // hosted proxy first, so they're filtered out of the picker.
  const [mcpCatalog, setMcpCatalog] = useState<McpServerSummary[]>([]);
  const [mcpCatalogError, setMcpCatalogError] = useState<string | null>(null);
  useEffect(() => {
    if (config.targetType !== 'mcp_server' || mcpCatalog.length > 0) return;
    let cancelled = false;
    listMcpServersApi()
      .then((all) => { if (!cancelled) setMcpCatalog(all.filter((s) => s.tier.startsWith('direct'))); })
      .catch((e) => { if (!cancelled) setMcpCatalogError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [config.targetType, mcpCatalog.length]);

  // Validation
  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];

    if (!config.name.trim()) {
      errors.push({ field: 'name', message: 'Name is required' });
    }

    // Target-specific validation
    switch (config.targetType) {
      case 'openapi': {
        const openApiConfig = config.targetConfig as OpenAPITargetConfig;
        if (!openApiConfig.specUrl && !openApiConfig.specContent) {
          errors.push({ field: 'specUrl', message: 'OpenAPI spec URL or content is required' });
        }
        break;
      }
      case 'lambda': {
        const lambdaConfig = config.targetConfig as LambdaTargetConfig;
        if (lambdaConfig.functionArn && !isValidLambdaArn(lambdaConfig.functionArn)) {
          errors.push({ field: 'functionArn', message: 'Invalid Lambda ARN format' });
        }
        break;
      }
    }

    return errors;
  }, [config]);

  // Update handlers
  const updateConfig = useCallback(<K extends keyof GatewayConfiguration>(
    key: K,
    value: GatewayConfiguration[K]
  ) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  }, []);

  // Handle target type change
  const handleTargetTypeChange = useCallback((targetType: GatewayTargetType) => {
    setConfig((prev) => ({
      ...prev,
      targetType,
      targetConfig: createDefaultTargetConfig(targetType),
    }));
  }, []);

  // Update target config
  const updateTargetConfig = useCallback(<K extends string>(key: K, value: unknown) => {
    setConfig((prev) => ({
      ...prev,
      targetConfig: { ...prev.targetConfig, [key]: value },
    }));
  }, []);

  // Handle save
  const handleSave = useCallback(() => {
    onSave(config);
    onClose();
  }, [config, onSave, onClose]);

  // Render target-specific fields
  const renderTargetFields = () => {
    const getFieldError = (field: string) =>
      validationErrors.find((e) => e.field === field)?.message;
    switch (config.targetType) {
      case 'openapi':
        return (
          <>
            <TextField
              id="specUrl"
              label="OpenAPI Spec URL"
              value={(config.targetConfig as OpenAPITargetConfig).specUrl || ''}
              onChange={(value) => updateTargetConfig('specUrl', value)}
              placeholder="https://api.example.com/openapi.json"
              error={getFieldError('specUrl')}
              helpText="URL to the OpenAPI specification"
            />
            <TextArea
              id="specContent"
              label="Or paste OpenAPI spec content"
              value={(config.targetConfig as OpenAPITargetConfig).specContent || ''}
              onChange={(value) => updateTargetConfig('specContent', value)}
              placeholder="Paste OpenAPI JSON/YAML here..."
              rows={6}
              helpText="Alternatively, paste the OpenAPI spec directly"
            />
          </>
        );

      case 'lambda':
        return (
          <TextField
            id="functionArn"
            label="Lambda Function ARN"
            value={(config.targetConfig as LambdaTargetConfig).functionArn || ''}
            onChange={(value) => updateTargetConfig('functionArn', value)}
            placeholder="arn:aws:lambda:us-west-2:123456789012:function:my-function"
            error={getFieldError('functionArn')}
            helpText="Leave empty to auto-create a test Lambda function"
          />
        );

      case 'smithy':
        return (
          <SelectField
            id="modelName"
            label="Smithy Model"
            value={(config.targetConfig as SmithyTargetConfig).modelName || 'dynamodb'}
            onChange={(value) => updateTargetConfig('modelName', value)}
            options={[
              { value: 'dynamodb', label: 'DynamoDB' },
            ]}
            helpText="Pre-configured Smithy model"
          />
        );

      case 'mcp_server': {
        const mcpCfg = config.targetConfig as MCPServerTargetConfig;
        const selected = mcpCatalog.find((s) => s.id === mcpCfg.serverId);
        // Extract {placeholder} tokens from the selected catalog endpoint.
        const placeholders = selected?.endpoint
          ? Array.from(selected.endpoint.matchAll(/\{([a-zA-Z0-9_]+)\}/g)).map((m) => m[1])
          : [];
        return (
          <>
            {mcpCatalogError && (
              <div className="text-xs text-red-600 mb-2">Failed to load MCP catalog: {mcpCatalogError}</div>
            )}
            <SelectField
              id="serverId"
              label="MCP Server"
              value={mcpCfg.serverId || ''}
              onChange={(value) => updateTargetConfig('serverId', value)}
              options={[
                { value: '', label: mcpCatalog.length ? 'Select a server…' : 'Loading catalog…' },
                ...mcpCatalog.map((s) => ({
                  value: s.id,
                  label: `${s.display_name} — ${s.auth_type === 'none' ? 'no auth' : s.auth_type}${s.live_testable ? ' ✓' : ''}`,
                })),
              ]}
              required
              helpText="Verified external MCP servers wireable as a Gateway target (direct tiers only)."
            />
            {selected && (
              <p className="text-[11px] text-gray-500 -mt-2">
                Endpoint: <span className="font-mono">{selected.endpoint}</span> · tier {selected.tier}
              </p>
            )}
            {/* Endpoint placeholders (e.g. a Shopify store domain). */}
            {placeholders.map((token) => (
              <TextField
                key={token}
                id={`ev_${token}`}
                label={`Endpoint value: ${token}`}
                value={(mcpCfg.endpointVars || {})[token] || ''}
                onChange={(value) =>
                  updateTargetConfig('endpointVars', { ...(mcpCfg.endpointVars || {}), [token]: value })
                }
                placeholder={token === 'store_domain' ? 'shop.myshopify.com' : token}
                helpText={`Fills {${token}} in the endpoint`}
              />
            ))}
            {/* Tier-2 API key */}
            {selected?.auth_type === 'api_key' && (
              <TextField
                id="apiKey"
                label="API Key"
                type="password"
                value={mcpCfg.apiKey || ''}
                onChange={(value) => updateTargetConfig('apiKey', value)}
                placeholder="Paste the provider API key"
                helpText="Stored in Secrets Manager at deploy; never persisted in the canvas."
              />
            )}
            {/* Tier-3 OAuth client-credentials */}
            {selected?.auth_type === 'oauth2_client_credentials' && (
              <>
                <TextField
                  id="oauthClientId" label="OAuth Client ID"
                  value={mcpCfg.oauth?.clientId || ''}
                  onChange={(value) => updateTargetConfig('oauth', { ...(mcpCfg.oauth || {}), clientId: value })}
                />
                <TextField
                  id="oauthClientSecret" label="OAuth Client Secret" type="password"
                  value={mcpCfg.oauth?.clientSecret || ''}
                  onChange={(value) => updateTargetConfig('oauth', { ...(mcpCfg.oauth || {}), clientSecret: value })}
                />
                <TextField
                  id="oauthDiscoveryUrl" label="OIDC Discovery URL"
                  value={mcpCfg.oauth?.discoveryUrl || ''}
                  onChange={(value) => updateTargetConfig('oauth', { ...(mcpCfg.oauth || {}), discoveryUrl: value })}
                  placeholder="https://idp.example.com/.well-known/openid-configuration"
                />
              </>
            )}
          </>
        );
      }

      default:
        return null;
    }
  };

  // Build tabs
  const tabs = useMemo(() => {
    const getFieldError = (field: string) =>
      validationErrors.find((e) => e.field === field)?.message;

    return [
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
              placeholder="Enter gateway name"
              required
              error={getFieldError('name')}
            />

            <SelectField
              id="targetType"
              label="Target Type"
              value={config.targetType}
              onChange={(value) => handleTargetTypeChange(value as GatewayTargetType)}
              options={TARGET_TYPE_OPTIONS}
              required
              helpText="Type of service to expose through the gateway"
            />
          </FormSection>
        </div>
      ),
    },
    {
      id: 'target',
      label: 'Target Configuration',
      hasError: validationErrors.some((e) => ['specUrl', 'functionArn'].includes(e.field)),
      content: (
        <div className="space-y-6">
          <FormSection
            title="Target Settings"
            description={`Configure the ${config.targetType} target`}
          >
            {renderTargetFields()}
          </FormSection>
        </div>
      ),
    },
    {
      id: 'features',
      label: 'Features',
      content: (
        <div className="space-y-6">
          <FormSection
            title="Gateway Features"
            description="Enable or disable gateway features"
          >
            <CheckboxField
              id="enableSemanticSearch"
              label="Enable Semantic Search"
              checked={config.enableSemanticSearch}
              onChange={(checked) => updateConfig('enableSemanticSearch', checked)}
              helpText="Enable semantic search for better tool discovery"
            />
          </FormSection>
        </div>
      ),
    },
    ];
  }, [config, validationErrors, updateConfig, handleTargetTypeChange, renderTargetFields]);

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title="Configure AgentCore Gateway"
      tabs={tabs}
      validationErrors={validationErrors}
    />
  );
}

export default GatewayConfigurationModal;
