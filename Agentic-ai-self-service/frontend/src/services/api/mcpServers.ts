/**
 * MCP Server Catalog API domain module.
 * Verified external MCP-server catalog (browsable in the Registry UI).
 */

import { apiRequest } from './client';

// ============================================================================
// Types
// ============================================================================

export interface McpServerSummary {
  id: string;
  display_name: string;
  publisher: string;
  category: string;
  /** Integration tier: direct-none | direct-apikey | direct-oauth | adapter-3lo | adapter-stdio */
  tier: string;
  /** live | docs | community */
  verified: string;
  auth_type: string;
  live_testable: boolean;
  endpoint?: string | null;
}

export interface McpServerDetail extends McpServerSummary {
  credentials_needed: string;
  example_tools: string[];
  api_key_descriptor?: Record<string, unknown> | null;
  oauth_descriptor?: Record<string, unknown> | null;
}

// ============================================================================
// MCP Server Operations
// ============================================================================

/** List the verified external MCP-server catalog (registry:read). */
export async function listMcpServers(): Promise<McpServerSummary[]> {
  return apiRequest<McpServerSummary[]>(`/api/mcp-servers`);
}

/** Fetch one MCP server's detail (endpoint/auth/tools). */
export async function getMcpServer(serverId: string): Promise<McpServerDetail> {
  return apiRequest<McpServerDetail>(
    `/api/mcp-servers/${encodeURIComponent(serverId)}`
  );
}
