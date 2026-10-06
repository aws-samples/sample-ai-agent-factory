/**
 * @agenticai/agent-registry — public export surface.
 *
 * AWS Agent Registry (GA) constructs and helpers. `GaPlatformRegistryConstruct`
 * provisions the platform-owned registry via the native
 * `AWS::AgentRegistry::Registry` resource; `GaPlatformToolsConstruct` owns the
 * Lambda tool aliases it points at; `ga-registry-consumer-context` carries
 * resolved records into per-workstream Gateway synth.
 *
 * The preview `bedrock-agentcore` Registry constructs were removed on
 * 2026-10-06 — AWS ended preview Registry support on 2026-09-17. See
 * docs/AGENT_REGISTRY_GA_MIGRATION.md.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export {
  GaPlatformRegistryConstruct,
  buildGaToolGovernanceDocument,
  type GaPlatformRegistryConstructProps,
  type GaPlatformRegistryTags,
  type GaToolGovernanceDocument,
} from "./ga-platform-registry-construct";

export {
  GA_REGISTRY_CONSUMER_CONTEXT_SCHEMA,
  parseGaRegistryConsumerContext,
  type GaResolvedRegistryRecord,
  type GaRegistryConsumerContext,
  type GaRegistryConsumerExpectation,
} from "./ga-registry-consumer-context";

export {
  GaPlatformToolsConstruct,
  type GaPlatformToolsConstructProps,
} from "./ga-platform-tools-construct";

export {
  AgentBuilderInspectRole,
  AGENT_BUILDER_INSPECT_ACTIONS,
  AGENT_BUILDER_INSPECT_RUNTIME_ACTIONS,
  AGENT_BUILDER_INSPECT_FORBIDDEN_FRAGMENTS,
  type AgentBuilderInspectRoleProps,
} from "./agent-builder-inspect-role";

export {
  evaluateRegistrationRequest,
  changesApprovedReference,
  type RegistrationAction,
  type RegistrarRole,
  type ApprovedReferences,
  type RegistrationPrincipal,
  type RegistrationRecordState,
  type RegistrationRequest,
  type RegistrationDecision,
} from "./agent-registration-api";
