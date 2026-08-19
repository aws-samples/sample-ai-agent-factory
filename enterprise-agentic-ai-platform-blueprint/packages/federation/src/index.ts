/**
 * @agenticai/federation
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-6 (federated mesh) +
 * Missing-7 (multi-framework) + Missing-9 (shared memory + conflict
 * resolution).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export {
  DEFAULT_DOMAIN_IDS,
  validateDomainId,
  buildDomainScopedSk,
  buildHomeDomainCondition,
} from './domain-scoping';
export {
  validateSharedMemoryOptions,
  buildSharedMemoryNamespacePath,
  describeConflictPolicy,
  type ConflictResolutionPolicy,
  type SharedMemoryOptions,
} from './shared-memory';
export {
  buildFrameworkAdapterConfig,
  type FrameworkId,
  type FrameworkAdapterConfig,
  type AdapterInputs,
} from './multi-framework';
