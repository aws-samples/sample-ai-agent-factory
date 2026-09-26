/**
 * Fail closed for components whose Bedrock path is not in the EMEA support
 * envelope. These constructs use direct Bedrock calls and/or EU cross-Region
 * inference profiles; neither has independent residency and routing evidence
 * under the single-Region `eu-west-1` boundary.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export function assertEmeaProfilePathSupported(
  region: string,
  component: string,
): void {
  if (region.startsWith('eu-')) {
    throw new Error(
      `${component} is not supported in EMEA: its direct-Bedrock or cross-Region profile path lacks independent residency evidence. Use the AgentCore Gateway generated-agent path.`,
    );
  }
}
