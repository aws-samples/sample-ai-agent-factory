/**
 * The revision contract of the workspace policy API module: the revision rides on `ETag`, a save
 * names it (`If-Match`) or asks for a create (`If-None-Match: *`), a `412` is a conflict the panel can
 * recover from, and a response without a revision is refused rather than handed out.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const { authFetchMock } = vi.hoisted(() => ({
  authFetchMock: vi.fn(),
}));

vi.mock('../../auth/authFetch', () => ({
  authFetch: authFetchMock,
}));

import {
  currentRevisionOf,
  getWorkspacePolicy,
  isRevisionConflict,
  preconditionHeaders,
  putWorkspacePolicy,
  revisionFromEtag,
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

function jsonResponse(body: unknown, status: number, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

function requestInit(callIndex = 0): RequestInit {
  return authFetchMock.mock.calls[callIndex][1] as RequestInit;
}

describe('revisionFromEtag', () => {
  it('reads the strong validator the backend serves, quoted or bare', () => {
    expect(revisionFromEtag('"3"')).toBe(3);
    expect(revisionFromEtag('3')).toBe(3);
    expect(revisionFromEtag(' "12" ')).toBe(12);
  });

  it('refuses anything that is not a revision', () => {
    expect(revisionFromEtag(null)).toBeNull();
    expect(revisionFromEtag('W/"3"')).toBeNull();
    expect(revisionFromEtag('"abc"')).toBeNull();
    expect(revisionFromEtag('')).toBeNull();
  });
});

describe('preconditionHeaders', () => {
  it('asks for a create or names the revision read, never both and never a wildcard If-Match', () => {
    expect(preconditionHeaders('create')).toEqual({ 'If-None-Match': '*' });
    expect(preconditionHeaders({ revision: 0 })).toEqual({ 'If-Match': '"0"' });
    expect(preconditionHeaders({ revision: 7 })).toEqual({ 'If-Match': '"7"' });
    expect(() => preconditionHeaders({ revision: -1 })).toThrow(/non-negative integer/);
    expect(() => preconditionHeaders({ revision: 1.5 })).toThrow(/non-negative integer/);
  });
});

describe('getWorkspacePolicy', () => {
  beforeEach(() => {
    authFetchMock.mockReset();
  });

  it('returns the document with the revision from the ETag', async () => {
    authFetchMock.mockResolvedValue(jsonResponse(policy, 200, { ETag: '"3"' }));
    await expect(getWorkspacePolicy('finance', 'https://api.example')).resolves.toEqual({ policy, revision: 3 });
    expect(authFetchMock).toHaveBeenCalledWith('https://api.example/api/workspaces/finance/policy', { method: 'GET' });
  });

  it('refuses a response without a revision instead of handing out a document that cannot be saved safely', async () => {
    authFetchMock.mockResolvedValue(jsonResponse(policy, 200));
    await expect(getWorkspacePolicy('finance', '')).rejects.toMatchObject({
      status: 200,
      message: expect.stringContaining('did not return the policy revision'),
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

  it('creates with If-None-Match: * and returns the new revision', async () => {
    authFetchMock.mockResolvedValue(jsonResponse(policy, 200, { ETag: '"1"' }));
    await expect(putWorkspacePolicy(policy, 'create', '')).resolves.toEqual({ policy, revision: 1 });
    const init = requestInit();
    expect(init.method).toBe('PUT');
    expect(init.headers).toEqual({ 'Content-Type': 'application/json', 'If-None-Match': '*' });
    expect(JSON.parse(init.body as string)).toEqual(policy);
  });

  it('replaces with the exact revision it was read at', async () => {
    authFetchMock.mockResolvedValue(jsonResponse(policy, 200, { ETag: '"4"' }));
    await expect(putWorkspacePolicy(policy, { revision: 3 }, '')).resolves.toEqual({ policy, revision: 4 });
    expect(requestInit().headers).toEqual({ 'Content-Type': 'application/json', 'If-Match': '"3"' });
  });

  it('surfaces a stale save as a 412 conflict carrying the current revision', async () => {
    authFetchMock.mockResolvedValue(
      jsonResponse(
        {
          detail: {
            code: 'policy_revision_conflict',
            workspaceId: 'finance',
            expectedRevision: 3,
            currentRevision: 5,
            message: "The policy for workspace 'finance' changed since it was read (revision 3, now 5); read the current policy and reapply the change. Nothing was changed.",
          },
        },
        412,
      ),
    );
    let caught: unknown;
    try {
      await putWorkspacePolicy(policy, { revision: 3 }, '');
    } catch (error) {
      caught = error;
    }
    expect(isRevisionConflict(caught)).toBe(true);
    expect(currentRevisionOf(caught)).toBe(5);
    expect(caught).toMatchObject({ status: 412, message: expect.stringContaining('changed since it was read') });
  });

  it('does not treat other failures as conflicts', async () => {
    authFetchMock.mockResolvedValue(jsonResponse({ detail: { code: 'precondition_required', message: 'send If-Match' } }, 428));
    let caught: unknown;
    try {
      await putWorkspacePolicy(policy, { revision: 3 }, '');
    } catch (error) {
      caught = error;
    }
    expect(isRevisionConflict(caught)).toBe(false);
    expect(currentRevisionOf(caught)).toBeNull();
    expect(caught).toMatchObject({ status: 428, message: 'send If-Match' });
  });
});
