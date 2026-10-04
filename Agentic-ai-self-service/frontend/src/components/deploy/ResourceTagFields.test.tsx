import { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ResourceTagFields } from './ResourceTagFields';
import {
  createInitialResourceTagState,
  type ResourceTagState,
  type TagGovernanceStatus,
} from './resourceTagState';

const authFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => authFetch(...args),
}));

const policies = [
  {
    key: 'application',
    default_value: 'payments',
    required: true,
    show_on_card: true,
    created_at: '2026-09-22T09:00:00Z',
    updated_at: '2026-09-23T09:00:00Z',
  },
  {
    key: 'owner',
    default_value: null,
    required: true,
    show_on_card: false,
    created_at: '2026-09-22T09:00:00Z',
    updated_at: '2026-09-23T09:00:00Z',
  },
];
const profiles = [
  {
    name: 'regulated',
    values: { owner: 'platform-team', profile_only: 'retained' },
    created_at: '2026-09-22T10:00:00Z',
    updated_at: '2026-09-23T10:00:00Z',
  },
];

function response(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function Harness({
  initial = createInitialResourceTagState(),
  onStatus,
}: {
  initial?: ResourceTagState;
  onStatus?: (status: TagGovernanceStatus) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <ResourceTagFields
        value={value}
        onChange={setValue}
        onStatusChange={onStatus}
      />
      <output data-testid="governance-state">{JSON.stringify(value)}</output>
    </>
  );
}

beforeEach(() => {
  authFetch.mockReset();
  authFetch.mockImplementation((url: string) => Promise.resolve(
    response(url.endsWith('/tag-profiles') ? profiles : policies),
  ));
});

describe('ResourceTagFields', () => {
  it('captures the current revision and blocks until required values are supplied', async () => {
    const statuses: TagGovernanceStatus[] = [];
    render(<Harness onStatus={(status) => statuses.push(status)} />);

    await screen.findByDisplayValue('payments');
    await waitFor(() => {
      const state = JSON.parse(screen.getByTestId('governance-state').textContent || '{}');
      expect(state.policyRevision).toMatch(/^sha256:[0-9a-f]{64}$/);
      expect(state.tags).toEqual({ application: 'payments' });
    });
    // The status callback fires from an effect that runs AFTER the governance-state text
    // above has updated, so under worker contention the latest status can still be
    // 'loading' when the previous waitFor resolves. Certification #28 (2026-09-29) failed
    // on exactly that. Wait for the status positively instead of asserting the instant.
    await waitFor(() => {
      expect(statuses.at(-1)).toMatchObject({
        state: 'ready',
        missingRequired: true,
      });
    });

    fireEvent.change(screen.getByLabelText(/owner/), {
      target: { value: 'alice' },
    });
    await waitFor(() => {
      const state = JSON.parse(screen.getByTestId('governance-state').textContent || '{}');
      expect(state.tags.owner).toBe('alice');
      expect(statuses.at(-1)).toMatchObject({
        state: 'ready',
        missingRequired: false,
      });
    });
  });

  it('surfaces a catalog outage instead of silently removing governance', async () => {
    authFetch.mockResolvedValue(response({ detail: 'down' }, 503));
    const statuses: TagGovernanceStatus[] = [];
    render(<Harness onStatus={(status) => statuses.push(status)} />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /Deployment and export are paused/,
    );
    expect(statuses.at(-1)).toMatchObject({ state: 'error' });
    expect(screen.getByRole('button', { name: 'Retry tag governance' })).toBeEnabled();
  });

  it('resolves each field in explicit > profile > default order, and shows the winner', async () => {
    // The effective value is a chain of `||` fallbacks, and the state assertions elsewhere
    // in this file cannot tell the three sources apart: `owner` has no default, so a
    // profile value and an explicit value produce an identically shaped `tags` object.
    // Only the rendered input distinguishes them, and only a profile selected THROUGH THE
    // UI exercises the branch a deploying user actually takes.
    render(<Harness />);

    // Default, with no profile and nothing typed.
    const application = await screen.findByDisplayValue('payments');
    expect(screen.getByLabelText(/owner/)).toHaveValue('');

    // Policies and profiles arrive from two fetches. A change fired before the profile's
    // <option> exists is a silent no-op on a <select>, so wait for the option, not just for
    // the policies' default to render (this raced and failed under CPU contention in a
    // certification run on 2026-09-28).
    await screen.findByRole('option', { name: 'regulated' });
    fireEvent.change(screen.getByLabelText(/Tag profile/i), {
      target: { value: 'regulated' },
    });

    // The profile fills a field that had no default, and does NOT displace a default it
    // says nothing about. A chain that short-circuited on the profile would blank
    // `application` here, and every state-level assertion would still pass.
    await waitFor(() => {
      expect(screen.getByLabelText(/owner/)).toHaveValue('platform-team');
    });
    expect(application).toHaveValue('payments');

    // Typing beats the profile. The reverse ordering is the dangerous one: it silently
    // discards what the operator entered and deploys the profile's value instead.
    fireEvent.change(screen.getByLabelText(/owner/), { target: { value: 'alice' } });
    await waitFor(() => {
      expect(screen.getByLabelText(/owner/)).toHaveValue('alice');
      const state = JSON.parse(screen.getByTestId('governance-state').textContent || '{}');
      expect(state.tags.owner).toBe('alice');
      expect(state.explicitValues.owner).toBe('alice');
      // The profile stays selected, so a later profile change still applies to the
      // fields the operator has not overridden.
      expect(state.profileName).toBe('regulated');
    });
  });

  it('marks a platform-reserved key as such so an operator does not fight the API over it', async () => {
    // `POST /api/settings/tags` reserves the `platform:` namespace from creation, so a
    // value typed into one of these fields is rejected server-side. The label is the only
    // thing that says so before the request is made.
    authFetch.mockImplementation((url: string) => Promise.resolve(
      response(
        url.endsWith('/tag-profiles')
          ? []
          : [{
            key: 'platform:tier',
            default_value: 'gold',
            required: false,
            show_on_card: false,
            created_at: '2026-09-22T09:00:00Z',
            updated_at: '2026-09-23T09:00:00Z',
          }],
      ),
    ));
    render(<Harness />);

    expect(await screen.findByText(/\(platform\)/)).toBeInTheDocument();
    expect(screen.getByLabelText(/platform:tier/)).toHaveValue('gold');
  });

  it('does not call a profile stale when only the timestamp spelling differs', async () => {
    // Live: the catalog said "+00:00", the reloaded workflow document said "Z" for the same
    // instant, and the false "changed" alert blocked every export after a reload.
    authFetch.mockImplementation((url: string) => Promise.resolve(
      response(url.endsWith('/tag-profiles')
        ? [{ ...profiles[0], updated_at: '2026-09-23T10:00:00.055194+00:00' }]
        : policies),
    ));
    const onStatus = vi.fn();
    render(
      <Harness
        initial={{
          tags: { application: 'payments', owner: 'platform-team', profile_only: 'retained' },
          profileName: 'regulated',
          profileUpdatedAt: '2026-09-23T10:00:00.055194Z',
          explicitValues: {},
          policyRevision: 'sha256:will-be-replaced-below',
        }}
        onStatus={onStatus}
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole('combobox', { name: 'Tag profile' })).toHaveValue('regulated');
    });
    // The policy revision is what the catalog computes; whatever it is, the alert must not
    // be the profile-changed one.
    const alert = screen.queryByRole('alert');
    if (alert) {
      expect(alert).not.toHaveTextContent(/changed after this workflow captured it/);
    }
  });

  it('detects stale profile/policy state and refreshes only after an explicit review', async () => {
    render(
      <Harness
        initial={{
          tags: { application: 'old', owner: 'old-owner' },
          profileName: 'regulated',
          profileUpdatedAt: '2026-09-22T10:00:00Z',
          explicitValues: {},
          policyRevision: 'sha256:old',
        }}
      />,
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /changed after this workflow captured it/,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Refresh captured tags' }));

    await waitFor(() => {
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      const state = JSON.parse(screen.getByTestId('governance-state').textContent || '{}');
      expect(state.profileUpdatedAt).toBe('2026-09-23T10:00:00Z');
      expect(state.tags).toEqual({
        application: 'payments',
        owner: 'platform-team',
        profile_only: 'retained',
      });
    });
  });
});
