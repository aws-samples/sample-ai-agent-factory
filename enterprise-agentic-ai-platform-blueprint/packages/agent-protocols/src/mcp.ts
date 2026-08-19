/**
 * MCP-native helpers.
 *
 * The AgentCore Gateway speaks MCP natively over StreamableHTTP. Two
 * conventions matter for callers, both validated here:
 *   1. The MCP-Protocol-Version header MUST be `2025-06-18` after the
 *      initialize handshake — otherwise AgentCore defaults to 2025-03-26
 *      and rejects calls (live-verified 2026-05-05; see CLAUDE.md
 *      landmines).
 *   2. Tool names appear in tools/list as `target-<target-name>___<tool-id>`.
 *      Callers must use the qualified name verbatim in tools/call.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export const MCP_PROTOCOL_VERSION = '2025-06-18';

export const MCP_PROTOCOL_HEADER = 'MCP-Protocol-Version';

const QUALIFIED_TOOL_NAME = /^target-[a-zA-Z0-9-]{1,64}___[a-z0-9-]{3,64}$/;

export function qualifyToolName(targetName: string, toolId: string): string {
  if (!/^[a-zA-Z0-9-]{1,64}$/.test(targetName)) {
    throw new Error(`MCP target name must match [a-zA-Z0-9-]{1,64}: ${targetName}`);
  }
  if (!/^[a-z0-9-]{3,64}$/.test(toolId)) {
    throw new Error(`MCP tool id must be kebab-case 3-64 chars: ${toolId}`);
  }
  return `target-${targetName}___${toolId}`;
}

export function isQualifiedToolName(name: string): boolean {
  return QUALIFIED_TOOL_NAME.test(name);
}
