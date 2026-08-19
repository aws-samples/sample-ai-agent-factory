/**
 * Namespace template builder — enforces spec §3.4.4 L4657-4660.
 *
 * Only these template variables are accepted at runtime by AgentCore Memory:
 *   {actorId}
 *   {memoryStrategyId}
 *   {sessionId}
 *
 * Any tenant / agent / environment path segment MUST be static (resolved at
 * synth time, not at runtime). This function emits a well-formed namespace
 * path with the static segments baked in, with the three dynamic vars appended.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export interface MemoryNamespaceOptions {
  /** Tenant / application id. Must be `[a-z0-9-]+`. */
  readonly tenantId: string;
  /** Agent id. Must be `[a-z0-9-]+`. */
  readonly agentId: string;
  /** Environment ('nonprod' | 'prod' | ...). Static path segment. */
  readonly envName: string;
  /**
   * Optional trailing dynamic-template segments. Defaults to the
   * three-variable canonical shape per spec §3.4.4 L4657-4660.
   */
  readonly dynamicTail?: string;
}

const SEGMENT_RE = /^[a-z0-9-]+$/;

export function buildMemoryNamespacePath(opts: MemoryNamespaceOptions): string {
  for (const [key, value] of Object.entries({
    tenantId: opts.tenantId,
    agentId: opts.agentId,
    envName: opts.envName,
  })) {
    if (!SEGMENT_RE.test(value)) {
      throw new Error(
        `Memory namespace segment '${key}' must match ${SEGMENT_RE} (got '${value}').`,
      );
    }
  }

  const tail = opts.dynamicTail ?? '{actorId}/{memoryStrategyId}/{sessionId}';

  // Validate the dynamic tail uses only the three allowed template variables.
  const allowedVars = /^(\{actorId\}|\{memoryStrategyId\}|\{sessionId\}|\/)+$/;
  if (!allowedVars.test(tail)) {
    throw new Error(
      `Memory namespace dynamic tail must use only {actorId}, {memoryStrategyId}, {sessionId} separated by '/'. Got '${tail}'.`,
    );
  }

  return `agenticai/${opts.envName}/${opts.tenantId}/${opts.agentId}/${tail}`;
}

/**
 * Z7-J: shared-namespace path. Federated mesh agents within the same domain
 * write to a shared topic; conflict resolution is enforced by the writer.
 *
 *   shared/<envName>/<domainId>/<topicId>/<dynamicTail>
 *
 * Same template-variable rules as the per-tenant namespace.
 */
export interface SharedMemoryNamespaceOptions {
  readonly envName: string;
  readonly domainId: string;     // matches @agenticai/federation domain id rules: [a-z][a-z0-9-]{2,31}
  readonly topicId: string;      // [a-z0-9-]{3,64}
  readonly dynamicTail?: string;
}

const DOMAIN_RE = /^[a-z][a-z0-9-]{2,31}$/;
const TOPIC_RE = /^[a-z0-9-]{3,64}$/;

export function buildSharedMemoryNamespacePath(opts: SharedMemoryNamespaceOptions): string {
  if (!SEGMENT_RE.test(opts.envName)) {
    throw new Error(`Shared memory envName must match ${SEGMENT_RE} (got '${opts.envName}').`);
  }
  if (!DOMAIN_RE.test(opts.domainId)) {
    throw new Error(`Shared memory domainId must match ${DOMAIN_RE} (got '${opts.domainId}').`);
  }
  if (!TOPIC_RE.test(opts.topicId)) {
    throw new Error(`Shared memory topicId must match ${TOPIC_RE} (got '${opts.topicId}').`);
  }
  const tail = opts.dynamicTail ?? '{actorId}/{sessionId}';
  const allowedVars = /^(\{actorId\}|\{memoryStrategyId\}|\{sessionId\}|\/)+$/;
  if (!allowedVars.test(tail)) {
    throw new Error(
      `Shared memory dynamic tail must use only {actorId}, {memoryStrategyId}, {sessionId} separated by '/'. Got '${tail}'.`,
    );
  }
  return `shared/${opts.envName}/${opts.domainId}/${opts.topicId}/${tail}`;
}
