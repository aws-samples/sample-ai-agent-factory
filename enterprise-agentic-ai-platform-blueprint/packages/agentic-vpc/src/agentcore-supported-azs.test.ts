/**
 * AgentCore supported-AZ contract tests.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  AGENTCORE_SUPPORTED_AVAILABILITY_ZONE_IDS,
  resolveAgentCoreSupportedAvailabilityZoneIds,
} from './agentcore-supported-azs';

describe('AgentCore supported Availability Zone IDs', () => {
  it('pins every governed Region to the documented AZ IDs', () => {
    expect(AGENTCORE_SUPPORTED_AVAILABILITY_ZONE_IDS).toEqual({
      'us-east-1': ['use1-az1', 'use1-az2', 'use1-az4'],
      'us-west-2': ['usw2-az1', 'usw2-az2', 'usw2-az3'],
      'eu-west-1': ['euw1-az1', 'euw1-az2', 'euw1-az3'],
    });
  });

  it('fails closed for an unreviewed Region', () => {
    expect(() =>
      resolveAgentCoreSupportedAvailabilityZoneIds('eu-central-1'),
    ).toThrow(/No reviewed AgentCore Availability Zone IDs/);
  });

  it('accepts an explicit reviewed override for an unreviewed Region', () => {
    expect(
      resolveAgentCoreSupportedAvailabilityZoneIds('eu-central-1', [
        'euc1-az1',
        'euc1-az2',
      ]),
    ).toEqual(['euc1-az1', 'euc1-az2']);
  });
});
