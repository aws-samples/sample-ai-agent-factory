/**
 * Phase 3 Gap 3E — static connector catalog mirror.
 *
 * This is a lightweight client-side mirror of the backend connector catalog
 * (backend/src/app/services/connectors_catalog.py) used for instant render and
 * iconography/grouping in the ConnectorPickerModal. The AUTHORITATIVE
 * tool_schemas + credential_schema are fetched from GET /api/connectors/{id}
 * at open time (see getConnectorApi in services/api.ts); only the summary
 * fields are mirrored here so the picker can paint before the fetch resolves.
 *
 * Scope: catalog-only. Per-connector execution is a documented follow-up.
 */

export type ConnectorAuthType = 'oauth' | 'api_key';

export interface ConnectorSummary {
  id: string;
  display_name: string;
  icon: string;
  category: string;
  auth_type: ConnectorAuthType;
  capabilities: string[];
}

export interface ConnectorToolSchema {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export interface ConnectorDetail extends ConnectorSummary {
  credential_schema: Record<string, unknown>;
  tool_schemas: ConnectorToolSchema[];
}

/** Back-compat alias. */
export type Connector = ConnectorSummary;

/**
 * Summary mirror of the 12 backend connectors. Order matches the backend
 * catalog. Keep ids in sync with connectors_catalog.py CONNECTORS.
 */
export const CONNECTORS: ConnectorSummary[] = [
  {
    id: 'slack',
    display_name: 'Slack',
    icon: 'slack',
    category: 'Communication',
    auth_type: 'oauth',
    capabilities: ['send messages', 'list channels'],
  },
  {
    id: 'github',
    display_name: 'GitHub',
    icon: 'github',
    category: 'Developer Tools',
    auth_type: 'api_key',
    capabilities: ['create issues', 'list pull requests'],
  },
  {
    id: 'jira',
    display_name: 'Jira',
    icon: 'jira',
    category: 'Project Management',
    auth_type: 'api_key',
    capabilities: ['create tickets', 'search issues'],
  },
  {
    id: 'notion',
    display_name: 'Notion',
    icon: 'notion',
    category: 'Productivity',
    auth_type: 'api_key',
    capabilities: ['create pages', 'search workspace'],
  },
  {
    id: 'salesforce',
    display_name: 'Salesforce',
    icon: 'salesforce',
    category: 'CRM',
    auth_type: 'oauth',
    capabilities: ['create leads', 'run SOQL queries'],
  },
  {
    id: 'google_drive',
    display_name: 'Google Drive',
    icon: 'google_drive',
    category: 'Storage',
    auth_type: 'oauth',
    capabilities: ['list files', 'download files'],
  },
  {
    id: 'gmail',
    display_name: 'Gmail',
    icon: 'gmail',
    category: 'Communication',
    auth_type: 'oauth',
    capabilities: ['send email', 'search messages'],
  },
  {
    id: 'confluence',
    display_name: 'Confluence',
    icon: 'confluence',
    category: 'Productivity',
    auth_type: 'api_key',
    capabilities: ['create pages', 'search pages'],
  },
  {
    id: 'pagerduty',
    display_name: 'PagerDuty',
    icon: 'pagerduty',
    category: 'Incident Management',
    auth_type: 'api_key',
    capabilities: ['create incidents', 'list incidents'],
  },
  {
    id: 'hubspot',
    display_name: 'HubSpot',
    icon: 'hubspot',
    category: 'CRM',
    auth_type: 'api_key',
    capabilities: ['create contacts', 'search contacts'],
  },
  {
    id: 'stripe',
    display_name: 'Stripe',
    icon: 'stripe',
    category: 'Payments',
    auth_type: 'api_key',
    capabilities: ['create customers', 'list charges'],
  },
  {
    id: 'sendgrid',
    display_name: 'SendGrid',
    icon: 'sendgrid',
    category: 'Communication',
    auth_type: 'api_key',
    capabilities: ['send transactional email'],
  },
];

/** Group connectors by category for the picker UI. */
export function connectorsByCategory(): Record<string, ConnectorSummary[]> {
  const grouped: Record<string, ConnectorSummary[]> = {};
  for (const c of CONNECTORS) {
    (grouped[c.category] ??= []).push(c);
  }
  return grouped;
}
