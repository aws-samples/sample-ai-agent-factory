/**
 * Banner shown when an active deployment exists for the current user.
 * Prompts the user to restore the deploy panel or dismiss.
 */

import { useState, useEffect, useCallback } from 'react';
import { authFetch } from '../../auth/authFetch';
import { fetchAuthSession } from 'aws-amplify/auth';

export interface ActiveDeployment {
  deployment_id: string;
  workflow_id?: string;
  runtime_id?: string;
  runtime_endpoint?: string;
  gateway_url?: string;
  status: string;
  started_at: string;
}

interface ActiveDeploymentBannerProps {
  onRestore: (deployment: ActiveDeployment) => void;
}

export function ActiveDeploymentBanner({ onRestore }: ActiveDeploymentBannerProps) {
  const [deployment, setDeployment] = useState<ActiveDeployment | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const session = await fetchAuthSession();
        const sub = session.tokens?.accessToken?.payload?.sub;
        if (!sub) return;

        const resp = await authFetch(`/api/deployments?status=succeeded`);
        if (!resp.ok || cancelled) return;
        const data: ActiveDeployment[] = await resp.json();
        if (data.length > 0 && !cancelled) {
          const sorted = data.sort(
            (a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime()
          );
          setDeployment(sorted[0]);
        }
      } catch { /* ignore */ }
    })();

    return () => { cancelled = true; };
  }, []);

  const handleRestore = useCallback(() => {
    if (deployment) {
      onRestore(deployment);
      setDismissed(true);
    }
  }, [deployment, onRestore]);

  if (!deployment || dismissed) return null;

  return (
    <div className="absolute top-14 left-1/2 -translate-x-1/2 z-40 flex items-center gap-3 px-4 py-2.5 bg-[#232f3e] text-white rounded-lg shadow-lg border border-[#ff9900]/30 text-sm">
      <div className="w-2 h-2 bg-emerald-400 rounded-full animate-pulse flex-shrink-0" />
      <span>You have an active deployment. </span>
      <button
        onClick={handleRestore}
        className="px-3 py-1 bg-[#ff9900] text-[#232f3e] rounded font-medium hover:bg-[#ec7211] transition-colors"
      >
        Restore
      </button>
      <button
        onClick={() => setDismissed(true)}
        className="px-2 py-1 text-white/60 hover:text-white transition-colors"
      >
        Dismiss
      </button>
    </div>
  );
}
