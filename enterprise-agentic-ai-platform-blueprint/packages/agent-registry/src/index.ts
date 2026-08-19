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
} from './platform-registry-construct';

export {
  RegistryRecordConstruct,
  type RegistryRecordConstructProps,
} from './registry-record-construct';

export { grantRegistryConsumer, type RegistryConsumerGrantOptions } from './registry-consumer-grant';

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
} from './registry-record-spec';
