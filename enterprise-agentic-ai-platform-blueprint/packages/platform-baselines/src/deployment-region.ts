/**
 * Resolve the concrete Region for a CDK deployment.
 *
 * The CDK CLI's ambient Region must win over an optional explicit context
 * override. This prevents a repository-level context default from silently
 * synthesizing stacks in a different Region than the active pipeline or CLI
 * session. No implicit Region is safe for a multi-Region deployment.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

const AWS_REGION_CODE = /^[a-z]{2}(?:-[a-z0-9]+)+-\d+$/;

export function resolveDeploymentRegion(
  cdkDefaultRegion: unknown,
  configuredRegion: unknown,
): string {
  const source = cdkDefaultRegion !== undefined
    ? 'CDK_DEFAULT_REGION'
    : "context 'agenticai/defaultRegion'";
  const selected = cdkDefaultRegion !== undefined
    ? cdkDefaultRegion
    : configuredRegion;

  if (typeof selected !== 'string' || !AWS_REGION_CODE.test(selected)) {
    throw new Error(
      `A concrete AWS Region is required: set CDK_DEFAULT_REGION or context 'agenticai/defaultRegion'; ${source} was missing or invalid.`,
    );
  }
  return selected;
}
