/**
 * Product-owned MCP discovery and tool invocation.
 *
 * The browser supplies only a deployment id, a selected tool name, and JSON
 * arguments. Runtime ARN/account/region/protocol and MCP routing metadata are
 * resolved and constrained by the backend.
 */

import { apiRequest } from './client';

export interface McpToolDescriptor {
  name: string;
  title?: string;
  description?: string;
  inputSchema: Record<string, unknown>;
  outputSchema?: Record<string, unknown>;
  annotations?: Record<string, unknown>;
}

export interface McpToolsResult {
  protocolVersion: string;
  sessionId?: string;
  serverInfo: {
    name: string;
    version: string;
  };
  tools: McpToolDescriptor[];
}

export interface McpToolCallResult {
  protocolVersion: string;
  sessionId?: string;
  content: Record<string, unknown>[];
  structuredContent?: unknown;
  isError: boolean;
}

export async function discoverMcpTools(
  deploymentId: string,
  signal?: AbortSignal,
): Promise<McpToolsResult> {
  return apiRequest<McpToolsResult>(
    '/api/test-mcp-runtime/tools',
    {
      method: 'POST',
      body: JSON.stringify({ deploymentId }),
      signal,
    },
  );
}

export async function callMcpTool(
  deploymentId: string,
  toolName: string,
  args: Record<string, unknown>,
  sessionId?: string,
  signal?: AbortSignal,
): Promise<McpToolCallResult> {
  return apiRequest<McpToolCallResult>(
    '/api/test-mcp-runtime/call',
    {
      method: 'POST',
      body: JSON.stringify({
        deploymentId,
        toolName,
        arguments: args,
        sessionId,
      }),
      signal,
    },
  );
}
