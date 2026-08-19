/**
 * @agenticai/otel-genai-semconv
 *
 * Pure-fn helpers that produce OTel-compliant span attribute maps for
 * GenAI inference invocations. Aligned to the OpenTelemetry "GenAI Semantic
 * Conventions" (`gen_ai.*` namespace, https://opentelemetry.io/docs/specs/semconv/gen-ai/).
 *
 * Bonus shippable (Z7-L). Pre-v0.4.0 the blueprint had no standardised
 * span attribute schema; tracing was free-form. This module fixes that
 * so observability dashboards, OAM consumers, and APM tools (Honeycomb,
 * Datadog, NR, Tempo) can correlate spans across multi-framework agents.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export type GenAiOperation = 'chat' | 'text_completion' | 'embeddings' | 'tool_call';

export type GenAiSystem = 'aws.bedrock' | 'anthropic' | 'openai';

export interface GenAiRequestSpanInput {
  readonly operation: GenAiOperation;
  readonly system: GenAiSystem;
  readonly modelId: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly maxTokens?: number;
  readonly temperature?: number;
  readonly topP?: number;
  readonly toolName?: string; // when operation === 'tool_call'
}

export interface GenAiResponseSpanInput {
  readonly modelId: string;
  readonly inputTokens: number;
  readonly outputTokens: number;
  readonly finishReason: 'stop' | 'length' | 'content_filter' | 'tool_calls' | 'error';
  readonly responseId?: string;
  readonly usageMs?: number;
}

/**
 * Span attributes for the request side. Keys match OTel `gen_ai.*` exactly.
 */
export function buildRequestAttributes(input: GenAiRequestSpanInput): Record<string, string | number> {
  const out: Record<string, string | number> = {
    'gen_ai.operation.name': input.operation,
    'gen_ai.system': input.system,
    'gen_ai.request.model': input.modelId,
    // AgenticAI-specific extensions, namespaced.
    'agenticai.tenant.id': input.tenantId,
    'agenticai.agent.id': input.agentId,
  };
  if (input.maxTokens !== undefined) out['gen_ai.request.max_tokens'] = input.maxTokens;
  if (input.temperature !== undefined) out['gen_ai.request.temperature'] = input.temperature;
  if (input.topP !== undefined) out['gen_ai.request.top_p'] = input.topP;
  if (input.toolName !== undefined) out['gen_ai.tool.name'] = input.toolName;
  return out;
}

/**
 * Span attributes for the response side.
 */
export function buildResponseAttributes(input: GenAiResponseSpanInput): Record<string, string | number> {
  const out: Record<string, string | number> = {
    'gen_ai.response.model': input.modelId,
    'gen_ai.response.finish_reasons': input.finishReason,
    'gen_ai.usage.input_tokens': input.inputTokens,
    'gen_ai.usage.output_tokens': input.outputTokens,
  };
  if (input.responseId) out['gen_ai.response.id'] = input.responseId;
  if (input.usageMs !== undefined) out['agenticai.invocation.latency_ms'] = input.usageMs;
  return out;
}

/**
 * Validate a span attribute map against the OTel sem-conv. Throws with
 * actionable messages on missing required keys / wrong types.
 */
export function validateRequestAttributes(attrs: Record<string, unknown>): void {
  const required = ['gen_ai.operation.name', 'gen_ai.system', 'gen_ai.request.model'];
  for (const k of required) {
    if (!(k in attrs)) throw new Error(`OTel GenAI sem-conv: missing required ${k}`);
    if (typeof attrs[k] !== 'string') throw new Error(`OTel GenAI sem-conv: ${k} must be string`);
  }
}
