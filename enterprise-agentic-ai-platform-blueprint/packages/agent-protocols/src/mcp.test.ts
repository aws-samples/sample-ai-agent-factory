/**
 * Tests for MCP-native helpers.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  MCP_PROTOCOL_HEADER,
  MCP_PROTOCOL_VERSION,
  isQualifiedToolName,
  qualifyToolName,
} from './mcp';

describe('MCP helpers', () => {
  it('locks the protocol version to 2025-06-18 (live-verified)', () => {
    expect(MCP_PROTOCOL_VERSION).toBe('2025-06-18');
    expect(MCP_PROTOCOL_HEADER).toBe('MCP-Protocol-Version');
  });

  it('qualifies tool names with the AgentCore convention', () => {
    expect(qualifyToolName('demo', 'tool-echo')).toBe('target-demo___tool-echo');
  });

  it('rejects malformed target names', () => {
    expect(() => qualifyToolName('demo with space', 'tool-echo')).toThrow();
  });

  it('rejects malformed tool ids', () => {
    expect(() => qualifyToolName('demo', 'Tool_Echo')).toThrow();
  });

  it('isQualifiedToolName matches the pattern', () => {
    expect(isQualifiedToolName('target-demo___tool-echo')).toBe(true);
    expect(isQualifiedToolName('tool-echo')).toBe(false);
    expect(isQualifiedToolName('target-demo--tool-echo')).toBe(false);
  });
});
