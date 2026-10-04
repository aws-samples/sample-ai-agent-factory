import { useEffect, useMemo, useState } from 'react';
import { authFetch } from '../../auth/authFetch';

export interface DeploymentTargetSelection {
  targetAccountId?: string;
  targetRegion?: string;
}

interface DeploymentTargetOptions {
  enabled: boolean;
  home_region: string;
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
        return response.json() as Promise<DeploymentTargetOptions>;
      })
      .then((result) => {
        if (!active || !result) return;
        setOptions({
          enabled: result.enabled === true,
          home_region: typeof result.home_region === 'string'
            ? result.home_region
            : 'platform default',
          regions: Array.isArray(result.regions)
            ? result.regions.filter((region): region is string => typeof region === 'string')
            : [],
          accounts: Array.isArray(result.accounts)
            ? result.accounts.filter(
                (account): account is { account_id: string; region: string } =>
                  Boolean(account)
                  && typeof account.account_id === 'string'
                  && typeof account.region === 'string',
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

  if (!options || (!options.enabled && selectedValue === 'default')) return null;

  return (
    <section className="rounded-lg border border-[#d5dbdb] bg-[#fafafa] p-3 space-y-2">
      <div>
        <h4 className="text-xs font-semibold text-[#16191f]">Live deployment target</h4>
        <p className="text-[11px] text-[#5f6b7a]">
          This applies only to Deploy to AgentCore. Exports remain account-neutral.
        </p>
      </div>
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
