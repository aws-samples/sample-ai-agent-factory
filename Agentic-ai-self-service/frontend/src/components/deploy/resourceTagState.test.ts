import { describe, expect, it } from 'vitest';
import {
  computeTagPolicyRevision,
  governanceTagsFromResourceState,
  resolveResourceTags,
  resourceTagStateFromGovernance,
  type TagPolicyRecord,
} from './resourceTagState';

const POLICIES: TagPolicyRecord[] = [
  {
    key: 'application',
    default_value: 'default-app',
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

describe('resource-tag governance state', () => {
  it('matches backend precedence and retains ad-hoc profile/explicit values', () => {
    expect(resolveResourceTags(
      POLICIES,
      {
        name: 'regulated',
        values: {
          application: 'profile-app',
          owner: 'profile-owner',
          profile_only: 'yes',
        },
        updated_at: '2026-09-23T10:00:00Z',
      },
      {
        owner: 'explicit-owner',
        explicit_only: 'yes',
      },
    )).toEqual({
      application: 'profile-app',
      explicit_only: 'yes',
      owner: 'explicit-owner',
      profile_only: 'yes',
    });
  });

  it('produces an order-independent SHA-256 revision that changes with policy state', async () => {
    const forward = await computeTagPolicyRevision(POLICIES);
    const reverse = await computeTagPolicyRevision([...POLICIES].reverse());
    const changed = await computeTagPolicyRevision([
      { ...POLICIES[0], required: false },
      POLICIES[1],
    ]);

    expect(forward).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(reverse).toBe(forward);
    expect(changed).not.toBe(forward);
  });

  it('hashes the exact digest the backend computes for the same adversarial policy set', async () => {
    // One constant, two independent implementations. The fixture is built to break on every
    // way the two can drift: an uppercase key, a non-ASCII key AND value (JSON.stringify and
    // json.dumps(ensure_ascii=False) must both emit them raw), punctuation in keys that
    // localeCompare weights differently from code points, a null default_value, and a policy
    // with no created_at (which must serialize as ""). Under localeCompare this set sorts
    // app-tier, Eclair, platform:owner, Zulu:z; under code points it sorts Zulu:z, app-tier,
    // platform:owner, Eclair -- so a locale-ordered implementation cannot produce this digest.
    // The backend pins the same constant in tests/test_tag_policy_revision_parity.py.
    const adversarial: TagPolicyRecord[] = [
      {
        key: 'Zulu:z',
        default_value: null,
        required: false,
        show_on_card: true,
        updated_at: '2026-09-23T09:00:00Z',
      },
      {
        key: 'app-tier',
        default_value: 'tier/1',
        required: true,
        show_on_card: false,
        created_at: '2026-09-22T09:00:00Z',
        updated_at: '2026-09-23T09:00:00Z',
      },
      {
        key: 'platform:owner',
        default_value: null,
        required: true,
        show_on_card: true,
        created_at: '2026-09-21T09:00:00Z',
        updated_at: '2026-09-23T09:00:00Z',
      },
      {
        key: 'Éclair',
        default_value: 'crème',
        required: false,
        show_on_card: false,
        created_at: '',
        updated_at: '2026-09-23T09:00:00Z',
      },
    ];

    expect(await computeTagPolicyRevision(adversarial)).toBe(
      'sha256:1518cafb7088ecc412bf2297aed7a82787b4d40ed7ecabf5342377ada6654384',
    );
  });

  it('round-trips the canonical workflow governance envelope without session storage', () => {
    const governanceTags = {
      explicitValues: { owner: 'alice' },
      effectiveValues: { application: 'payments', owner: 'alice' },
      profile: {
        name: 'regulated',
        updatedAt: '2026-09-23T10:00:00Z',
      },
      policyRevision: 'sha256:abc',
    };

    expect(
      governanceTagsFromResourceState(
        resourceTagStateFromGovernance(governanceTags),
      ),
    ).toEqual(governanceTags);
  });
});
