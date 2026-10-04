import type {
  DeploymentGovernanceTagsV1,
  GovernanceTagProfileRef,
} from '../../types/workflow';

export interface TagPolicyRecord {
  key: string;
  default_value: string | null;
  required: boolean;
  show_on_card: boolean;
  created_at?: string;
  updated_at: string;
}

export interface TagProfileRecord {
  name: string;
  values: Record<string, string>;
  created_at?: string;
  updated_at: string;
}

export interface ResourceTagState {
  /** Final values captured from the same precedence rules as the backend. */
  tags: Record<string, string>;
  profileName: string | null;
  profileUpdatedAt: string | null;
  explicitValues: Record<string, string>;
  policyRevision: string;
}

export type TagGovernanceStatus =
  | { state: 'loading'; message: string; missingRequired: false }
  | { state: 'error'; message: string; missingRequired: false }
  | { state: 'stale'; message: string; missingRequired: boolean }
  | { state: 'ready'; message: null; missingRequired: boolean };

export function createInitialResourceTagState(): ResourceTagState {
  return {
    tags: {},
    profileName: null,
    profileUpdatedAt: null,
    explicitValues: {},
    policyRevision: '',
  };
}

export function resourceTagStateFromGovernance(
  tags: DeploymentGovernanceTagsV1,
): ResourceTagState {
  return {
    tags: { ...tags.effectiveValues },
    profileName: tags.profile?.name ?? null,
    profileUpdatedAt: tags.profile?.updatedAt ?? null,
    explicitValues: { ...tags.explicitValues },
    policyRevision: tags.policyRevision,
  };
}

export function governanceTagsFromResourceState(
  value: ResourceTagState,
): DeploymentGovernanceTagsV1 {
  const profile: GovernanceTagProfileRef | null = (
    value.profileName && value.profileUpdatedAt
  )
    ? { name: value.profileName, updatedAt: value.profileUpdatedAt }
    : null;
  return {
    explicitValues: { ...value.explicitValues },
    effectiveValues: { ...value.tags },
    profile,
    policyRevision: value.policyRevision,
  };
}

/**
 * Match TagPolicyStore.resolve_tags exactly:
 * explicit value > profile value > policy default, followed by non-policy
 * profile/explicit keys. Empty strings mean "not supplied".
 */
export function resolveResourceTags(
  policies: TagPolicyRecord[],
  profile: TagProfileRecord | null,
  explicitValues: Record<string, string>,
): Record<string, string> {
  const resolved: Record<string, string> = {};
  const policyKeys = new Set(policies.map((policy) => policy.key));

  for (const policy of policies) {
    const value = (
      explicitValues[policy.key]
      || profile?.values[policy.key]
      || policy.default_value
      || ''
    );
    if (value) resolved[policy.key] = value;
  }

  for (const [key, value] of Object.entries({
    ...(profile?.values ?? {}),
    ...explicitValues,
  })) {
    if (!policyKeys.has(key) && value) resolved[key] = value;
  }

  return Object.fromEntries(
    Object.entries(resolved).sort(([left], [right]) => left.localeCompare(right)),
  );
}

/**
 * Stable cross-runtime policy revision. The backend must hash this exact JSON
 * projection: records sorted by key, with these six fields and JSON's compact
 * separators. created_at is included because deleting and recreating an
 * otherwise identical policy is a distinct governance decision.
 *
 * The sort is a plain code-point comparison, NOT localeCompare: localeCompare is
 * locale- and ICU-version-dependent (it orders "app" before "Env", and weights
 * punctuation like ':' and '-' differently), and Python's sorted() is code-point
 * ordered. Two runtimes that disagree about the order produce two digests for one
 * policy set, and since the backend refuses a mismatch, that disagreement would
 * read as a stale policy and block every deploy carrying a mixed-case tag key.
 * Parity is pinned by resourceTagState.test.ts and the backend's
 * test_tag_policy_revision_parity.py against the same fixture.
 */
export async function computeTagPolicyRevision(
  policies: TagPolicyRecord[],
): Promise<string> {
  const canonical = [...policies]
    .sort((left, right) => {
      if (left.key < right.key) return -1;
      return left.key > right.key ? 1 : 0;
    })
    .map((policy) => ({
      key: policy.key,
      default_value: policy.default_value,
      required: policy.required,
      show_on_card: policy.show_on_card,
      created_at: policy.created_at ?? '',
      updated_at: policy.updated_at,
    }));
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    throw new Error('This browser cannot verify the tag-policy revision (SHA-256 unavailable).');
  }
  const bytes = new TextEncoder().encode(JSON.stringify(canonical));
  const digest = await subtle.digest('SHA-256', bytes);
  const hex = Array.from(new Uint8Array(digest))
    .map((value) => value.toString(16).padStart(2, '0'))
    .join('');
  return `sha256:${hex}`;
}

/**
 * The catalog returns the profile's timestamp as the API serialises it ("+00:00"); the
 * workflow document round-trips through the backend's datetime model and comes back as
 * "Z". Measured live: the same instant in two spellings made every reloaded workflow
 * report "profile changed" and blocked deploy, export and publish until the profile was
 * re-selected. Compare instants; fall back to the strings only when one does not parse.
 */
export function sameInstant(a: string | null | undefined, b: string | null | undefined): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  const ta = Date.parse(a);
  const tb = Date.parse(b);
  if (Number.isNaN(ta) || Number.isNaN(tb)) return false;
  return ta === tb;
}
