/**
 * AgentCore VPC connectivity is available only in documented AZ IDs.
 *
 * Source (verified 2026-09-26):
 * https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html
 *
 * Keep every approved deployment Region explicit. Unknown Regions return
 * undefined so callers must supply `supportedAvailabilityZoneIds` rather than
 * silently emitting an unfiltered Runtime subnet set.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export const AGENTCORE_SUPPORTED_AVAILABILITY_ZONE_IDS: Readonly<
  Record<string, readonly string[]>
> = {
  'us-east-1': ['use1-az1', 'use1-az2', 'use1-az4'],
  'us-west-2': ['usw2-az1', 'usw2-az2', 'usw2-az3'],
  'eu-west-1': ['euw1-az1', 'euw1-az2', 'euw1-az3'],
} as const;

export function resolveAgentCoreSupportedAvailabilityZoneIds(
  region: string,
  configured?: readonly string[],
): readonly string[] {
  const resolved = configured ?? AGENTCORE_SUPPORTED_AVAILABILITY_ZONE_IDS[region];
  if (!resolved || resolved.length === 0) {
    throw new Error(
      `No reviewed AgentCore Availability Zone IDs are configured for ${region}; supply supportedAvailabilityZoneIds explicitly.`,
    );
  }
  return resolved;
}
