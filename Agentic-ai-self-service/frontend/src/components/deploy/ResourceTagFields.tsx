/**
 * ResourceTagFields — durable, fail-closed deployment-tag governance.
 *
 * The selected profile, explicit values, effective values, policy revision and
 * profile revision live in workflowStore governance. This component owns only
 * the remote catalog snapshot and renders stale/error recovery controls.
 */

import { useCallback, useEffect, useId, useMemo, useState } from 'react';
import { authFetch } from '../../auth/authFetch';
import {
  computeTagPolicyRevision,
  resolveResourceTags,
  type ResourceTagState,
  type TagGovernanceStatus,
  type TagPolicyRecord,
  type TagProfileRecord,
  sameInstant,
} from './resourceTagState';

function recordsEqual(
  left: Record<string, string>,
  right: Record<string, string>,
): boolean {
  const leftEntries = Object.entries(left);
  return leftEntries.length === Object.keys(right).length
    && leftEntries.every(([key, value]) => right[key] === value);
}

async function readJsonArray<T>(response: Response, label: string): Promise<T[]> {
  if (!response.ok) {
    throw new Error(`${label} could not be loaded (${response.status}).`);
  }
  const value: unknown = await response.json();
  if (!Array.isArray(value)) {
    throw new Error(`${label} returned an invalid response.`);
  }
  return value as T[];
}

