/**
 * ConnectorConfigModal — configuration for SaaS connector nodes (Phase A).
 *
 * A connector is a `tool`-typed node whose toolId is "connector:<id>" (see
 * ComponentPalette / dragDrop). This modal lets the user:
 *   - confirm the connector (catalog-driven, read-only for branded connectors;
 *     editable connectorId for the generic OpenAPI/MCP connector),
 *   - pick an auth method gated by the catalog support map (CONNECTOR_AUTH_SUPPORT,
 *     mirroring backend services/connectors.py — Asana = api_key only),
 *   - enter the secret (api key / client secret). The raw secret is TRANSIENT:
 *     it is forwarded to the deploy payload (which mints a Secrets Manager
 *     secret) and stripped from node data before persist — never written to the
 *     canvas JSON / DDB.
 *   - configure scopes + clientId (oauth2_cc), or a spec URL / inline spec
 *     (generic connector).
 *
 * Styling mirrors ToolConfigModal / GatewayConfigurationModal (ConfigurationModal
 * shell + FormFields).
 */

import { useState, useCallback, useMemo } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { FormSection, TextField, TextArea, SelectField } from './FormFields';
import {
  CONNECTOR_TOOL_PREFIX,
  type ConnectorConfiguration,
  type ConnectorAuthMethod,
  type ConnectorId,
} from '../../types/components';

// ============================================================================
// Catalog support map (mirror of backend services/connectors.py)
// ============================================================================

interface ConnectorCatalogEntry {
  displayName: string;
  /** Auth methods the backend catalog supports for this connector. */
  authMethods: ConnectorAuthMethod[];
  /** OAuth2 vendor passed to create_oauth2_credential_provider (oauth2_cc). */
  oauthVendor?: string;
  /** Default api-key wiring (catalog defaults; editable for the generic one). */
  credentialLocation: 'HEADER' | 'QUERY_PARAMETER';
  credentialParameterName: string;
  credentialPrefix?: string;
  /** generic connector requires a user-supplied spec. */
  generic?: boolean;
}

// asana = api_key only (no oauth vendor). jira=AtlassianOauth2, slack=SlackOauth2,
// github=GithubOauth2, salesforce=SalesforceOauth2.
export const CONNECTOR_AUTH_SUPPORT: Record<string, ConnectorCatalogEntry> = {
  jira: {
    displayName: 'Jira',
    authMethods: ['oauth2_cc', 'api_key'],
    oauthVendor: 'AtlassianOauth2',
    credentialLocation: 'HEADER',
    credentialParameterName: 'Authorization',
    credentialPrefix: 'Bearer',
  },
  asana: {
    displayName: 'Asana',
    authMethods: ['api_key'],
    credentialLocation: 'HEADER',
    credentialParameterName: 'Authorization',
    credentialPrefix: 'Bearer',
  },
  slack: {
    displayName: 'Slack',
    authMethods: ['oauth2_cc', 'api_key'],
    oauthVendor: 'SlackOauth2',
    credentialLocation: 'HEADER',
    credentialParameterName: 'Authorization',
    credentialPrefix: 'Bearer',
  },
  github: {
    displayName: 'GitHub',
    authMethods: ['oauth2_cc', 'api_key'],
    oauthVendor: 'GithubOauth2',
    credentialLocation: 'HEADER',
    credentialParameterName: 'Authorization',
    credentialPrefix: 'Bearer',
  },
  salesforce: {
    displayName: 'Salesforce',
    authMethods: ['oauth2_cc', 'api_key'],
    oauthVendor: 'SalesforceOauth2',
    credentialLocation: 'HEADER',
    credentialParameterName: 'Authorization',
    credentialPrefix: 'Bearer',
  },
  generic_openapi: {
    displayName: 'OpenAPI / MCP Connector',
    authMethods: ['api_key', 'oauth2_cc'],
    credentialLocation: 'HEADER',
    credentialParameterName: 'Authorization',
    credentialPrefix: 'Bearer',
    generic: true,
  },
};

const AUTH_METHOD_LABELS: Record<ConnectorAuthMethod, string> = {
  api_key: 'API key',
  oauth2_cc: 'OAuth 2.0 (client credentials)',
};

const CREDENTIAL_LOCATION_OPTIONS = [
  { value: 'HEADER', label: 'Header' },
  { value: 'QUERY_PARAMETER', label: 'Query parameter' },
];

function connectorIdFromConfig(cfg: Partial<ConnectorConfiguration>): ConnectorId {
  if (cfg.connectorId) return cfg.connectorId;
  if (cfg.toolId?.startsWith(CONNECTOR_TOOL_PREFIX)) {
    return cfg.toolId.slice(CONNECTOR_TOOL_PREFIX.length);
  }
  return 'generic_openapi';
}

