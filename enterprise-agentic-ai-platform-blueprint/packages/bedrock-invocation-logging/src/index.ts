/**
 * @agenticai/bedrock-invocation-logging
 *
 * Enables Bedrock Model Invocation Logging per spec §2.4.7 (R-BED-036..038).
 * Logs stay local to the workload account; only metadata ships centrally
 * via OAM in Phase 6.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export {
  BedrockInvocationLoggingConstruct,
  type BedrockInvocationLoggingProps,
} from './invocation-logging-construct';
