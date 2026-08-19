/**
 * Unit tests for @agenticai/agent-registry.
 *
 * Pins the typed RegistryRecordSpec validator, the descriptor-payload
 * renderer, and the legacy-ToolSpec-to-record mapper. CDK construct synth is
 * exercised by `tests/conformance/phase-22-agent-registry.test.ts`.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  PLATFORM_TOOL_CATALOGUE,
  type ToolSpec,
} from '@agenticai/platform-tool-catalogue';

import {
  resolveGatewayTargetArn,
  renderMcpDescriptorPayload,
  renderA2aDescriptorPayload,
  toolSpecToRegistryRecordSpec,
  validateRegistryRecordSpec,
  type McpRegistryRecordSpec,
  type A2aRegistryRecordSpec,
} from './index';

const validMcp: McpRegistryRecordSpec = {
  recordId: 'tool-echo',
  descriptorType: 'MCP',
  description: 'Echo tool',
  ownerTeam: 'platform-ai',
  costCentre: 'platform',
  gatewayTargetArn:
    'arn:aws:lambda:us-east-1:${PLATFORM_ACCOUNT_ID}:function:agenticai-d03-tool-echo:PROD',
  cedarPolicy: 'permit(principal, action == Action::"InvokeTool", resource == Tool::"tool-echo");',
};

describe('validateRegistryRecordSpec — MCP', () => {
  it('accepts a well-formed MCP record', () => {
    expect(() => validateRegistryRecordSpec(validMcp)).not.toThrow();
  });

  it('rejects an MCP record whose gatewayTargetArn lacks an alias suffix', () => {
    const bad: McpRegistryRecordSpec = {
      ...validMcp,
      gatewayTargetArn:
        'arn:aws:lambda:us-east-1:${PLATFORM_ACCOUNT_ID}:function:agenticai-d03-tool-echo',
    };
    expect(() => validateRegistryRecordSpec(bad)).toThrow(/alias/i);
  });

  it('rejects a non-12-digit targetAccountId', () => {
    const bad: McpRegistryRecordSpec = {
      ...validMcp,
      targetAccountId: '1234',
    };
    expect(() => validateRegistryRecordSpec(bad)).toThrow(/12 digits/);
  });

  it('rejects a cedarPolicy missing permit()', () => {
    const bad: McpRegistryRecordSpec = {
      ...validMcp,
      cedarPolicy: 'forbid(principal, action, resource);',
    };
    expect(() => validateRegistryRecordSpec(bad)).toThrow(/permit/);
  });

  it('rejects a recordId that is not kebab-case', () => {
    const bad: McpRegistryRecordSpec = { ...validMcp, recordId: 'NotKebab' };
    expect(() => validateRegistryRecordSpec(bad)).toThrow(/recordId/);
  });
});

describe('validateRegistryRecordSpec — A2A', () => {
  const validA2a: A2aRegistryRecordSpec = {
    recordId: 'agent-fulfilment',
    descriptorType: 'A2A',
    description: 'Peer fulfilment agent',
    ownerTeam: 'retail',
    costCentre: 'retail',
    a2aEndpointUrl: 'https://agent-runtime/a2a',
    cedarPolicy: 'permit(principal, action == Action::"InvokeAgent", resource);',
  };

  it('accepts a well-formed A2A record', () => {
    expect(() => validateRegistryRecordSpec(validA2a)).not.toThrow();
  });

  it('rejects an http (non-https) endpoint', () => {
    const bad: A2aRegistryRecordSpec = {
      ...validA2a,
      a2aEndpointUrl: 'http://agent-runtime/a2a',
    };
    expect(() => validateRegistryRecordSpec(bad)).toThrow(/https/);
  });
});

describe('resolveGatewayTargetArn', () => {
  it('substitutes ${PLATFORM_ACCOUNT_ID} when targetAccountId is undefined', () => {
    const arn = resolveGatewayTargetArn(validMcp, '111111111111');
    expect(arn).toBe(
      'arn:aws:lambda:us-east-1:111111111111:function:agenticai-d03-tool-echo:PROD',
    );
    expect(arn).not.toContain('${PLATFORM_ACCOUNT_ID}');
  });

  it('uses targetAccountId literally when present', () => {
    const arn = resolveGatewayTargetArn(
      { ...validMcp, targetAccountId: '999999999999' },
      '111111111111',
    );
    expect(arn).toContain('999999999999');
    expect(arn).not.toContain('111111111111');
  });
});

describe('renderMcpDescriptorPayload', () => {
  // Live-verified 2026-07-02: descriptors = { mcp: { server, tools } } with
  // JSON-string inlineContent. Pins the real CreateRegistryRecord contract.
  it('emits the mcp{server,tools} structure with schema-valid JSON inlineContent', () => {
    const payload = renderMcpDescriptorPayload(validMcp);
    expect(payload.descriptorType).toBe('MCP');
    const mcp = (payload.descriptors as Record<string, any>).mcp;
    expect(mcp).toBeDefined();
    expect(typeof mcp.server.inlineContent).toBe('string');
    expect(typeof mcp.tools.inlineContent).toBe('string');
    // Server document is the strict MCP shape { name, description, version }.
    const server = JSON.parse(mcp.server.inlineContent);
    expect(server.name).toBe(`agenticai/${validMcp.recordId}`);
    expect(server.version).toBe('1.0.0');
    expect(server.transport).toBeUndefined();
    expect(server.metadata).toBeUndefined();
    // Tools document carries the tool list with input schema.
    const tools = JSON.parse(mcp.tools.inlineContent);
    expect(Array.isArray(tools.tools)).toBe(true);
    expect(tools.tools[0].name).toBe(validMcp.recordId);
    expect(tools.tools[0].inputSchema).toBeDefined();
  });

  it('uses default schema versions 2025-12-11 (server) + 2024-11-05 (tools) when omitted', () => {
    const payload = renderMcpDescriptorPayload(validMcp);
    const mcp = (payload.descriptors as Record<string, any>).mcp;
    expect(mcp.server.schemaVersion).toBe('2025-12-11');
    // Live-verified: AgentCore accepts tools protocolVersion '2024-11-05'.
    expect(mcp.tools.protocolVersion).toBe('2024-11-05');
    // Server document must be the strict MCP shape — no extra keys.
    const server = JSON.parse(mcp.server.inlineContent);
    expect(Object.keys(server).sort()).toEqual(['description', 'name', 'version']);
  });
});

describe('renderA2aDescriptorPayload', () => {
  it('emits the a2a{agentCard} structure with JSON inlineContent carrying the url', () => {
    const payload = renderA2aDescriptorPayload({
      recordId: 'agent-fulfilment',
      descriptorType: 'A2A',
      description: 'Peer fulfilment agent',
      ownerTeam: 'retail',
      costCentre: 'retail',
      a2aEndpointUrl: 'https://agent-runtime/a2a',
      cedarPolicy: 'permit(principal, action == Action::"InvokeAgent", resource);',
    });
    expect(payload.descriptorType).toBe('A2A');
    const a2a = (payload.descriptors as Record<string, any>).a2a;
    expect(a2a.agentCard.schemaVersion).toBe('0.3');
    expect(typeof a2a.agentCard.inlineContent).toBe('string');
    const card = JSON.parse(a2a.agentCard.inlineContent);
    expect(card.url).toBe('https://agent-runtime/a2a');
  });
});

describe('toolSpecToRegistryRecordSpec', () => {
  it('maps a lambda ToolSpec to an MCP record', () => {
    const ts: ToolSpec = PLATFORM_TOOL_CATALOGUE['tool-echo'];
    const rec = toolSpecToRegistryRecordSpec(ts);
    expect(rec.descriptorType).toBe('MCP');
    expect(rec.recordId).toBe('tool-echo');
    if (rec.descriptorType !== 'MCP') {
      throw new Error('expected MCP');
    }
    expect(rec.gatewayTargetArn).toBe(ts.targetArn);
    expect(rec.cedarPolicy).toBe(ts.cedarPolicy);
    expect(rec.inputSchema).toEqual(ts.inputSchema);
  });

  it('maps an agent-a2a ToolSpec to an A2A record (smoke)', () => {
    const ts: ToolSpec = {
      toolId: 'agent-fulfilment',
      toolType: 'agent-a2a',
      targetArn:
        'arn:aws:lambda:us-east-1:${PLATFORM_ACCOUNT_ID}:function:placeholder:PROD',
      cedarPolicy: 'permit(principal, action == Action::"InvokeAgent", resource);',
      ownerTeam: 'retail',
      costCentre: 'retail',
      description: 'Peer fulfilment agent',
      approvalStatus: 'approved',
      a2aEndpointUrl: 'https://agent-runtime/a2a',
    };
    const rec = toolSpecToRegistryRecordSpec(ts);
    expect(rec.descriptorType).toBe('A2A');
    if (rec.descriptorType !== 'A2A') {
      throw new Error('expected A2A');
    }
    expect(rec.a2aEndpointUrl).toBe('https://agent-runtime/a2a');
  });

  it('round-trips every approved entry in PLATFORM_TOOL_CATALOGUE through validate()', () => {
    for (const ts of Object.values(PLATFORM_TOOL_CATALOGUE)) {
      if (ts.approvalStatus === 'deprecated') continue;
      const rec = toolSpecToRegistryRecordSpec(ts);
      expect(() => validateRegistryRecordSpec(rec)).not.toThrow();
    }
  });
});