// ============================================================================
// Props
// ============================================================================

export interface ConnectorConfigModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: ConnectorConfiguration) => void;
  initialConfig?: Partial<ConnectorConfiguration>;
}

// ============================================================================
// Component
// ============================================================================

export function ConnectorConfigModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: ConnectorConfigModalProps) {
  const initialConnectorId = connectorIdFromConfig(initialConfig ?? {});
  const initialEntry = CONNECTOR_AUTH_SUPPORT[initialConnectorId] ?? CONNECTOR_AUTH_SUPPORT.generic_openapi;

  // Preserve every field we don't surface by spreading initialConfig.
  const [config, setConfig] = useState<ConnectorConfiguration>(() => ({
    ...(initialConfig as ConnectorConfiguration),
    name: initialConfig?.name ?? initialEntry.displayName,
    toolId: initialConfig?.toolId ?? `${CONNECTOR_TOOL_PREFIX}${initialConnectorId}`,
    description: initialConfig?.description ?? '',
    enabled: initialConfig?.enabled ?? true,
    isConnector: true,
    connectorId: initialConnectorId,
    authMethod:
      initialConfig?.authMethod && initialEntry.authMethods.includes(initialConfig.authMethod)
        ? initialConfig.authMethod
        : initialEntry.authMethods[0],
    configured: initialConfig?.configured ?? false,
    // api-key defaults from the catalog (editable for the generic connector).
    credentialLocation: initialConfig?.credentialLocation ?? initialEntry.credentialLocation,
    credentialParameterName: initialConfig?.credentialParameterName ?? initialEntry.credentialParameterName,
    credentialPrefix: initialConfig?.credentialPrefix ?? initialEntry.credentialPrefix,
    oauthVendor: initialConfig?.oauthVendor ?? initialEntry.oauthVendor,
    scopes: initialConfig?.scopes ?? [],
  }));

  // Transient secret — held in modal state only, never spread back into the
  // node unless the user typed a new value (see handleSave).
  const [secretValue, setSecretValue] = useState('');
  const [scopesText, setScopesText] = useState((initialConfig?.scopes ?? []).join(', '));

  const entry = CONNECTOR_AUTH_SUPPORT[config.connectorId] ?? CONNECTOR_AUTH_SUPPORT.generic_openapi;
  const isGeneric = !!entry.generic;

  const update = useCallback(
    <K extends keyof ConnectorConfiguration>(k: K, v: ConnectorConfiguration[K]) =>
      setConfig((prev) => ({ ...prev, [k]: v })),
    [],
  );

  const authOptions = useMemo(
    () => entry.authMethods.map((m) => ({ value: m, label: AUTH_METHOD_LABELS[m] })),
    [entry],
  );

  const validationErrors: ValidationError[] = useMemo(() => {
    const errs: ValidationError[] = [];
    if (!config.name?.trim()) {
      errs.push({ field: 'name', message: 'A connector name is required.' });
    }
    if (isGeneric && !config.specUrl?.trim() && !config.specContent?.trim()) {
      errs.push({ field: 'specUrl', message: 'Provide an OpenAPI spec URL or inline spec.' });
    }
    if (config.authMethod === 'oauth2_cc' && !config.clientId?.trim()) {
      errs.push({ field: 'clientId', message: 'OAuth client credentials require a client ID.' });
    }
    // A secret is required the first time (not yet configured + none provided).
    if (!config.configured && !secretValue.trim() && !config.secretArn) {
      const label = config.authMethod === 'oauth2_cc' ? 'a client secret' : 'an API key';
      errs.push({ field: 'secret', message: `Enter ${label} to configure this connector.` });
    }
    return errs;
  }, [config, isGeneric, secretValue]);

  const handleSave = useCallback(() => {
    const scopes = scopesText
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);

    const next: ConnectorConfiguration = {
      ...config,
      name: config.name.trim(),
      // Keep toolId in sync with the (possibly edited) connectorId.
      toolId: `${CONNECTOR_TOOL_PREFIX}${config.connectorId}`,
      scopes: config.authMethod === 'oauth2_cc' ? scopes : undefined,
      oauthVendor: config.authMethod === 'oauth2_cc' ? entry.oauthVendor ?? config.oauthVendor : undefined,
      // Transient secret: attach only when the user typed one this session. It
      // is forwarded to the deploy payload then stripped before persist.
      secretValue: secretValue.trim() ? secretValue.trim() : undefined,
      // Marked configured once a secret has ever been supplied or an arn exists.
      configured: config.configured || !!secretValue.trim() || !!config.secretArn,
    };
    onSave(next);
    onClose();
  }, [config, scopesText, secretValue, entry, onSave, onClose]);

  const generalTab = (
    <div className="space-y-5">
      <FormSection title="Connector">
        <TextField
          id="name"
          label="Name"
          required
          value={config.name}
          onChange={(v) => update('name', v)}
          placeholder={entry.displayName}
          helpText="Display name shown on the canvas node."
        />
        {isGeneric ? (
          <TextField
            id="connectorId"
            label="Connector ID"
            value={config.connectorId}
            onChange={(v) => update('connectorId', v)}
            placeholder="generic_openapi"
            helpText="Stable identifier for this generic connector."
          />
        ) : (
          <TextField
            id="connectorId"
            label="Connector"
            value={entry.displayName}
            onChange={() => undefined}
            disabled
            helpText="Built-in connector from the catalog. Not editable."
          />
        )}
        <SelectField
          id="authMethod"
          label="Authentication method"
          value={config.authMethod}
          onChange={(v) => update('authMethod', v as ConnectorAuthMethod)}
          options={authOptions}
          helpText={
            entry.authMethods.length === 1
              ? `${entry.displayName} supports ${AUTH_METHOD_LABELS[entry.authMethods[0]]} only.`
              : 'Choose how the gateway authenticates outbound calls.'
          }
        />
      </FormSection>

      <FormSection
        title="Credentials"
        description="Stored in AWS Secrets Manager scoped to your account. Never saved to the canvas."
      >
        <TextField
          id="secret"
          label={config.authMethod === 'oauth2_cc' ? 'Client secret' : 'API key'}
          type="password"
          value={secretValue}
          onChange={setSecretValue}
          placeholder={
            config.configured
              ? 'Configured — leave blank to keep the existing secret'
              : config.authMethod === 'oauth2_cc'
                ? 'Paste the OAuth client secret'
                : 'Paste the API key / token'
          }
          helpText={
            config.configured
              ? 'A secret is already configured. Enter a new value to rotate it.'
              : undefined
          }
        />
        {config.authMethod === 'oauth2_cc' && (
          <>
            <TextField
              id="clientId"
              label="Client ID"
              required
              value={config.clientId ?? ''}
              onChange={(v) => update('clientId', v)}
              placeholder="OAuth client ID"
            />
            <TextField
              id="scopes"
              label="Scopes"
              value={scopesText}
              onChange={setScopesText}
              placeholder="read:issues, write:issues"
              helpText="Comma-separated OAuth scopes."
            />
            {isGeneric && (
              <TextField
                id="discoveryUrl"
                label="Discovery URL"
                type="url"
                value={config.discoveryUrl ?? ''}
                onChange={(v) => update('discoveryUrl', v)}
                placeholder="https://issuer/.well-known/openid-configuration"
                helpText="OIDC/OAuth discovery document for the token endpoint."
              />
            )}
          </>
        )}
        {config.authMethod === 'api_key' && isGeneric && (
          <>
            <SelectField
              id="credentialLocation"
              label="Credential location"
              value={config.credentialLocation ?? 'HEADER'}
              onChange={(v) => update('credentialLocation', v as 'HEADER' | 'QUERY_PARAMETER')}
              options={CREDENTIAL_LOCATION_OPTIONS}
            />
            <TextField
              id="credentialParameterName"
              label="Parameter name"
              value={config.credentialParameterName ?? 'Authorization'}
              onChange={(v) => update('credentialParameterName', v)}
              placeholder="Authorization"
            />
            <TextField
              id="credentialPrefix"
              label="Prefix"
              value={config.credentialPrefix ?? ''}
              onChange={(v) => update('credentialPrefix', v)}
              placeholder="Bearer"
              helpText="Optional prefix prepended to the key (e.g. Bearer, token)."
            />
          </>
        )}
      </FormSection>

      {isGeneric && (
        <FormSection
          title="OpenAPI spec"
          description="Provide a spec URL or paste the spec inline (one is required)."
        >
          <TextField
            id="specUrl"
            label="Spec URL"
            type="url"
            value={config.specUrl ?? ''}
            onChange={(v) => update('specUrl', v)}
            placeholder="https://api.example.com/openapi.json"
          />
          <TextArea
            id="specContent"
            label="Inline spec"
            value={config.specContent ?? ''}
            onChange={(v) => update('specContent', v)}
            rows={6}
            placeholder="Paste an OpenAPI JSON/YAML document"
            helpText="Used when no spec URL is provided."
          />
        </FormSection>
      )}
    </div>
  );

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title={`Configure Connector: ${config.name || entry.displayName}`}
      tabs={[{ id: 'general', label: 'General', content: generalTab }]}
      validationErrors={validationErrors}
    />
  );
}

export default ConnectorConfigModal;
