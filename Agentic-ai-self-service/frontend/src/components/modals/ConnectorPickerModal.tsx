/**
 * Phase 3 Gap 3E — ConnectorPickerModal.
 *
 * Lists the pre-built connector catalog grouped by category (using the static
 * data/connectors.ts mirror for instant paint, refreshed by listConnectorsApi)
 * and, on select, fetches getConnectorApi(id) for the credential_schema and
 * renders an auth form:
 *   - api_key  -> one field per credential_schema property
 *   - oauth    -> a "Connect" placeholder button
 *
 * Scope: catalog-only. The credential POST targets the documented Secrets
 * Manager hook (agentcore-connector/{id}/{owner}-{uuid}); until that backend
 * endpoint lands, the submit button is labelled "Coming soon" and disabled so
 * there is no dead button (see design risks). Per-connector execution is a
 * documented follow-up.
 */

import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  CONNECTORS as STATIC_CONNECTORS,
  connectorsByCategory,
  type ConnectorSummary,
} from '../../data/connectors';
import {
  listConnectorsApi,
  getConnectorApi,
  type ConnectorDetail,
} from '../../services/api';

// Set to true once the backend credential POST hook (Secrets Manager) lands.
const CREDENTIAL_HOOK_ENABLED = false;

export interface ConnectorPickerModalProps {
  isOpen: boolean;
  onClose: () => void;
  /**
   * Called when the user confirms a connector + (optionally) supplied
   * credentials. The parent owns wiring this to the canvas / credential hook.
   */
  onSelect?: (selection: {
    connector: ConnectorDetail;
    credentials: Record<string, string>;
  }) => void;
}

interface CredentialField {
  key: string;
  description: string;
}

function credentialFields(detail: ConnectorDetail): CredentialField[] {
  const schema = detail.credential_schema ?? {};
  const props = (schema as Record<string, unknown>).properties;
  if (!props || typeof props !== 'object') return [];
  return Object.entries(props as Record<string, Record<string, unknown>>).map(
    ([key, value]) => ({
      key,
      description: typeof value?.description === 'string' ? value.description : key,
    }),
  );
}

