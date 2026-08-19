/**
 * cdk-context — read/write helpers for an agent repo's `cdk.context.json`.
 *
 * The CLI uses this as the single source of truth for what a workstream is
 * subscribed to, mirroring the synth-time invariants pinned in
 * `bin/agentic-ai-platform.ts`:
 *   - `agenticai/tenantId`
 *   - `agenticai/agentId`
 *   - `agenticai/subscribedRegistryRecords`  (v0.5.0 Registry mode)
 *   - `agenticai/d03RegistryId`              (required when above is set)
 *
 * No filesystem side-effects in this module — callers pass the parsed JSON
 * object in and get the (possibly mutated) JSON object back. That keeps the
 * helpers pure and unit-testable.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export const CTX_TENANT_ID = 'agenticai/tenantId';
export const CTX_AGENT_ID = 'agenticai/agentId';
export const CTX_SUBSCRIBED_RECORDS = 'agenticai/subscribedRegistryRecords';
export const CTX_REGISTRY_ID = 'agenticai/d03RegistryId';
export const CTX_PLATFORM_REGISTRY_ARN = 'agenticai/platformRegistryArn';

export interface AgenticAiContext {
  readonly [CTX_TENANT_ID]?: string;
  readonly [CTX_AGENT_ID]?: string;
  readonly [CTX_SUBSCRIBED_RECORDS]?: readonly string[];
  readonly [CTX_REGISTRY_ID]?: string;
  readonly [CTX_PLATFORM_REGISTRY_ARN]?: string;
  readonly [otherKey: string]: unknown;
}

/**
 * Read the agenticai-namespaced subset of a parsed cdk.context.json. Throws
 * when the input is not a plain object — guards the CLI against
 * accidentally consuming an array or null.
 */
export function readAgenticContext(parsed: unknown): AgenticAiContext {
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('readAgenticContext: cdk.context.json root must be a JSON object');
  }
  return parsed as AgenticAiContext;
}

/**
 * Append a Registry record id to the subscriptions list. Idempotent (no-op
 * on duplicate). Returns a NEW context object — does not mutate the input.
 */
export function appendSubscription(
  ctx: AgenticAiContext,
  recordId: string,
): AgenticAiContext {
  if (!/^[a-z][a-z0-9-]{1,63}$/.test(recordId)) {
    throw new Error(
      `appendSubscription: recordId must match /^[a-z][a-z0-9-]{1,63}$/; got '${recordId}'`,
    );
  }
  const existing = (ctx[CTX_SUBSCRIBED_RECORDS] ?? []) as readonly string[];
  if (existing.includes(recordId)) {
    return ctx;
  }
  return {
    ...ctx,
    [CTX_SUBSCRIBED_RECORDS]: [...existing, recordId],
  };
}

/**
 * Remove a record id from the subscriptions list. No-op if not present.
 */
export function removeSubscription(
  ctx: AgenticAiContext,
  recordId: string,
): AgenticAiContext {
  const existing = (ctx[CTX_SUBSCRIBED_RECORDS] ?? []) as readonly string[];
  if (!existing.includes(recordId)) {
    return ctx;
  }
  return {
    ...ctx,
    [CTX_SUBSCRIBED_RECORDS]: existing.filter((r) => r !== recordId),
  };
}

/**
 * Return the subscriptions list, or an empty array if unset. Always returns
 * a fresh array — callers can mutate freely.
 */
export function listSubscriptions(ctx: AgenticAiContext): string[] {
  return [...((ctx[CTX_SUBSCRIBED_RECORDS] ?? []) as readonly string[])];
}

/**
 * Validate that a context object has the minimum surface required to drive
 * a Registry-mode synth (tenantId, agentId, registryId, ≥1 subscription).
 * Returns a list of missing-key error messages — empty on success.
 */
export function validateForSynth(ctx: AgenticAiContext): string[] {
  const errors: string[] = [];
  if (typeof ctx[CTX_TENANT_ID] !== 'string' || (ctx[CTX_TENANT_ID] as string).length === 0) {
    errors.push(`missing context key: ${CTX_TENANT_ID}`);
  }
  if (typeof ctx[CTX_AGENT_ID] !== 'string' || (ctx[CTX_AGENT_ID] as string).length === 0) {
    errors.push(`missing context key: ${CTX_AGENT_ID}`);
  }
  if (
    typeof ctx[CTX_REGISTRY_ID] !== 'string' ||
    (ctx[CTX_REGISTRY_ID] as string).length === 0
  ) {
    errors.push(`missing context key: ${CTX_REGISTRY_ID}`);
  }
  const subs = listSubscriptions(ctx);
  if (subs.length === 0) {
    errors.push(`empty: ${CTX_SUBSCRIBED_RECORDS} — subscribe to at least one record`);
  }
  return errors;
}
