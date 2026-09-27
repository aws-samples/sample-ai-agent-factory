/**
 * cdk-context — read/write helpers for an agent repo's `cdk.context.json`.
 *
 * The CLI uses this as the single source of truth for what a workstream is
 * subscribed to, mirroring the synth-time invariants pinned in
 * `bin/agentic-ai-platform.ts`:
 *   - `agenticai/tenantId`
 *   - `agenticai/agentId`
 *   - `agenticai/gaRegistryExpectedToolIds` (stable R2 tool subscriptions)
 *
 * Opaque RegistryRecord IDs are environment-specific and are resolved by the
 * Workload pipeline from versioned SSM parameters; developer repos never pin
 * them directly.
 *
 * No filesystem side-effects in this module — callers pass the parsed JSON
 * object in and get the (possibly mutated) JSON object back. That keeps the
 * helpers pure and unit-testable.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export const CTX_TENANT_ID = "agenticai/tenantId";
export const CTX_AGENT_ID = "agenticai/agentId";
export const CTX_SUBSCRIBED_RECORDS = "agenticai/gaRegistryExpectedToolIds";
export const CTX_PLATFORM_REGISTRY_ARN = "agenticai/platformRegistryArn";

export interface AgenticAiContext {
  readonly [CTX_TENANT_ID]?: string;
  readonly [CTX_AGENT_ID]?: string;
  readonly [CTX_SUBSCRIBED_RECORDS]?: readonly string[];
  readonly [CTX_PLATFORM_REGISTRY_ARN]?: string;
  readonly [otherKey: string]: unknown;
}

/**
 * Read the agenticai-namespaced subset of a parsed cdk.context.json. Throws
 * when the input is not a plain object — guards the CLI against
 * accidentally consuming an array or null.
 */
export function readAgenticContext(parsed: unknown): AgenticAiContext {
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(
      "readAgenticContext: cdk.context.json root must be a JSON object",
    );
  }
  return parsed as AgenticAiContext;
}

/**
 * Append a stable Registry tool id to the subscriptions list. Idempotent.
 */
export function appendSubscription(
  ctx: AgenticAiContext,
  toolId: string,
): AgenticAiContext {
  if (!/^[a-z][a-z0-9-]{1,63}$/.test(toolId)) {
    throw new Error(
      `appendSubscription: toolId must match /^[a-z][a-z0-9-]{1,63}$/; got '${toolId}'`,
    );
  }
  const existing = (ctx[CTX_SUBSCRIBED_RECORDS] ?? []) as readonly string[];
  if (existing.includes(toolId)) {
    return ctx;
  }
  return {
    ...ctx,
    [CTX_SUBSCRIBED_RECORDS]: [...existing, toolId],
  };
}

/** Remove a stable tool id from the subscriptions list. */
export function removeSubscription(
  ctx: AgenticAiContext,
  toolId: string,
): AgenticAiContext {
  const existing = (ctx[CTX_SUBSCRIBED_RECORDS] ?? []) as readonly string[];
  if (!existing.includes(toolId)) {
    return ctx;
  }
  return {
    ...ctx,
    [CTX_SUBSCRIBED_RECORDS]: existing.filter((value) => value !== toolId),
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
 * Validate the minimum developer context for R2 Workload pipeline synth:
 * tenantId, agentId, and at least one stable tool subscription.
 * Environment-specific Registry and record IDs are resolved by the pipeline.
 */
export function validateForSynth(ctx: AgenticAiContext): string[] {
  const errors: string[] = [];
  if (
    typeof ctx[CTX_TENANT_ID] !== "string" ||
    (ctx[CTX_TENANT_ID] as string).length === 0
  ) {
    errors.push(`missing context key: ${CTX_TENANT_ID}`);
  }
  if (
    typeof ctx[CTX_AGENT_ID] !== "string" ||
    (ctx[CTX_AGENT_ID] as string).length === 0
  ) {
    errors.push(`missing context key: ${CTX_AGENT_ID}`);
  }
  const subs = listSubscriptions(ctx);
  if (subs.length === 0) {
    errors.push(
      `empty: ${CTX_SUBSCRIBED_RECORDS} — subscribe to at least one tool`,
    );
  }
  return errors;
}