export function ConnectorPickerModal({
  isOpen,
  onClose,
  onSelect,
}: ConnectorPickerModalProps) {
  const [connectors, setConnectors] = useState<ConnectorSummary[]>(STATIC_CONNECTORS);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ConnectorDetail | null>(null);
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Refresh the catalog from the API when opened (static list paints first).
  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    listConnectorsApi()
      .then((fresh) => {
        if (!cancelled && fresh.length) setConnectors(fresh);
      })
      .catch(() => {
        /* keep static mirror on failure */
      });
    return () => {
      cancelled = true;
    };
  }, [isOpen]);

  // Reset selection state whenever the modal closes.
  useEffect(() => {
    if (!isOpen) {
      setSelectedId(null);
      setDetail(null);
      setCredentials({});
      setError(null);
    }
  }, [isOpen]);

  // Escape-to-close, mirroring ConfigurationModal.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [isOpen, onClose]);

  const handlePick = useCallback(async (id: string) => {
    setSelectedId(id);
    setDetail(null);
    setCredentials({});
    setError(null);
    setLoadingDetail(true);
    try {
      const d = await getConnectorApi(id);
      setDetail(d);
    } catch {
      setError('Could not load connector details. Please try again.');
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  const grouped = useMemo(() => {
    // Prefer the (possibly refreshed) API list; fall back to static grouping.
    if (connectors === STATIC_CONNECTORS) return connectorsByCategory();
    const g: Record<string, ConnectorSummary[]> = {};
    for (const c of connectors) (g[c.category] ??= []).push(c);
    return g;
  }, [connectors]);

  const handleSubmit = useCallback(() => {
    if (!detail) return;
    onSelect?.({ connector: detail, credentials });
    onClose();
  }, [detail, credentials, onSelect, onClose]);

  if (!isOpen) return null;

  const fields = detail ? credentialFields(detail) : [];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      data-testid="connector-picker-backdrop"
    >
      <div
        className="bg-white rounded-xl shadow-2xl max-h-[90vh] flex flex-col"
        style={{ width: '560px' }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="connector-picker-title"
        data-testid="connector-picker-modal"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-200">
          <h2
            id="connector-picker-title"
            className="text-base font-semibold text-gray-800 truncate pr-2"
          >
            {detail ? `Connect ${detail.display_name}` : 'Add a connector'}
          </h2>
          <button
            onClick={onClose}
            className="p-2 rounded-lg hover:bg-gray-100 transition-colors"
            aria-label="Close modal"
            data-testid="connector-picker-close"
          >
            <svg className="w-5 h-5 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Content */}
        <div className="overflow-y-auto p-5" style={{ height: '420px' }}>
          {!selectedId && (
            <div className="space-y-5" data-testid="connector-catalog">
              {Object.entries(grouped).map(([category, items]) => (
                <div key={category}>
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">
                    {category}
                  </h3>
                  <div className="grid grid-cols-2 gap-2">
                    {items.map((c) => (
                      <button
                        key={c.id}
                        onClick={() => handlePick(c.id)}
                        className="flex flex-col items-start gap-1 p-3 text-left border border-gray-200 rounded-lg hover:border-blue-400 hover:bg-blue-50 transition-colors"
                        data-testid={`connector-card-${c.id}`}
                      >
                        <span className="text-sm font-medium text-gray-800">
                          {c.display_name}
                        </span>
                        <span className="text-xs text-gray-500">
                          {c.capabilities.join(' · ')}
                        </span>
                        <span className="mt-1 inline-block text-[10px] font-medium px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 uppercase">
                          {c.auth_type === 'oauth' ? 'OAuth' : 'API key'}
                        </span>
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}

          {selectedId && loadingDetail && (
            <div className="flex items-center justify-center h-full text-sm text-gray-500">
              Loading connector…
            </div>
          )}

          {selectedId && error && (
            <div className="flex flex-col items-center justify-center h-full gap-3">
              <p className="text-sm text-red-600">{error}</p>
              <button
                onClick={() => setSelectedId(null)}
                className="text-sm text-blue-600 hover:underline"
              >
                Back to catalog
              </button>
            </div>
          )}

          {selectedId && detail && !loadingDetail && !error && (
            <div className="space-y-4" data-testid="connector-credential-form">
              <p className="text-sm text-gray-600">
                {detail.tool_schemas.length} tool
                {detail.tool_schemas.length === 1 ? '' : 's'} available:{' '}
                {detail.tool_schemas.map((t) => t.name).join(', ')}
              </p>

              {detail.auth_type === 'oauth' ? (
                <button
                  type="button"
                  disabled={!CREDENTIAL_HOOK_ENABLED}
                  className="w-full px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg disabled:bg-blue-300 disabled:cursor-not-allowed"
                  data-testid="connector-oauth-connect"
                >
                  Connect with {detail.display_name}
                  {!CREDENTIAL_HOOK_ENABLED ? ' (coming soon)' : ''}
                </button>
              ) : (
                fields.map((f) => (
                  <label key={f.key} className="block">
                    <span className="block text-xs font-medium text-gray-700 mb-1">
                      {f.key}
                    </span>
                    <input
                      type="password"
                      value={credentials[f.key] ?? ''}
                      onChange={(e) =>
                        setCredentials((prev) => ({ ...prev, [f.key]: e.target.value }))
                      }
                      placeholder={f.description}
                      className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-400"
                      data-testid={`connector-cred-${f.key}`}
                    />
                  </label>
                ))
              )}

              <p className="text-xs text-gray-400">
                Credentials are stored in AWS Secrets Manager scoped to your
                account. Per-connector execution is coming soon.
              </p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between gap-3 px-5 py-3 border-t border-gray-200 bg-gray-50 rounded-b-xl">
          <div>
            {selectedId && (
              <button
                onClick={() => {
                  setSelectedId(null);
                  setDetail(null);
                  setError(null);
                }}
                className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
                data-testid="connector-picker-back"
              >
                Back
              </button>
            )}
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
              data-testid="connector-picker-cancel"
            >
              Cancel
            </button>
            <button
              onClick={handleSubmit}
              disabled={!detail || !CREDENTIAL_HOOK_ENABLED}
              className="px-4 py-2 text-sm font-medium text-white rounded-lg transition-colors bg-blue-600 hover:bg-blue-700 disabled:bg-blue-300 disabled:cursor-not-allowed"
              data-testid="connector-picker-submit"
            >
              {CREDENTIAL_HOOK_ENABLED ? 'Save connector' : 'Coming soon'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default ConnectorPickerModal;
