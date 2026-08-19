/**
 * @agenticai/agentcore-memory
 *
 * AgentCore Memory per spec §3.4:
 *   - CMK-encrypted (R-MEM-017 + §3.4.7 L4697-4698)
 *   - TTL ≤ 365d for short-term (R-MEM-015 / §3.4.2 L4596)
 *   - Actor scoping via `actorId` only — no real end-user identity (R-MEM-016 / §3.4.6)
 *   - Namespace template accepts only {actorId} / {memoryStrategyId} / {sessionId}
 *     — tenant/agent segments must be STATIC (spec §3.4.4 L4657-4660). This
 *     forces tenant onboarding to be deploy-time; documented in
 *     README section 11 (Choice architecture).
 *
 * The CfnMemory L1 for AgentCore lands in a Phase 5 follow-on when the CFN
 * resource type ships. For now the construct emits the CMK + retention
 * policy scaffolding so conformance tests can pin shape.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export { AgentCoreMemoryConstruct, type AgentCoreMemoryConstructProps } from './memory-construct';
export {
  buildMemoryNamespacePath,
  buildSharedMemoryNamespacePath,
  type MemoryNamespaceOptions,
  type SharedMemoryNamespaceOptions,
} from './namespace-template';
