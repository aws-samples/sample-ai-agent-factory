import { beforeEach, describe, expect, it, vi } from 'vitest';

const { authFetchMock } = vi.hoisted(() => ({
  authFetchMock: vi.fn(),
}));

vi.mock('../../auth/authFetch', () => ({
  authFetch: authFetchMock,
}));

import { createApiClient } from '../api';
import { addDeployAccount } from './admin';

const accountId = '123456789012';
const deploymentRoleArn =
  `arn:aws:iam::${accountId}:role/AgentCoreFlowsDeploymentRole`;
const runtimeRoleArn =
  `arn:aws:iam::${accountId}:role/AgentCoreFlowsRuntimeRole`;
const mcpRuntimeRoleArn =
  `arn:aws:iam::${accountId}:role/AgentCoreFlowsMCPRuntimeRole`;
const harnessRoleArn =
  `arn:aws:iam::${accountId}:role/AgentCoreFlowsHarnessRole`;
const artifactBucket = 'customer-agent-runtime-artifacts';

function successfulRegistration(): Response {
  return new Response(
    JSON.stringify({
      account_id: accountId,
      runtime_role_arn: runtimeRoleArn,
      mcp_runtime_role_arn: mcpRuntimeRoleArn,
      harness_role_arn: harnessRoleArn,
      artifact_bucket: artifactBucket,
      validated: true,
    }),
    {
      status: 200,
      headers: { 'content-type': 'application/json' },
    },
  );
}

function submittedBody(callIndex = 0): Record<string, unknown> {
  const options = authFetchMock.mock.calls[callIndex]?.[1] as
    | RequestInit
    | undefined;
  expect(options?.method).toBe('POST');
  expect(typeof options?.body).toBe('string');
  return JSON.parse(String(options?.body)) as Record<string, unknown>;
}

describe('cross-account deployment target registration', () => {
  beforeEach(() => {
    authFetchMock.mockReset();
  });

  it('sends the dedicated MCP execution role through the legacy UI client', async () => {
    authFetchMock.mockResolvedValueOnce(successfulRegistration());

    await createApiClient('https://api.example').addDeployAccount(
      accountId,
      deploymentRoleArn,
      'eu-west-1',
      runtimeRoleArn,
      mcpRuntimeRoleArn,
      harnessRoleArn,
      artifactBucket,
    );

    expect(authFetchMock.mock.calls[0]?.[0]).toBe(
      'https://api.example/api/admin/deploy-targets/accounts',
    );
    expect(submittedBody()).toEqual({
      account_id: accountId,
      role_arn: deploymentRoleArn,
      region: 'eu-west-1',
      runtime_role_arn: runtimeRoleArn,
      mcp_runtime_role_arn: mcpRuntimeRoleArn,
      harness_role_arn: harnessRoleArn,
      artifact_bucket: artifactBucket,
    });
  });

  it('keeps the domain API and legacy client on the same role contract', async () => {
    authFetchMock.mockResolvedValueOnce(successfulRegistration());

    await addDeployAccount(
      accountId,
      deploymentRoleArn,
      'eu-west-1',
      runtimeRoleArn,
      mcpRuntimeRoleArn,
      harnessRoleArn,
      artifactBucket,
    );

    expect(authFetchMock.mock.calls[0]?.[0]).toBe(
      '/api/admin/deploy-targets/accounts',
    );
    expect(submittedBody()).toEqual({
      account_id: accountId,
      role_arn: deploymentRoleArn,
      region: 'eu-west-1',
      runtime_role_arn: runtimeRoleArn,
      mcp_runtime_role_arn: mcpRuntimeRoleArn,
      harness_role_arn: harnessRoleArn,
      artifact_bucket: artifactBucket,
    });
  });
});
