/**
 * @agenticai/agent-registry — public export surface.
 *
 * AWS Bedrock AgentCore Registry constructs + helpers used by the platform
 * stack to provision the org-wide registry, by per-workstream Gateway synth
 * to resolve subscriptions, and by the developer CLI to drive the publish /
 * search / approve workflow.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export {
  PlatformRegistryConstruct,
  type PlatformRegistryConstructProps,
} from "./platform-registry-construct";

export {
  RegistryRecordConstruct,
  type RegistryRecordConstructProps,
} from "./registry-record-construct";

export {
  grantRegistryConsumer,
  type RegistryConsumerGrantOptions,
} from "./registry-consumer-grant";

export {
  validateRegistryRecordSpec,
  resolveGatewayTargetArn,
  renderMcpDescriptorPayload,
  renderA2aDescriptorPayload,
  toolSpecToRegistryRecordSpec,
  type RegistryRecordSpec,
  type McpRegistryRecordSpec,
  type A2aRegistryRecordSpec,
  type AgentSkillsRegistryRecordSpec,
  type CustomRegistryRecordSpec,
  type RegistryRecordId,
  type RegistryRecordStatus,
  type RegistryInboundAuthType,
} from "./registry-record-spec";

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
