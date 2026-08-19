/**
 * @agenticai/platform-tool-catalogue
 *
 * Public export surface. See ./tool-catalogue.ts for the authoritative SSOT
 * and governing architectural decisions (Q1 / Q3 / Q5).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export {
  PLATFORM_TOOL_CATALOGUE,
  PLATFORM_TOOL_CATALOGUE_VERSION,
  validateToolSpec,
  resolveSubscribedTools,
  resolveTargetArn,
  composeCedarPolicyDocument,
} from './tool-catalogue';
export type { ToolId, ToolSpec } from './tool-catalogue';
