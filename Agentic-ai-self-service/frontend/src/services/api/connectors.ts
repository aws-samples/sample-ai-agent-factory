/**
 * Connector Catalog API domain module (Phase 3 Gap 3E).
 */

import { apiRequest } from './client';

// ============================================================================
// Types
// ============================================================================

export interface ConnectorSummary {
  id: string;
  display_name: string;
  icon: string;
  category: string;
  auth_type: 'oauth' | 'api_key';
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

// ============================================================================
// Connector Operations
// ============================================================================

/** List the pre-built connector catalog (auth-gated, public catalog). */
export async function listConnectors(): Promise<ConnectorSummary[]> {
  return apiRequest<ConnectorSummary[]>(`/api/connectors`);
}

/** Fetch one connector's detail (tool + credential schema). */
export async function getConnector(id: string): Promise<ConnectorDetail> {
  return apiRequest<ConnectorDetail>(
    `/api/connectors/${encodeURIComponent(id)}`
  );
}