export function ResourceTagFields({
  value,
  onChange,
  onStatusChange,
}: {
  value: ResourceTagState;
  onChange: (state: ResourceTagState) => void;
  onStatusChange?: (status: TagGovernanceStatus) => void;
}) {
  const [policies, setPolicies] = useState<TagPolicyRecord[]>([]);
  const [profiles, setProfiles] = useState<TagProfileRecord[]>([]);
  const profileSelectId = useId();
  const [catalogState, setCatalogState] = useState<
    | { state: 'loading' }
    | { state: 'error'; message: string }
    | { state: 'ready'; revision: string }
  >({ state: 'loading' });
  const [reloadGeneration, setReloadGeneration] = useState(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [policyResponse, profileResponse] = await Promise.all([
          authFetch('/api/settings/tags'),
          authFetch('/api/settings/tag-profiles'),
        ]);
        const [nextPolicies, nextProfiles] = await Promise.all([
          readJsonArray<TagPolicyRecord>(policyResponse, 'Tag policies'),
          readJsonArray<TagProfileRecord>(profileResponse, 'Tag profiles'),
        ]);
        const revision = await computeTagPolicyRevision(nextPolicies);
        if (cancelled) return;
        setPolicies(nextPolicies);
        setProfiles(nextProfiles);
        setCatalogState({ state: 'ready', revision });
      } catch (error) {
        if (cancelled) return;
        setCatalogState({
          state: 'error',
          message: error instanceof Error
            ? error.message
            : 'Tag governance could not be loaded.',
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadGeneration]);

  const selectedProfile = useMemo(
    () => profiles.find((profile) => profile.name === value.profileName) ?? null,
    [profiles, value.profileName],
  );
  const resolved = useMemo(
    () => resolveResourceTags(policies, selectedProfile, value.explicitValues),
    [policies, selectedProfile, value.explicitValues],
  );
  const missingRequired = policies.some(
    (policy) => policy.required && !resolved[policy.key],
  );
  const hasCapturedTagState = (
    value.profileName !== null
    || Object.keys(value.explicitValues).length > 0
    || Object.keys(value.tags).length > 0
  );

  const staleReason = useMemo(() => {
    if (catalogState.state !== 'ready') return null;
    if (value.profileName && !selectedProfile) {
      return `Tag profile "${value.profileName}" no longer exists.`;
    }
    if (
      selectedProfile
      && !sameInstant(value.profileUpdatedAt, selectedProfile.updated_at)
    ) {
      return `Tag profile "${selectedProfile.name}" changed after this workflow captured it.`;
    }
    if (
      value.policyRevision
      && value.policyRevision !== catalogState.revision
    ) {
      return 'The organisation tag policies changed after this workflow captured them.';
    }
    if (!value.policyRevision && hasCapturedTagState) {
      return 'This workflow has tag values but no verifiable policy revision.';
    }
    if (value.policyRevision && !recordsEqual(value.tags, resolved)) {
      return 'The captured effective tags no longer match their inputs.';
    }
    return null;
  }, [
    catalogState,
    hasCapturedTagState,
    resolved,
    selectedProfile,
    value.policyRevision,
    value.profileName,
    value.profileUpdatedAt,
    value.tags,
  ]);

  // A legacy/empty workflow has no tag authority to preserve. Capture the
  // current empty/default policy snapshot automatically so its next deploy can
  // be revision-checked without forcing a meaningless button click.
  useEffect(() => {
    if (
      catalogState.state === 'ready'
      && !hasCapturedTagState
      && !value.policyRevision
    ) {
      onChange({
        ...value,
        tags: resolved,
        policyRevision: catalogState.revision,
      });
    }
  }, [
    catalogState,
    hasCapturedTagState,
    onChange,
    resolved,
    value,
  ]);

  const status = useMemo<TagGovernanceStatus>(() => {
    if (catalogState.state === 'loading') {
      return {
        state: 'loading',
        message: 'Loading tag governance…',
        missingRequired: false,
      };
    }
    if (catalogState.state === 'error') {
      return {
        state: 'error',
        message: catalogState.message,
        missingRequired: false,
      };
    }
    if (!value.policyRevision && !hasCapturedTagState) {
      return {
        state: 'loading',
        message: 'Capturing the current tag-policy revision…',
        missingRequired: false,
      };
    }
    if (staleReason) {
      return {
        state: 'stale',
        message: staleReason,
        missingRequired,
      };
    }
    return { state: 'ready', message: null, missingRequired };
  }, [
    catalogState,
    hasCapturedTagState,
    missingRequired,
    staleReason,
    value.policyRevision,
  ]);

  useEffect(() => {
    onStatusChange?.(status);
  }, [onStatusChange, status]);

  const capture = useCallback((
    profile: TagProfileRecord | null,
    explicitValues: Record<string, string>,
  ) => {
    if (catalogState.state !== 'ready') return;
    onChange({
      tags: resolveResourceTags(policies, profile, explicitValues),
      profileName: profile?.name ?? null,
      profileUpdatedAt: profile?.updated_at ?? null,
      explicitValues,
      policyRevision: catalogState.revision,
    });
  }, [catalogState, onChange, policies]);

  if (catalogState.state === 'loading') {
    return (
      <div className="rounded-lg border border-white/10 p-3 text-xs text-[#5f6b7a]" role="status">
        Loading resource-tag governance…
      </div>
    );
  }

  if (catalogState.state === 'error') {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-3 space-y-2">
        <p role="alert" className="text-xs text-red-700">
          {catalogState.message} Deployment and export are paused so required
          governance cannot be bypassed.
        </p>
        <button
          type="button"
          className="text-xs font-medium text-blue-700 hover:text-blue-800"
          onClick={() => {
            setCatalogState({ state: 'loading' });
            setReloadGeneration((generation) => generation + 1);
          }}
        >
          Retry tag governance
        </button>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-white/10 p-3 space-y-3 no-darkmap">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-medium">Resource tags</span>
        {missingRequired && (
          <span className="text-xs text-amber-700">Required tag(s) missing</span>
        )}
      </div>

      {staleReason && (
        <div className="rounded border border-amber-300 bg-amber-50 p-2 space-y-2">
          <p role="alert" className="text-xs text-amber-800">
            {staleReason} Review and refresh before deploying, exporting, or publishing.
          </p>
          <div className="flex gap-3">
            {selectedProfile && (
              <button
                type="button"
                className="text-xs font-medium text-blue-700 hover:text-blue-800"
                onClick={() => capture(selectedProfile, value.explicitValues)}
              >
                Refresh captured tags
              </button>
            )}
            {value.profileName && (
              <button
                type="button"
                className="text-xs font-medium text-blue-700 hover:text-blue-800"
                onClick={() => capture(null, value.explicitValues)}
              >
                Detach profile
              </button>
            )}
            {!value.profileName && (
              <button
                type="button"
                className="text-xs font-medium text-blue-700 hover:text-blue-800"
                onClick={() => capture(null, value.explicitValues)}
              >
                Refresh captured tags
              </button>
            )}
          </div>
        </div>
      )}

      {profiles.length > 0 && (
        <div className="block text-xs">
          {/* explicit association: the select's accessible name is exactly "Tag profile" (a wrapping label would
              name it after its whole text content, option labels included) */}
          <label htmlFor={profileSelectId} className="opacity-70">Tag profile</label>
          <select
            id={profileSelectId}
            className="mt-1 w-full rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
            value={value.profileName ?? ''}
            onChange={(event) => {
              const profile = profiles.find(
                (candidate) => candidate.name === event.target.value,
              ) ?? null;
              capture(profile, value.explicitValues);
            }}
          >
            <option value="">— none —</option>
            {profiles.map((profile) => (
              <option key={profile.name} value={profile.name}>
                {profile.name}
              </option>
            ))}
          </select>
        </div>
      )}

      {policies.map((policy) => {
        const effective = (
          value.explicitValues[policy.key]
          || selectedProfile?.values[policy.key]
          || policy.default_value
          || ''
        );
        const isPlatform = policy.key.startsWith('platform:');
        return (
          <label key={policy.key} className="block text-xs">
            <span className="opacity-70">
              {policy.key}
              {policy.required && <span className="text-amber-700"> *</span>}
              {isPlatform && <span className="opacity-50"> (platform)</span>}
            </span>
            <input
              type="text"
              className="mt-1 w-full rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
              value={effective}
              placeholder={policy.default_value || (policy.required ? 'required' : 'optional')}
              onChange={(event) => {
                capture(selectedProfile, {
                  ...value.explicitValues,
                  [policy.key]: event.target.value,
                });
              }}
            />
          </label>
        );
      })}

      {policies.length === 0 && profiles.length === 0 && (
        <p className="text-xs text-[#5f6b7a]">
          No organisation tag policies or profiles are configured.
        </p>
      )}
    </div>
  );
}
