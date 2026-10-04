import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ActiveDeploymentBanner, isActiveDeployment, type ActiveDeployment } from './ActiveDeploymentBanner';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({ tokens: { accessToken: { payload: { sub: 'owner-1' } } } })),
}));

const authFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({ authFetch: (...args: unknown[]) => authFetch(...args) }));

const deployment = (id: string, startedAt: string, deleteStatus?: string | null): ActiveDeployment => ({
  deployment_id: id,
  runtime_id: `rt-${id}`,
  status: 'succeeded',
  started_at: startedAt,
  ...(deleteStatus === undefined ? {} : { delete_status: deleteStatus }),
});

const answer = (rows: ActiveDeployment[]) =>
  authFetch.mockResolvedValue({ ok: true, json: async () => rows } as Response);

describe('ActiveDeploymentBanner', () => {
  beforeEach(() => authFetch.mockReset());

  it.each(['deleting', 'deleted', 'delete_retained', 'delete_failed'])('a %s deployment is not active', (deleteStatus) => {
    expect(isActiveDeployment(deployment('d', '2026-10-02T00:00:00Z', deleteStatus))).toBe(false);
  });

  it('a succeeded deployment no delete touched is active', () => {
    expect(isActiveDeployment(deployment('d', '2026-10-02T00:00:00Z'))).toBe(true);
    expect(isActiveDeployment(deployment('d', '2026-10-02T00:00:00Z', null))).toBe(true);
  });

  it('offers nothing when every succeeded deployment was deleted', async () => {
    answer([deployment('new', '2026-10-02T09:00:00Z', 'deleted'), deployment('old', '2026-10-01T09:00:00Z', 'deleted')]);
    const onRestore = vi.fn();
    render(<ActiveDeploymentBanner onRestore={onRestore} />);

    await waitFor(() => expect(authFetch).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText(/active deployment/i)).toBeNull();
  });

  it('restores the newest live deployment, not a newer deleted one', async () => {
    answer([deployment('deleted-newest', '2026-10-02T09:00:00Z', 'deleted'), deployment('live', '2026-10-01T09:00:00Z')]);
    const onRestore = vi.fn();
    render(<ActiveDeploymentBanner onRestore={onRestore} />);

    (await screen.findByRole('button', { name: 'Restore' })).click();
    expect(onRestore).toHaveBeenCalledWith(expect.objectContaining({ deployment_id: 'live' }));
  });
});
