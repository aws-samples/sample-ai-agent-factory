/**
 * Connector configuration helpers and constants.
 * Extracted to support React Fast Refresh requirements.
 */

import type { ConnectorAuthMethod, ConnectorId, ConnectorConfiguration } from '../../types/components';
import { CONNECTOR_TOOL_PREFIX } from '../../types/components';

export interface ConnectorCatalogEntry {
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

export const AUTH_METHOD_LABELS: Record<ConnectorAuthMethod, string> = {
  api_key: 'API key',
  oauth2_cc: 'OAuth 2.0 (client credentials)',
};

export const CREDENTIAL_LOCATION_OPTIONS = [
  { value: 'HEADER', label: 'Header' },
  { value: 'QUERY_PARAMETER', label: 'Query parameter' },
];

export function connectorIdFromConfig(cfg: Partial<ConnectorConfiguration>): ConnectorId {
  if (cfg.connectorId) return cfg.connectorId;
  if (cfg.toolId?.startsWith(CONNECTOR_TOOL_PREFIX)) {
    return cfg.toolId.slice(CONNECTOR_TOOL_PREFIX.length);
  }
  return 'generic_openapi';
}
