import { useEffect, useMemo, useState } from 'react';
import { authFetch } from '../../auth/authFetch';
import { isReleaseRegion, releaseRegionUnavailableReason } from '../../utils/awsRegion';

export interface DeploymentTargetSelection {
  targetAccountId?: string;
  targetRegion?: string;
}

interface DeploymentTargetOptions {
  enabled: boolean;
  home_region: string;
  home_region_admissible: boolean;
  regions: string[];
  accounts: Array<{ account_id: string; region: string }>;
}

interface DeploymentTargetFieldsProps {
  value: DeploymentTargetSelection;
  onChange: (value: DeploymentTargetSelection) => void;
  disabled?: boolean;
}

function selectionValue(value: DeploymentTargetSelection): string {
  if (value.targetAccountId) {
    return `account:${value.targetAccountId}:${value.targetRegion || ''}`;
  }
  if (value.targetRegion) return `region:${value.targetRegion}`;
  return 'default';
}

export function DeploymentTargetFields({
  value,
  onChange,
  disabled = false,
}: DeploymentTargetFieldsProps) {
  const [options, setOptions] = useState<DeploymentTargetOptions | null>(null);

  useEffect(() => {
    let active = true;
    void authFetch('/api/deploy-targets')
      .then(async (response) => {
        if (!response.ok) return null;
        return response.json() as Promise<Partial<DeploymentTargetOptions> & { home_region_admissible?: boolean }>;
      })
      .then((result) => {
        if (!active || !result) return;
        // Twin of the server-side filter in /api/deploy-targets: never offer a target the
        // deploy route would refuse, even if an older backend returned an unfiltered list.
        setOptions({
          enabled: result.enabled === true,
          home_region: typeof result.home_region === 'string'
            ? result.home_region
            : 'platform default',
          // Refuse only on a KNOWN inadmissible home Region: the backend's own verdict when it
          // sends one, else the predicate over a real Region string. A catalog that names no
          // home Region (older backend, minimal response) is unknown, not refused -- the deploy
          // route remains the enforcement point and will refuse an inadmissible home itself.
          home_region_admissible: typeof result.home_region === 'string'
            ? result.home_region_admissible !== false && isReleaseRegion(result.home_region)
            : true,
          regions: Array.isArray(result.regions)
            ? result.regions.filter((region): region is string => isReleaseRegion(region))
            : [],
          accounts: Array.isArray(result.accounts)
            ? result.accounts.filter(
                (account): account is { account_id: string; region: string } =>
                  Boolean(account)
                  && typeof account.account_id === 'string'
                  && isReleaseRegion(account.region),
              )
            : [],
        });
      })
      .catch(() => {
        // Deployment to the platform default remains available if the optional
        // target catalog cannot be loaded.
      });
    return () => {
      active = false;
    };
  }, []);

  const allowedValues = useMemo(() => {
    const values = new Set(['default']);
    if (!options?.enabled) return values;
    for (const region of options.regions) {
      if (region !== options.home_region) values.add(`region:${region}`);
    }
    for (const account of options.accounts) {
      values.add(`account:${account.account_id}:${account.region}`);
    }
    return values;
  }, [options]);

  const selectedValue = selectionValue(value);
  useEffect(() => {
    if (options && !allowedValues.has(selectedValue)) onChange({});
  }, [allowedValues, onChange, options, selectedValue]);

  if (!options || (!options.enabled && selectedValue === 'default' && options.home_region_admissible)) return null;

  return (
    <section className="rounded-lg border border-[#d5dbdb] bg-[#fafafa] p-3 space-y-2">
      <div>
        <h4 className="text-xs font-semibold text-[#16191f]">Live deployment target</h4>
        <p className="text-[11px] text-[#5f6b7a]">
          This applies only to Deploy to AgentCore. Exports remain account-neutral.
        </p>
      </div>
      {!options.home_region_admissible && (
        <p role="alert" className="text-[11px] text-[#d13212]">
          {releaseRegionUnavailableReason(options.home_region, 'deploying to the platform account')}
        </p>
      )}
      <label className="block text-xs font-medium text-[#414d5c]" htmlFor="live-deployment-target">
        Account and region
      </label>
      <select
        id="live-deployment-target"
        aria-label="Live deployment target"
        value={allowedValues.has(selectedValue) ? selectedValue : 'default'}
        disabled={disabled}
        onChange={(event) => {
          const selected = event.target.value;
          if (selected === 'default') {
            onChange({});
            return;
          }
          const [kind, first, second] = selected.split(':');
          if (kind === 'region') {
            onChange({ targetRegion: first });
            return;
          }
          onChange({
            targetAccountId: first,
            targetRegion: second,
          });
        }}
        className="w-full rounded-md border border-[#aab7b8] bg-white px-2.5 py-2 text-sm text-[#16191f] disabled:opacity-60"
      >
        <option value="default">
          Platform account · {options.home_region}
        </option>
        {options.enabled && options.regions
          .filter((region) => region !== options.home_region)
          .map((region) => (
            <option key={`region:${region}`} value={`region:${region}`}>
              Platform account · {region}
            </option>
          ))}
        {options.enabled && options.accounts.map((account) => (
          <option
            key={`account:${account.account_id}:${account.region}`}
            value={`account:${account.account_id}:${account.region}`}
          >
            Account {account.account_id} · {account.region}
          </option>
        ))}
      </select>
      {value.targetAccountId && (
        <p className="text-[11px] text-[#5f6b7a]">
          The backend revalidates the target deployment, Runtime, and Harness roles
          before starting this deployment.
        </p>
      )}
    </section>
  );
}
