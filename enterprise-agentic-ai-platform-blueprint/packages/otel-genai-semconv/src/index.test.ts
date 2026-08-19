/**
 * Tests for OTel GenAI sem-conv helpers.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  buildRequestAttributes,
  buildResponseAttributes,
  validateRequestAttributes,
} from './index';

describe('buildRequestAttributes', () => {
  it('emits OTel-compliant gen_ai.* keys', () => {
    const a = buildRequestAttributes({
      operation: 'chat',
      system: 'aws.bedrock',
      modelId: 'anthropic.claude-sonnet-4-5-20250929-v1:0',
      tenantId: 'demo',
      agentId: 'primary',
      maxTokens: 1024,
      temperature: 0.7,
    });
    expect(a['gen_ai.operation.name']).toBe('chat');
    expect(a['gen_ai.system']).toBe('aws.bedrock');
    expect(a['gen_ai.request.model']).toContain('claude-sonnet-4-5');
    expect(a['gen_ai.request.max_tokens']).toBe(1024);
    expect(a['gen_ai.request.temperature']).toBe(0.7);
    expect(a['agenticai.tenant.id']).toBe('demo');
  });

  it('omits optional fields when not supplied', () => {
    const a = buildRequestAttributes({
      operation: 'chat',
      system: 'aws.bedrock',
      modelId: 'm',
      tenantId: 't',
      agentId: 'a',
    });
    expect('gen_ai.request.max_tokens' in a).toBe(false);
    expect('gen_ai.request.temperature' in a).toBe(false);
  });

  it('emits tool name for tool_call operation', () => {
    const a = buildRequestAttributes({
      operation: 'tool_call',
      system: 'aws.bedrock',
      modelId: 'm',
      tenantId: 't',
      agentId: 'a',
      toolName: 'target-demo___tool-echo',
    });
    expect(a['gen_ai.tool.name']).toBe('target-demo___tool-echo');
  });
});

describe('buildResponseAttributes', () => {
  it('emits gen_ai.usage.* and finish_reasons', () => {
    const a = buildResponseAttributes({
      modelId: 'm',
      inputTokens: 100,
      outputTokens: 50,
      finishReason: 'stop',
      usageMs: 320,
    });
    expect(a['gen_ai.usage.input_tokens']).toBe(100);
    expect(a['gen_ai.usage.output_tokens']).toBe(50);
    expect(a['gen_ai.response.finish_reasons']).toBe('stop');
    expect(a['agenticai.invocation.latency_ms']).toBe(320);
  });
});

describe('validateRequestAttributes', () => {
  it('passes for valid attribute maps', () => {
    expect(() => validateRequestAttributes({
      'gen_ai.operation.name': 'chat',
      'gen_ai.system': 'aws.bedrock',
      'gen_ai.request.model': 'm',
    })).not.toThrow();
  });
  it('throws on missing required fields', () => {
    expect(() => validateRequestAttributes({})).toThrow(/missing required/);
  });
  it('throws when types are wrong', () => {
    expect(() => validateRequestAttributes({
      'gen_ai.operation.name': 42,
      'gen_ai.system': 'aws.bedrock',
      'gen_ai.request.model': 'm',
    })).toThrow(/must be string/);
  });
});
