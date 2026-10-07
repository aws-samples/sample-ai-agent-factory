import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AuditDashboard } from './AuditDashboard';

const api = vi.hoisted(() => ({ getAudit: vi.fn() }));

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    getApiClient: () => api,
    getErrorMessage: (error: unknown) => error instanceof Error ? error.message : String(error),
  };
});

describe('AuditDashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows incomplete attempts even when there are no completed outcomes', async () => {
    api.getAudit.mockResolvedValue({
      total: 0,
      audit_records: 1,
      incomplete_attempts: 1,
      distinct_actors: 0,
      distinct_sessions: 0,
      by_action: {},
      by_actor: {},
      by_day: [],
      events: [],
      incomplete_events: [{
        action: 'agent.deploy',
        actor_sub: 'user-1',
        method: 'POST',
        path: '/api/deploy',
        status_code: 0,
        ts: '2026-10-07T10:00:00Z',
        phase: 'attempt',
        correlation_id: 'corr-1',
        integrity_sha256: 'a'.repeat(64),
      }],
    });

    render(<AuditDashboard />);

    expect(await screen.findByRole('alert')).toHaveTextContent('1 audited write attempt');
    expect(screen.getByText('No completed audit outcomes yet.')).toBeInTheDocument();
  });

  it('surfaces audit integrity or availability errors', async () => {
    api.getAudit.mockRejectedValue(new Error('Audit evidence is unavailable'));

    render(<AuditDashboard />);

    expect(await screen.findByText('Audit evidence is unavailable')).toBeInTheDocument();
  });

  it('marks pre-upgrade rows without digests as unverified', async () => {
    api.getAudit.mockResolvedValue({
      total: 1,
      audit_records: 1,
      incomplete_attempts: 0,
      unverified_legacy_events: 1,
      by_action: { 'agent.deploy': 1 },
      by_actor: { 'user-1': 1 },
      events: [{
        action: 'agent.deploy',
        actor_sub: 'user-1',
        method: 'POST',
        path: '/api/deploy',
        status_code: 202,
        ts: '2026-10-07T10:00:00Z',
        integrity_verified: false,
        integrity_sha256: null,
      }],
      incomplete_events: [],
    });

    render(<AuditDashboard />);

    expect(await screen.findByText(/pre-upgrade audit record/)).toHaveTextContent('not verified evidence');
  });
});
