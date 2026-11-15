/**
 * The version contract of the workspace policy API module: the version token rides on `ETag` and is
 * opaque, a save names it (`If-Match`) or asks for a create (`If-None-Match: *`), a `412` is a conflict
 * the panel can recover from, and a response without a version is refused rather than handed out.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const { authFetchMock } = vi.hoisted(() => ({
  authFetchMock: vi.fn(),
}));

vi.mock('../../auth/authFetch', () => ({
  authFetch: authFetchMock,
}));

import {
  currentVersionOf,
  getWorkspacePolicy,
  isRevisionConflict,
  preconditionHeaders,
  putWorkspacePolicy,
  versionFromEtag,
  type WorkspacePolicyDocument,
} from './workspacePolicies';

const policy: WorkspacePolicyDocument = {
  workspaceId: 'finance',
  name: 'Finance',
  adminGroups: [],
  memberGroups: ['g-finance'],
  allowed: { providers: [], models: [], connectors: [], toolRiskClasses: [], dataClasses: [], channels: [], regions: [] },
  budgets: { perAgentMonthlyUsd: null, maxAgentMonthlyUsd: null, perWorkspaceMonthlyUsd: null },
  approval: { low: [], medium: ['g-approvers'], high: ['g-security', 'g-approvers'] },
  defaults: {
    platformVersion: null,
    guardrailProfile: 'standard',
    memory: 'none',
    unclassifiedRiskTier: 'medium',
    trustBuilderClassifications: false,
  },
};

const V1 = '0123456789abcdef.1';
const V3 = '0123456789abcdef.3';
const V4 = '0123456789abcdef.4';
const V5 = '0123456789abcdef.5';

function jsonResponse(body: unknown, status: number, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

function requestInit(callIndex = 0): RequestInit {
  return authFetchMock.mock.calls[callIndex][1] as RequestInit;
}

describe('versionFromEtag', () => {
  it('reads the strong validator the backend serves, quoted or bare, as an opaque token', () => {
    expect(versionFromEtag(`"${V3}"`)).toBe(V3);
    expect(versionFromEtag(V3)).toBe(V3);
    expect(versionFromEtag(` "${V3}" `)).toBe(V3);
    expect(versionFromEtag('"0"')).toBe('0');
  });

  it('refuses an absent, weak or empty validator', () => {
    expect(versionFromEtag(null)).toBeNull();
    expect(versionFromEtag(`W/"${V3}"`)).toBeNull();
    expect(versionFromEtag('')).toBeNull();
    expect(versionFromEtag('""')).toBeNull();
    expect(versionFromEtag('"a b"')).toBeNull();
  });
});

describe('preconditionHeaders', () => {
  it('asks for a create or names the version read, never both and never a wildcard If-Match', () => {
    expect(preconditionHeaders('create')).toEqual({ 'If-None-Match': '*' });
    expect(preconditionHeaders({ version: V3 })).toEqual({ 'If-Match': `"${V3}"` });
    expect(preconditionHeaders({ version: '0' })).toEqual({ 'If-Match': '"0"' });
    expect(() => preconditionHeaders({ version: '' })).toThrow(/ETag token/);
    expect(() => preconditionHeaders({ version: '*' })).not.toThrow(); // the backend refuses it with 400; nothing to guess here
    expect(() => preconditionHeaders({ version: 'a"b' })).toThrow(/ETag token/);
  });
});

describe('getWorkspacePolicy', () => {
  beforeEach(() => {
    authFetchMock.mockReset();
  });

  it('returns the document with the version from the ETag', async () => {
    authFetchMock.mockResolvedValue(jsonResponse(policy, 200, { ETag: `"${V3}"` }));
    await expect(getWorkspacePolicy('finance', 'https://api.example')).resolves.toEqual({ policy, version: V3 });
    expect(authFetchMock).toHaveBeenCalledWith('https://api.example/api/workspaces/finance/policy', { method: 'GET' });
  });

  it('refuses a response without a version instead of handing out a document that cannot be saved safely', async () => {
    authFetchMock.mockResolvedValue(jsonResponse(policy, 200));
    await expect(getWorkspacePolicy('finance', '')).rejects.toMatchObject({
      status: 200,
      message: expect.stringContaining('did not return the policy version'),
    });
  });

  it('throws the normalized ApiError for a failed read', async () => {
    authFetchMock.mockResolvedValue(jsonResponse({ detail: "No policy is stored for workspace 'finance'" }, 404));
    await expect(getWorkspacePolicy('finance', '')).rejects.toMatchObject({
      status: 404,
      message: "No policy is stored for workspace 'finance'",
    });
  });
});

describe('putWorkspacePolicy', () => {
  beforeEach(() => {
    authFetchMock.mockReset();
  });

  it('creates with If-None-Match: * and returns the new version', async () => {
    authFetchMock.mockResolvedValue(jsonResponse(policy, 200, { ETag: `"${V1}"` }));
    await expect(putWorkspacePolicy(policy, 'create', '')).resolves.toEqual({ policy, version: V1 });
    const init = requestInit();
    expect(init.method).toBe('PUT');
    expect(init.headers).toEqual({ 'Content-Type': 'application/json', 'If-None-Match': '*' });
    expect(JSON.parse(init.body as string)).toEqual(policy);
  });

  it('replaces with exactly the version it was read at', async () => {
    authFetchMock.mockResolvedValue(jsonResponse(policy, 200, { ETag: `"${V4}"` }));
    await expect(putWorkspacePolicy(policy, { version: V3 }, '')).resolves.toEqual({ policy, version: V4 });
    expect(requestInit().headers).toEqual({ 'Content-Type': 'application/json', 'If-Match': `"${V3}"` });
  });

  it('surfaces a stale save as a 412 conflict carrying the current version', async () => {
    authFetchMock.mockResolvedValue(
      jsonResponse(
        {
          detail: {
            code: 'policy_revision_conflict',
            workspaceId: 'finance',
            expectedVersion: V3,
            currentVersion: V5,
            message: "The policy for workspace 'finance' changed since it was read (revision 3, now 5); read the current policy and reapply the change. Nothing was changed.",
          },
        },
        412,
      ),
    );
    let caught: unknown;
    try {
      await putWorkspacePolicy(policy, { version: V3 }, '');
    } catch (error) {
      caught = error;
    }
    expect(isRevisionConflict(caught)).toBe(true);
    expect(currentVersionOf(caught)).toBe(V5);
    expect(caught).toMatchObject({ status: 412, message: expect.stringContaining('changed since it was read') });
  });

  it('reports a recreated policy as a conflict with no usable current version to reuse blindly', async () => {
    authFetchMock.mockResolvedValue(
      jsonResponse(
        {
          detail: {
            code: 'policy_revision_conflict',
            workspaceId: 'finance',
            expectedVersion: V3,
            currentVersion: 'fedcba9876543210.1',
            message: "The policy for workspace 'finance' was deleted and recreated since it was read, so the policy you read no longer exists; read the current policy and reapply the change. Nothing was changed.",
          },
        },
        412,
      ),
    );
    let caught: unknown;
    try {
      await putWorkspacePolicy(policy, { version: V3 }, '');
    } catch (error) {
      caught = error;
    }
    expect(isRevisionConflict(caught)).toBe(true);
    expect(currentVersionOf(caught)).toBe('fedcba9876543210.1');
    expect(caught).toMatchObject({ status: 412, message: expect.stringContaining('deleted and recreated') });
  });

  it('does not treat other failures as conflicts', async () => {
    authFetchMock.mockResolvedValue(jsonResponse({ detail: { code: 'precondition_required', message: 'send If-Match' } }, 428));
    let caught: unknown;
    try {
      await putWorkspacePolicy(policy, { version: V3 }, '');
    } catch (error) {
      caught = error;
    }
    expect(isRevisionConflict(caught)).toBe(false);
    expect(currentVersionOf(caught)).toBeNull();
    expect(caught).toMatchObject({ status: 428, message: 'send If-Match' });
  });
});
