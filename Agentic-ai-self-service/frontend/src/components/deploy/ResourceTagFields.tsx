/**
 * ResourceTagFields — governance tagging inputs for the deploy flow (Phase 2).
 *
 * Fetches the org's tag policies + profiles from /api/settings, lets the user
 * pick a named profile and/or fill required/optional tag values, and reports
 * the resolved { tags, profileName } up to the parent so DeployPanel can attach
 * them to the deploy payload (resourceTags / tagProfile). The selected profile
 * persists in sessionStorage so it survives across deploys in a session.
 *
 * Required tags with no value block the deploy at the backend (HTTP 400); this
 * component surfaces the requirement inline so the user fills it before deploy.
 */

import { useEffect, useMemo, useState } from 'react';
import { authFetch } from '../../auth/authFetch';

interface TagPolicy {
  key: string;
  default_value: string | null;
  required: boolean;
  show_on_card: boolean;
}
interface TagProfile {
  name: string;
  values: Record<string, string>;
}

const PROFILE_KEY = 'acf:selectedTagProfile';

export interface ResourceTagState {
  tags: Record<string, string>;
  profileName: string | null;
}

export function ResourceTagFields({
  onChange,
}: {
  onChange: (state: ResourceTagState) => void;
}) {
  const [policies, setPolicies] = useState<TagPolicy[]>([]);
  const [profiles, setProfiles] = useState<TagProfile[]>([]);
  const [profileName, setProfileName] = useState<string | null>(
    () => sessionStorage.getItem(PROFILE_KEY),
  );
  const [values, setValues] = useState<Record<string, string>>({});
  const [loaded, setLoaded] = useState(false);

  // Fetch policies + profiles once.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [pRes, prRes] = await Promise.all([
          authFetch('/api/settings/tags'),
          authFetch('/api/settings/tag-profiles'),
        ]);
        if (cancelled) return;
        if (pRes.ok) setPolicies(await pRes.json());
        if (prRes.ok) setProfiles(await prRes.json());
      } catch {
        /* tagging is optional — degrade silently if the endpoint is absent */
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const selectedProfile = useMemo(
    () => profiles.find((p) => p.name === profileName) ?? null,
    [profiles, profileName],
  );

  // Resolve effective tag values: explicit input > profile value > default.
  const resolved = useMemo(() => {
    const out: Record<string, string> = {};
    for (const pol of policies) {
      const v = values[pol.key] || selectedProfile?.values?.[pol.key] || pol.default_value || '';
      if (v) out[pol.key] = v;
    }
    return out;
  }, [policies, values, selectedProfile]);

  // Report upward whenever resolution changes.
  useEffect(() => {
    onChange({ tags: resolved, profileName });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolved, profileName]);

  const selectProfile = (name: string) => {
    const next = name || null;
    setProfileName(next);
    if (next) sessionStorage.setItem(PROFILE_KEY, next);
    else sessionStorage.removeItem(PROFILE_KEY);
  };

  if (!loaded || (policies.length === 0 && profiles.length === 0)) return null;

  const missingRequired = policies.some(
    (p) => p.required && !resolved[p.key],
  );

  return (
    <div className="rounded-lg border border-white/10 p-3 space-y-3 no-darkmap">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">Resource tags</span>
        {missingRequired && (
          <span className="text-xs text-amber-400">Required tag(s) missing</span>
        )}
      </div>

      {profiles.length > 0 && (
        <label className="block text-xs">
          <span className="opacity-70">Tag profile</span>
          <select
            className="mt-1 w-full rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
            value={profileName ?? ''}
            onChange={(e) => selectProfile(e.target.value)}
          >
            <option value="">— none —</option>
            {profiles.map((p) => (
              <option key={p.name} value={p.name}>{p.name}</option>
            ))}
          </select>
        </label>
      )}

      {policies.map((pol) => {
        const effective = values[pol.key] ?? selectedProfile?.values?.[pol.key] ?? pol.default_value ?? '';
        const isPlatform = pol.key.startsWith('platform:');
        return (
          <label key={pol.key} className="block text-xs">
            <span className="opacity-70">
              {pol.key}
              {pol.required && <span className="text-amber-400"> *</span>}
              {isPlatform && <span className="opacity-50"> (platform)</span>}
            </span>
            <input
              type="text"
              className="mt-1 w-full rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
              value={effective}
              placeholder={pol.default_value || (pol.required ? 'required' : 'optional')}
              onChange={(e) => setValues((v) => ({ ...v, [pol.key]: e.target.value }))}
            />
          </label>
        );
      })}
    </div>
  );
}
