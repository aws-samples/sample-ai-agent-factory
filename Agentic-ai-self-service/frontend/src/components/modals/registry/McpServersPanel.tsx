/**
 * McpServersPanel — browse the verified external MCP-server catalog inside the
 * Registry. Companion to the agent-blueprint registry: these are remote MCP
 * servers that can be wired as an AgentCore Gateway `mcpServer` target.
 *
 * Same "discovery is transparency" spirit as the rest of the registry: each card
 * shows the integration tier, auth style, and whether it's live-verified, and the
 * detail view spells out the exact endpoint, credentials needed, and example
 * tools — so a user knows how to connect it (and what it costs in setup) before
 * they try.
 */

import { useEffect, useMemo, useState } from 'react';
import {
  listMcpServersApi,
  getMcpServerApi,
  getErrorMessage,
  type McpServerSummary,
  type McpServerDetail,
} from '../../../services/api';

// Tier → human label + how it connects (mirrors docs/MCP_GATEWAY_INTEGRATION.md).
const TIER_LABEL: Record<string, string> = {
  'direct-none': 'Direct · no credentials',
  'direct-apikey': 'Direct · API key',
  'direct-oauth': 'Direct · OAuth / SigV4',
  'adapter-3lo': 'Adapter · interactive OAuth',
  'adapter-stdio': 'Adapter · self-host (stdio)',
};

function badgeClass(kind: 'verified' | 'tier', value: string): string {
  if (kind === 'verified') {
    return value === 'live'
      ? 'bg-emerald-100 text-emerald-700'
      : value === 'docs'
        ? 'bg-blue-100 text-blue-700'
        : 'bg-gray-100 text-gray-600';
  }
  return value.startsWith('direct')
    ? 'bg-indigo-100 text-indigo-700'
    : 'bg-amber-100 text-amber-700';
}

export function McpServersPanel() {
  const [servers, setServers] = useState<McpServerSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<McpServerDetail | null>(null);

  useEffect(() => {
    listMcpServersApi()
      .then(setServers)
      .catch((e) => setError(getErrorMessage(e)));
  }, []);

  useEffect(() => {
    // No synchronous state reset here — staleness is derived at render time by
    // comparing detail.id to selectedId, so deselecting needs no effect work.
    if (!selectedId) return;
    let cancelled = false;
    getMcpServerApi(selectedId)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((e) => {
        if (!cancelled) setError(getErrorMessage(e));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const filtered = useMemo(() => {
    if (!servers) return [];
    const q = query.toLowerCase();
    return servers.filter(
      (s) =>
        !q ||
        s.display_name.toLowerCase().includes(q) ||
        s.publisher.toLowerCase().includes(q) ||
        s.category.toLowerCase().includes(q),
    );
  }, [servers, query]);

  if (error) {
    return <div className="text-sm" style={{ color: '#dc2626' }}>Failed to load MCP catalog: {error}</div>;
  }
  if (!servers) {
    return <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>Loading MCP servers…</div>;
  }

  // Detail view
  if (selectedId && detail && detail.id === selectedId) {
    return <McpDetail detail={detail} onBack={() => setSelectedId(null)} />;
  }

  const liveCount = servers.filter((s) => s.live_testable).length;

  return (
    <div>
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search MCP servers by name, publisher, or category…"
        className="w-full mb-3 px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
      />
      <p className="text-xs mb-3" style={{ color: 'var(--color-text-secondary)' }}>
        {servers.length} verified external MCP servers · {liveCount} live-testable with no
        credentials. Wire any <strong>Direct</strong>-tier server as a Gateway target; <strong>Adapter</strong>-tier
        servers need a hosted proxy first.
      </p>
      <div className="grid grid-cols-1 gap-3">
        {filtered.map((s) => (
          <div
            key={s.id}
            role="button"
            tabIndex={0}
            onClick={() => setSelectedId(s.id)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                setSelectedId(s.id);
              }
            }}
            className="border border-gray-200 rounded-lg p-4 bg-white hover:border-blue-300 cursor-pointer transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <div className="flex items-center gap-2 mb-1 flex-wrap">
              <h3 className="text-sm font-semibold text-gray-900 tracking-tight">{s.display_name}</h3>
              <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wide ${badgeClass('verified', s.verified)}`}>
                {s.verified}
              </span>
              <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${badgeClass('tier', s.tier)}`}>
                {TIER_LABEL[s.tier] || s.tier}
              </span>
              {s.live_testable && (
                <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 text-[10px] font-medium">
                  no creds
                </span>
              )}
            </div>
            <div className="text-xs text-gray-600 mb-1">
              {s.publisher} · {s.category} · auth: <code className="text-[11px]">{s.auth_type}</code>
            </div>
            {s.endpoint && (
              <div className="text-[11px] font-mono text-gray-400 truncate">{s.endpoint}</div>
            )}
          </div>
        ))}
        {filtered.length === 0 && (
          <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>No MCP servers match.</p>
        )}
      </div>
    </div>
  );
}

function McpDetail({ detail, onBack }: { detail: McpServerDetail; onBack: () => void }) {
  return (
    <div>
      <button
        type="button"
        onClick={onBack}
        className="text-xs mb-3 hover:underline"
        style={{ color: 'var(--accent)' }}
      >
        ← All MCP servers
      </button>
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <h2 className="text-lg font-semibold tracking-tight" style={{ color: 'var(--color-text-primary)' }}>
          {detail.display_name}
        </h2>
        <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium uppercase ${badgeClass('verified', detail.verified)}`}>
          {detail.verified}
        </span>
        <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${badgeClass('tier', detail.tier)}`}>
          {TIER_LABEL[detail.tier] || detail.tier}
        </span>
      </div>
      <div className="space-y-3 text-sm">
        <Row label="Publisher" value={detail.publisher} />
        <Row label="Category" value={detail.category} />
        <Row label="Endpoint" value={detail.endpoint || '— (self-host / adapter required)'} mono />
        <Row label="Auth" value={detail.auth_type} />
        <Row label="Credentials needed" value={detail.credentials_needed} />
        <div>
          <div className="text-xs font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>Example tools</div>
          <div className="flex flex-wrap gap-1">
            {detail.example_tools.map((t) => (
              <code key={t} className="inline-flex items-center px-1.5 py-0.5 rounded bg-gray-100 text-gray-700 text-[11px]">{t}</code>
            ))}
          </div>
        </div>
        <div className="px-3 py-2 rounded-lg text-xs" style={{ background: 'rgba(79,156,255,.08)', border: '1px solid var(--accent)', color: 'var(--color-text-secondary)' }}>
          {detail.tier.startsWith('direct')
            ? 'Direct target: add this endpoint as a Gateway mcpServer target; the platform wires the credential provider from the auth type above.'
            : 'Adapter tier: this server needs a hosted MCP proxy (AgentCore Runtime / container) before it can be a Gateway target — see docs/MCP_GATEWAY_INTEGRATION.md.'}
        </div>
      </div>
    </div>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>{label}: </span>
      <span className={mono ? 'font-mono text-[12px]' : 'text-sm'} style={{ color: 'var(--color-text-primary)', wordBreak: 'break-all' }}>{value}</span>
    </div>
  );
}
