/**
 * Shared-memory namespace + conflict-resolution policy.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-9.
 *
 * The existing AgentCore Memory namespace template shape (`tenantId/agentId/
 * {actorId}/{memoryStrategyId}/{sessionId}`) is per-tenant + per-agent. The
 * federated mesh sometimes needs a *shared* namespace that two agents in the
 * same domain (or two domains via a defined contract) can read and write.
 *
 * Spec §3.4.4 only allows three template variables at runtime — the shared
 * namespace path is therefore static + dynamic-tail-suffixed, same as the
 * regular namespace.
 *
 * Conflict resolution is deliberately enumerated (not free-form) so each
 * agent reads the same expectations.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { validateDomainId } from './domain-scoping';

export type ConflictResolutionPolicy = 'last-write-wins' | 'merge-array' | 'human-arbitrate-via-hitl';

const SEGMENT = /^[a-z0-9-]+$/;
const TOPIC_SEGMENT = /^[a-z0-9-]{3,64}$/;

export interface SharedMemoryOptions {
  readonly domainId: string;
  readonly topicId: string;
  readonly conflictPolicy: ConflictResolutionPolicy;
  /** Optional dynamic-tail. Defaults to `{actorId}/{sessionId}` (no strategy split). */
  readonly dynamicTail?: string;
}

export function validateSharedMemoryOptions(o: SharedMemoryOptions): void {
  validateDomainId(o.domainId);
  if (!TOPIC_SEGMENT.test(o.topicId)) {
    throw new Error(`Shared-memory topicId must match ${TOPIC_SEGMENT} (got '${o.topicId}').`);
  }
  if (!['last-write-wins', 'merge-array', 'human-arbitrate-via-hitl'].includes(o.conflictPolicy)) {
    throw new Error(`Invalid conflict policy: ${o.conflictPolicy}`);
  }
  const tail = o.dynamicTail ?? '{actorId}/{sessionId}';
  if (!/^(\{actorId\}|\{memoryStrategyId\}|\{sessionId\}|\/)+$/.test(tail)) {
    throw new Error(`Shared-memory dynamicTail uses non-allowed template vars: ${tail}`);
  }
}

export function buildSharedMemoryNamespacePath(o: SharedMemoryOptions): string {
  validateSharedMemoryOptions(o);
  const tail = o.dynamicTail ?? '{actorId}/{sessionId}';
  return `shared/${o.domainId}/${o.topicId}/${tail}`;
}

/**
 * Returns a short JSON-encoded conflict-resolution descriptor for the agent
 * runtime to attach to memory-write requests. Keeps the SSOT here so two
 * agents in the same domain agree on semantics.
 */
export function describeConflictPolicy(p: ConflictResolutionPolicy): { id: ConflictResolutionPolicy; rule: string; humanFallback: boolean } {
  switch (p) {
    case 'last-write-wins':
      return { id: p, rule: 'Newest emittedAt wins. Older writes are silently overwritten.', humanFallback: false };
    case 'merge-array':
      return { id: p, rule: 'Array values are concatenated and de-duped by value identity.', humanFallback: false };
    case 'human-arbitrate-via-hitl':
      return { id: p, rule: 'On conflict, pause the writer agent and route to HITL approver.', humanFallback: true };
  }
  // exhaustive
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const _exhaustive: never = p;
  throw new Error(`Unhandled conflict policy: ${_exhaustive}`);
}

// Silence unused segment regex (kept for future extension).
void SEGMENT;
