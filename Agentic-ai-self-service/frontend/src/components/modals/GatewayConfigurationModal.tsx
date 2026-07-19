/**
 * GatewayConfiguration modal for configuring AgentCore Gateway components.
 * Requirements: 4.1, 4.2, 4.3, 4.4
 *
 * Supports MULTIPLE targets of different families on ONE gateway (MCP servers +
 * Lambda ARNs + OpenAPI schemas + Smithy). The editor operates on a `targets[]`
 * array; on save it also mirrors `targets[0]` into the legacy single
 * `targetType`/`targetConfig` fields for backward compatibility.
 */

import { useState, useCallback, useMemo, useEffect } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, TextArea, SelectField, CheckboxField, FormSection } from './FormFields';
import { listMcpServersApi, type McpServerSummary } from '../../services/api';
import type {
  GatewayConfiguration,
  GatewayTargetType,
  GatewayTargetConfig,
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
  resolveGatewayTargets,
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

/** Seed the editable target list from an initial config, preferring the
 *  multi-target `targets[]` and falling back to the legacy single target. */
function seedTargets(initial?: Partial<GatewayConfiguration>): GatewayTargetConfig[] {
  const merged = { ...createDefaultGatewayConfig(), ...initial } as GatewayConfiguration;
  const resolved = resolveGatewayTargets(merged);
  return resolved.length > 0 ? resolved : [createDefaultTargetConfig('lambda')];
}

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
  const [targets, setTargets] = useState<GatewayTargetConfig[]>(() => seedTargets(initialConfig));

  // Reset config when modal opens with new initial config (adjust state during render pattern)
  const [lastInitial, setLastInitial] = useState<typeof initialConfig | symbol>(Symbol('unset'));
  if (isOpen && initialConfig !== lastInitial) {
    setLastInitial(initialConfig);
    setConfig({ ...createDefaultGatewayConfig(), ...initialConfig });
    setTargets(seedTargets(initialConfig));
  }

  // External MCP catalog — loaded lazily when ANY target is an MCP Server.
  // Only `direct-*` tiers are wireable as a Gateway target; `adapter-*` need a
  // hosted proxy first, so they're filtered out of the picker.
  const hasMcpTarget = useMemo(() => targets.some((t) => t.type === 'mcp_server'), [targets]);
  const [mcpCatalog, setMcpCatalog] = useState<McpServerSummary[]>([]);
  const [mcpCatalogError, setMcpCatalogError] = useState<string | null>(null);
  useEffect(() => {
    if (!hasMcpTarget || mcpCatalog.length > 0) return;
    let cancelled = false;
    listMcpServersApi()
      .then((all) => { if (!cancelled) setMcpCatalog(all.filter((s) => s.tier.startsWith('direct'))); })
      .catch((e) => { if (!cancelled) setMcpCatalogError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [hasMcpTarget, mcpCatalog.length]);

  // Validation — per-target, keyed by index so errors surface on the right row.
  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];

    if (!config.name.trim()) {
      errors.push({ field: 'name', message: 'Name is required' });
    }

    if (targets.length === 0) {
      errors.push({ field: 'targets', message: 'At least one target is required' });
    }

    targets.forEach((target, i) => {
      switch (target.type) {
        case 'openapi': {
          const openApiConfig = target as OpenAPITargetConfig;
          if (!openApiConfig.specUrl && !openApiConfig.specContent) {
            errors.push({ field: `specUrl_${i}`, message: 'OpenAPI spec URL or content is required' });
          }
          break;
        }
        case 'lambda': {
          const lambdaConfig = target as LambdaTargetConfig;
          if (lambdaConfig.functionArn && !isValidLambdaArn(lambdaConfig.functionArn)) {
            errors.push({ field: `functionArn_${i}`, message: 'Invalid Lambda ARN format' });
          }
          break;
        }
        case 'mcp_server': {
          const mcpConfig = target as MCPServerTargetConfig;
          if (mcpConfig.serverId === '__custom__' && !mcpConfig.serverUrl) {
            errors.push({ field: `serverUrl_${i}`, message: 'MCP endpoint URL is required' });
          }
          break;
        }
      }
    });

    return errors;
  }, [config, targets]);

  // Update handlers
  const updateConfig = useCallback(<K extends keyof GatewayConfiguration>(
    key: K,
    value: GatewayConfiguration[K]
  ) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  }, []);

  // Change one target's family — reseed that row with the family's defaults.
  const handleTargetTypeChange = useCallback((index: number, targetType: GatewayTargetType) => {
    setTargets((prev) => prev.map((t, i) => (i === index ? createDefaultTargetConfig(targetType) : t)));
  }, []);

  // Update a single field on the target at `index`.
  const updateTargetField = useCallback((index: number, key: string, value: unknown) => {
    setTargets((prev) =>
      prev.map((t, i) => (i === index ? ({ ...t, [key]: value } as GatewayTargetConfig) : t)),
    );
  }, []);

  const addTarget = useCallback(() => {
    setTargets((prev) => [...prev, createDefaultTargetConfig('lambda')]);
  }, []);

  const removeTarget = useCallback((index: number) => {
    setTargets((prev) => prev.filter((_, i) => i !== index));
  }, []);

  // Handle save — persist the array AND mirror targets[0] into the legacy
  // single-target fields so old consumers keep working.
  const handleSave = useCallback(() => {
    const primary = targets[0];
    onSave({
      ...config,
      targets,
      ...(primary ? { targetType: primary.type, targetConfig: primary } : {}),
    });
    onClose();
  }, [config, targets, onSave, onClose]);

  // Render one target's family-specific fields (keyed by row index so field
  // ids / testids stay unique across rows).
  const renderTargetFields = (target: GatewayTargetConfig, index: number) => {
    const getFieldError = (field: string) =>
      validationErrors.find((e) => e.field === field)?.message;

    switch (target.type) {
      case 'openapi': {
        const cfg = target as OpenAPITargetConfig;
        return (
          <>
            <TextField
              id={`specUrl_${index}`}
              label="OpenAPI Spec URL"
              value={cfg.specUrl || ''}
              onChange={(value) => updateTargetField(index, 'specUrl', value)}
              placeholder="https://api.example.com/openapi.json"
              error={getFieldError(`specUrl_${index}`)}
              helpText="URL to the OpenAPI specification"
            />
            <TextArea
              id={`specContent_${index}`}
              label="Or paste OpenAPI spec content"
              value={cfg.specContent || ''}
              onChange={(value) => updateTargetField(index, 'specContent', value)}
              placeholder="Paste OpenAPI JSON/YAML here..."
              rows={6}
              helpText="Alternatively, paste the OpenAPI spec directly"
            />
          </>
        );
      }

      case 'lambda': {
        const cfg = target as LambdaTargetConfig;
        return (
          <TextField
            id={`functionArn_${index}`}
            label="Lambda Function ARN"
            value={cfg.functionArn || ''}
            onChange={(value) => updateTargetField(index, 'functionArn', value)}
            placeholder="arn:aws:lambda:us-west-2:123456789012:function:my-function"
            error={getFieldError(`functionArn_${index}`)}
            helpText="Leave empty to auto-create a test Lambda function"
          />
        );
      }

      case 'smithy': {
        const cfg = target as SmithyTargetConfig;
        return (
          <SelectField
            id={`modelName_${index}`}
            label="Smithy Model"
            value={cfg.modelName || 'dynamodb'}
            onChange={(value) => updateTargetField(index, 'modelName', value)}
            options={[
              { value: 'dynamodb', label: 'DynamoDB' },
            ]}
            helpText="Pre-configured Smithy model"
          />
        );
      }

      case 'mcp_server': {
        const mcpCfg = target as MCPServerTargetConfig;
        const isCustom = mcpCfg.serverId === '__custom__';
        const selected = mcpCatalog.find((s) => s.id === mcpCfg.serverId);
        // For a catalog entry use its auth_type; for custom use the user's choice.
        const effectiveAuth = isCustom ? mcpCfg.authType || 'none' : selected?.auth_type;
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
              id={`serverId_${index}`}
              label="MCP Server"
              value={mcpCfg.serverId || ''}
              onChange={(value) => updateTargetField(index, 'serverId', value)}
              options={[
                { value: '', label: mcpCatalog.length ? 'Select a server…' : 'Loading catalog…' },
                ...mcpCatalog.map((s) => ({
                  value: s.id,
                  label: `${s.display_name} — ${s.auth_type === 'none' ? 'no auth' : s.auth_type}${s.live_testable ? ' ✓' : ''}`,
                })),
                { value: '__custom__', label: 'Custom endpoint…' },
              ]}
              required
              helpText="Pick a verified catalog server, or 'Custom endpoint…' to wire any external MCP server URL (direct tiers only)."
            />
            {selected && (
              <p className="text-[11px] text-gray-500 -mt-2">
                Endpoint: <span className="font-mono">{selected.endpoint}</span> · tier {selected.tier}
              </p>
            )}
            {/* Custom endpoint — raw https MCP server URL + auth type */}
            {isCustom && (
              <>
                <TextField
                  id={`customName_${index}`}
                  label="Name (optional)"
                  value={mcpCfg.customName || ''}
                  onChange={(value) => updateTargetField(index, 'customName', value)}
                  placeholder="my-mcp-server"
                  helpText="Label for the gateway target; a safe id is derived from it."
                />
                <TextField
                  id={`serverUrl_${index}`}
                  label="MCP Endpoint URL"
                  value={mcpCfg.serverUrl || ''}
                  onChange={(value) => updateTargetField(index, 'serverUrl', value)}
                  placeholder="https://your-mcp-host.example.com/mcp"
                  error={getFieldError(`serverUrl_${index}`)}
                  helpText="Must be https. Validated at deploy (private/link-local/metadata hosts are blocked)."
                  required
                />
                <SelectField
                  id={`customAuthType_${index}`}
                  label="Outbound Auth"
                  value={mcpCfg.authType || 'none'}
                  onChange={(value) => updateTargetField(index, 'authType', value)}
                  options={[
                    { value: 'none', label: 'None (public server)' },
                    { value: 'api_key', label: 'API key (Bearer / header)' },
                    { value: 'oauth2_client_credentials', label: 'OAuth2 client-credentials (M2M)' },
                    { value: 'iam_sigv4', label: 'IAM SigV4 (AWS-native target)' },
                  ]}
                  helpText="How the gateway authenticates outbound to this MCP server."
                />
              </>
            )}
            {/* Endpoint placeholders (e.g. a Shopify store domain). */}
            {placeholders.map((token) => (
              <TextField
                key={token}
                id={`ev_${index}_${token}`}
                label={`Endpoint value: ${token}`}
                value={(mcpCfg.endpointVars || {})[token] || ''}
                onChange={(value) =>
                  updateTargetField(index, 'endpointVars', { ...(mcpCfg.endpointVars || {}), [token]: value })
                }
                placeholder={token === 'store_domain' ? 'shop.myshopify.com' : token}
                helpText={`Fills {${token}} in the endpoint`}
              />
            ))}
            {/* Tier-2 API key */}
            {effectiveAuth === 'api_key' && (
              <TextField
                id={`apiKey_${index}`}
                label="API Key"
                type="password"
                value={mcpCfg.apiKey || ''}
                onChange={(value) => updateTargetField(index, 'apiKey', value)}
                placeholder="Paste the provider API key"
                helpText="Stored in Secrets Manager at deploy; never persisted in the canvas."
              />
            )}
            {/* Tier-3 OAuth client-credentials */}
            {effectiveAuth === 'oauth2_client_credentials' && (
              <>
                <TextField
                  id={`oauthClientId_${index}`} label="OAuth Client ID"
                  value={mcpCfg.oauth?.clientId || ''}
                  onChange={(value) => updateTargetField(index, 'oauth', { ...(mcpCfg.oauth || {}), clientId: value })}
                />
                <TextField
                  id={`oauthClientSecret_${index}`} label="OAuth Client Secret" type="password"
                  value={mcpCfg.oauth?.clientSecret || ''}
                  onChange={(value) => updateTargetField(index, 'oauth', { ...(mcpCfg.oauth || {}), clientSecret: value })}
                />
                <TextField
                  id={`oauthDiscoveryUrl_${index}`} label="OIDC Discovery URL"
                  value={mcpCfg.oauth?.discoveryUrl || ''}
                  onChange={(value) => updateTargetField(index, 'oauth', { ...(mcpCfg.oauth || {}), discoveryUrl: value })}
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

    const targetFieldPrefixes = ['specUrl', 'functionArn', 'serverUrl'];

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
          </FormSection>
        </div>
      ),
    },
    {
      id: 'target',
      label: 'Targets',
      hasError: validationErrors.some((e) =>
        e.field === 'targets' || targetFieldPrefixes.some((p) => e.field.startsWith(`${p}_`)),
      ),
      content: (
        <div className="space-y-6" data-testid="gateway-targets">
          <FormSection
            title="Gateway Targets"
            description="Add one or more targets of different families (OpenAPI, Lambda, Smithy, MCP Server) to this gateway."
          >
            {targets.map((target, index) => (
              <div
                key={index}
                data-testid={`target-row-${index}`}
                className="rounded-lg border border-gray-200 p-4 space-y-4 relative"
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="flex-1">
                    <SelectField
                      id={`targetType_${index}`}
                      label={`Target ${index + 1}`}
                      value={target.type}
                      onChange={(value) => handleTargetTypeChange(index, value as GatewayTargetType)}
                      options={TARGET_TYPE_OPTIONS}
                      required
                    />
                  </div>
                  <button
                    type="button"
                    data-testid={`remove-target-${index}`}
                    onClick={() => removeTarget(index)}
                    disabled={targets.length <= 1}
                    className="mt-6 px-2 py-1 text-xs font-medium text-red-600 hover:text-red-800 disabled:text-gray-300 disabled:cursor-not-allowed"
                    aria-label={`Remove target ${index + 1}`}
                  >
                    Remove
                  </button>
                </div>
                {renderTargetFields(target, index)}
              </div>
            ))}
            <button
              type="button"
              data-testid="add-target"
              onClick={addTarget}
              className="w-full py-2 px-4 border border-dashed border-gray-300 rounded-lg text-sm font-medium text-[#0972d3] hover:border-[#0972d3] hover:bg-blue-50 transition-colors"
            >
              + Add target
            </button>
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
  }, [config, targets, validationErrors, updateConfig, handleTargetTypeChange, addTarget, removeTarget]); // eslint-disable-line react-hooks/exhaustive-deps

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
