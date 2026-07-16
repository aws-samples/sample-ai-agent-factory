/**
 * TokenInfoCard — the signed-in user's decoded identity (Loom-study 1.3).
 *
 * Renders the caller's JWT claims (with plain-language annotations), their
 * Cognito groups, and the resolved capability scopes — so a user (or a security
 * reviewer) can see exactly WHO they are to the platform and WHAT that grants.
 * Same "discovery is transparency" spirit as the registry Access tab: makes the
 * identity → group → scope chain legible rather than opaque.
 */

import { useEffect, useState } from 'react';
import { getTokenInfoApi, getErrorMessage, type TokenInfo } from '../../../services/api';

function renderValue(v: unknown): string {
  if (Array.isArray(v)) return v.join(', ');
  if (typeof v === 'object' && v !== null) return JSON.stringify(v);
  // epoch → readable for exp/iat is handled by the note; show raw here.
  return String(v);
}

export function TokenInfoCard() {
  const [info, setInfo] = useState<TokenInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getTokenInfoApi().then(setInfo).catch((e) => setError(getErrorMessage(e)));
  }, []);

  if (error) {
    return <div className="text-sm" style={{ color: '#dc2626' }}>Couldn't load identity: {error}</div>;
  }
  if (!info) {
    return <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>Loading your identity…</div>;
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border p-4" style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-subtle)' }}>
        <h3 className="text-sm font-semibold mb-2" style={{ color: 'var(--color-text-primary)' }}>Who you are</h3>
        <div className="text-xs mb-1" style={{ color: 'var(--color-text-secondary)' }}>
          Subject: <code className="text-[11px]">{info.sub}</code>
        </div>
        {info.claims.length > 0 && (
          <table className="w-full text-sm mt-2">
            <thead>
              <tr className="text-left" style={{ color: 'var(--color-text-secondary)' }}>
                <th className="py-1 pr-3 font-medium">Claim</th>
                <th className="py-1 pr-3 font-medium">Value</th>
                <th className="py-1 font-medium">Meaning</th>
              </tr>
            </thead>
            <tbody>
              {info.claims.map((c) => (
                <tr key={c.claim} className="border-t" style={{ borderColor: 'var(--color-border)' }}>
                  <td className="py-1 pr-3"><code className="text-[11px]">{c.claim}</code></td>
                  <td className="py-1 pr-3 break-all" style={{ color: 'var(--color-text-primary)' }}>{renderValue(c.value)}</td>
                  <td className="py-1 text-xs" style={{ color: 'var(--color-text-secondary)' }}>{c.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="rounded-lg border p-4" style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-subtle)' }}>
        <h3 className="text-sm font-semibold mb-2" style={{ color: 'var(--color-text-primary)' }}>Groups → scopes</h3>
        <div className="mb-2">
          <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>Cognito groups: </span>
          {info.groups.length ? (
            info.groups.map((g) => (
              <span key={g} className="inline-flex items-center px-1.5 py-0.5 mr-1 rounded bg-blue-50 text-blue-700 text-[11px]">{g}</span>
            ))
          ) : (
            <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>none (no scopes granted)</span>
          )}
        </div>
        <div>
          <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>Resolved scopes: </span>
          {info.scopes.length ? (
            info.scopes.map((s) => (
              <span key={s} className="inline-flex items-center px-1.5 py-0.5 mr-1 mb-1 rounded bg-emerald-50 text-emerald-700 text-[11px]">{s}</span>
            ))
          ) : (
            <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>none</span>
          )}
        </div>
        <div className="mt-3 px-3 py-2 rounded-lg text-xs" style={{ background: 'rgba(79,156,255,.08)', border: '1px solid var(--accent)', color: 'var(--color-text-secondary)' }}>
          Your capabilities are derived from your Cognito group membership. If an action is disabled, your groups don't grant its scope — an admin assigns groups in AWS Cognito (see docs/PERSONAS.md).
        </div>
      </div>
    </div>
  );
}
