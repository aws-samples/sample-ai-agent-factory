/**
 * Tests for multi-framework adapter config builder.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { buildFrameworkAdapterConfig } from './multi-framework';

const base = {
  framework: 'langgraph' as const,
  gatewayUrl: 'https://gateway.example.com/a2a',
  targetName: 'demo',
  toolIds: ['tool-echo', 'tool-ping'],
  guardrailIdentifier: 'gd-12345',
  inferenceProfileArn: 'arn:aws:bedrock:us-west-2:111111111111:application-inference-profile/demo-primary',
};

describe('buildFrameworkAdapterConfig', () => {
  it('produces qualified tool names', () => {
    const c = buildFrameworkAdapterConfig(base);
    expect(c.toolNames).toEqual(['target-demo___tool-echo', 'target-demo___tool-ping']);
  });

  it('locks the MCP protocol version to 2025-06-18', () => {
    const c = buildFrameworkAdapterConfig(base);
    expect(c.mcpProtocolVersion).toBe('2025-06-18');
  });

  it('rejects HTTP gateway URLs', () => {
    expect(() => buildFrameworkAdapterConfig({ ...base, gatewayUrl: 'http://x' })).toThrow();
  });

  it('rejects empty guardrail', () => {
    expect(() => buildFrameworkAdapterConfig({ ...base, guardrailIdentifier: '' })).toThrow();
  });

  it('supports all three frameworks', () => {
    for (const framework of ['strands', 'langgraph', 'crewai'] as const) {
      const c = buildFrameworkAdapterConfig({ ...base, framework });
      expect(c.framework).toBe(framework);
    }
  });

  it('rejects unsupported frameworks', () => {
    expect(() =>
      buildFrameworkAdapterConfig({ ...base, framework: 'autogen' as any }),
    ).toThrow();
  });
});
