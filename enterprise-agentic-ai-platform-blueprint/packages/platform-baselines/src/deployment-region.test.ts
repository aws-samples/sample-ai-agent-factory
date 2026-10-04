/**
 * Deployment Region contract tests.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { resolveDeploymentRegion } from './deployment-region';

describe('resolveDeploymentRegion', () => {
  it('prefers the ambient CDK Region over repository or command context', () => {
    expect(resolveDeploymentRegion('eu-west-1', 'us-west-2')).toBe('eu-west-1');
  });

  it('accepts an explicit context Region when CDK_DEFAULT_REGION is absent', () => {
    expect(resolveDeploymentRegion(undefined, 'eu-west-1')).toBe('eu-west-1');
  });

  it.each([
    [undefined, undefined],
    ['', 'eu-west-1'],
    ['EU-WEST-1', undefined],
    [undefined, 'not-a-region'],
  ])('fails closed for missing or malformed inputs (%p, %p)', (ambient, configured) => {
    expect(() => resolveDeploymentRegion(ambient, configured)).toThrow(
      /concrete AWS Region is required/,
    );
  });
});
