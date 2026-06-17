/**
 * TriggersPanel — Phase 3 Gap 3F frontend.
 *
 * Runtime-scoped scheduled/event-trigger manager for the DeployPanel.
 * Lists existing triggers (cron, webhook, EventBridge, S3), allows creating
 * new triggers, and deleting existing ones. Surfaces API errors (e.g. bad cron
 * syntax, SSRF-blocked URLs) in the error box.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  getApiClient,
  getErrorMessage,
  isNotReadyError,
  type TriggerSummary,
  type CreateTriggerInput,
} from '../../services/api';

interface TriggersPanelProps {
  runtimeName: string | null;
  /** Refresh trigger — increment to force a reload (e.g. after a new deploy). */
  refreshKey?: number;
}

export function TriggersPanel({ runtimeName, refreshKey }: TriggersPanelProps) {
  const [triggers, setTriggers] = useState<TriggerSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null); // trigger_id being deleted

  // Create form state
  const [createType, setCreateType] = useState<CreateTriggerInput['type']>('cron');
  const [cronSchedule, setCronSchedule] = useState('');
  const [webhookUrl, setWebhookUrl] = useState('');

  const reload = useCallback(async () => {
    if (!runtimeName) {
      setTriggers([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const api = getApiClient();
      const ts = await api.listTriggers(runtimeName);
      setTriggers(ts);
    } catch (e) {
      // A not-yet-deployed runtime returns 404/401 — that's an empty state, not
      // an error the user should see (Bug 136).
      if (isNotReadyError(e)) {
        setTriggers([]);
      } else {
        setError(getErrorMessage(e));
      }
    } finally {
      setLoading(false);
    }
  }, [runtimeName]);

  useEffect(() => {
    void reload();
  }, [reload, refreshKey]);

  const handleCreate = async () => {
    if (!runtimeName) return;
    setError(null);
    const input: CreateTriggerInput = { type: createType };
    if (createType === 'cron' && cronSchedule.trim()) {
      input.schedule = cronSchedule.trim();
    }
    if (createType === 'webhook' && webhookUrl.trim()) {
      input.webhook_out_url = webhookUrl.trim();
    }
    try {
      await getApiClient().createTrigger(runtimeName, input);
      // Reset form
      setCronSchedule('');
      setWebhookUrl('');
      await reload();
    } catch (e) {
      setError(getErrorMessage(e));
    }
  };

  const handleDelete = async (triggerId: string) => {
    if (!runtimeName) return;
    setActing(triggerId);
    setError(null);
    try {
      await getApiClient().deleteTrigger(runtimeName, triggerId);
      await reload();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setActing(null);
    }
  };

  if (!runtimeName) {
    return (
      <div className="p-5 text-sm text-gray-500">
        Deploy this agent at least once to manage triggers.
      </div>
    );
  }

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h4 className="text-sm font-semibold text-gray-800">Triggers</h4>
          <p className="text-xs text-gray-500">
            Define cron, EventBridge, S3, or webhook triggers for this agent.
            Triggers are <span className="font-medium">recorded</span> here; a
            trigger shows <span className="font-medium">registered</span> until
            its AWS resource is provisioned, and only fires once it turns{' '}
            <span className="font-medium text-emerald-700">active</span>.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void reload()}
          disabled={loading}
          className="text-xs px-2 py-1 rounded border border-gray-200 hover:bg-gray-50 disabled:opacity-50"
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {/* Create form */}
      <div className="rounded-lg border border-gray-200 px-3 py-3 space-y-2.5 bg-white">
        <div className="flex items-end gap-2">
          <div className="flex-1 space-y-1">
            <label htmlFor="trigger-type" className="text-xs font-medium text-gray-700">
              Type
            </label>
            <select
              id="trigger-type"
              value={createType}
              onChange={(e) => setCreateType(e.target.value as CreateTriggerInput['type'])}
              className="w-full text-xs px-2 py-1.5 rounded border border-gray-200 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
            >
              <option value="cron">Cron</option>
              <option value="webhook">Webhook</option>
              <option value="eventbridge">EventBridge</option>
              <option value="s3">S3</option>
            </select>
          </div>
        </div>

        {createType === 'cron' && (
          <div className="space-y-1">
            <label htmlFor="cron-schedule" className="text-xs font-medium text-gray-700">
              Schedule
            </label>
            <input
              id="cron-schedule"
              type="text"
              value={cronSchedule}
              onChange={(e) => setCronSchedule(e.target.value)}
              placeholder="cron(0 9 * * ? *)"
              className="w-full text-xs px-2 py-1.5 rounded border border-gray-200 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none font-mono"
            />
          </div>
        )}

        {createType === 'webhook' && (
          <div className="space-y-1">
            <label htmlFor="webhook-url" className="text-xs font-medium text-gray-700">
              Webhook URL (optional)
            </label>
            <input
              id="webhook-url"
              type="text"
              value={webhookUrl}
              onChange={(e) => setWebhookUrl(e.target.value)}
              placeholder="https://example.com/webhook"
              className="w-full text-xs px-2 py-1.5 rounded border border-gray-200 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
            />
          </div>
        )}

        <button
          type="button"
          onClick={() => void handleCreate()}
          className="text-xs px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
        >
          Add trigger
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </div>
      )}

      {loading && triggers.length === 0 ? (
        <div className="text-xs text-gray-500">Loading triggers…</div>
      ) : triggers.length === 0 ? (
        <div className="text-xs text-gray-500">No triggers yet.</div>
      ) : (
        <ul className="space-y-2">
          {triggers.map((t) => (
            <li
              key={t.trigger_id}
              className="rounded-lg border border-gray-200 bg-white px-3 py-2.5 text-xs space-y-1"
            >
              <div className="flex items-center gap-2">
                <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-blue-100 text-blue-700">
                  {t.type}
                </span>
                <span
                  className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${
                    t.status === 'active'
                      ? 'bg-emerald-100 text-emerald-700'
                      : 'bg-gray-100 text-gray-600'
                  }`}
                >
                  {t.status}
                </span>
              </div>

              {t.schedule && (
                <div className="text-[11px] text-gray-700">
                  <span className="font-medium">Schedule:</span>{' '}
                  <code className="font-mono bg-gray-50 px-1 py-0.5 rounded">
                    {t.schedule}
                  </code>
                </div>
              )}

              {t.webhook_secret_ref && (
                <div className="text-[11px] text-gray-700">
                  <span className="font-medium">Secret:</span>{' '}
                  <code className="font-mono bg-gray-50 px-1 py-0.5 rounded">
                    {t.webhook_secret_ref.length > 40
                      ? `${t.webhook_secret_ref.slice(0, 40)}…`
                      : t.webhook_secret_ref}
                  </code>
                </div>
              )}

              <div className="text-[11px] text-gray-500">
                {new Date(t.created_at).toLocaleString()}
              </div>

              <div className="pt-1">
                <button
                  type="button"
                  onClick={() => void handleDelete(t.trigger_id)}
                  disabled={acting !== null}
                  className="text-[11px] px-2 py-0.5 rounded border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-40"
                >
                  {acting === t.trigger_id ? 'Deleting…' : 'Delete'}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
