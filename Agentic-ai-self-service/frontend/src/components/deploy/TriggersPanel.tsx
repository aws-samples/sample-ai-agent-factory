/**
 * Runtime-scoped trigger manager for the Deploy panel.
 *
 * Trigger creation provisions the backing EventBridge/webhook resources and
 * pins them to the exact deployed runtime version selected by the backend.
 * Webhook signing secrets are intentionally held in component memory only:
 * the create response is the sole opportunity to copy one.
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import {
  API_BASE_URL,
  getApiClient,
  getErrorMessage,
  isNotReadyError,
  type CreateTriggerInput,
  type TriggerStatus,
  type TriggerSummary,
} from '../../services/api';

interface TriggersPanelProps {
  runtimeName: string | null;
  /** Refresh trigger — increment to force a reload (e.g. after a new deploy). */
  refreshKey?: number;
}

interface OneTimeWebhookCredentials {
  runtimeName: string;
  triggerId: string;
  endpoint: string;
  signingSecret: string;
}

const STATUS_STYLES: Record<TriggerStatus, string> = {
  active: 'bg-emerald-100 text-emerald-700',
  provisioning: 'bg-blue-100 text-blue-700',
  error: 'bg-red-100 text-red-700',
  deleting: 'bg-amber-100 text-amber-800',
  disabled: 'bg-gray-100 text-gray-600',
  registered: 'bg-amber-100 text-amber-800',
};

function webhookEndpoint(path: string): string {
  const base =
    API_BASE_URL ||
    (typeof window === 'undefined' ? '' : window.location.origin);
  return `${base.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`;
}

function isS3EventPattern(pattern: Record<string, unknown>): boolean {
  const source = pattern.source;
  return (
    Array.isArray(source) &&
    source.length === 1 &&
    source[0] === 'aws.s3'
  );
}

function DetailRow({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="text-[11px] text-gray-700">
      <span className="font-medium">{label}:</span> {children}
    </div>
  );
}

export function TriggersPanel({ runtimeName, refreshKey }: TriggersPanelProps) {
  const [triggers, setTriggers] = useState<TriggerSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [oneTimeWebhook, setOneTimeWebhook] =
    useState<OneTimeWebhookCredentials | null>(null);

  const [createType, setCreateType] =
    useState<CreateTriggerInput['type']>('cron');
  const [cronSchedule, setCronSchedule] = useState('');
  const [eventPattern, setEventPattern] = useState('');
  const [callbackUrl, setCallbackUrl] = useState('');

  const reloadGeneration = useRef(0);
  const createInFlight = useRef(false);
  const deleteInFlight = useRef(false);

  const reload = useCallback(async () => {
    const generation = ++reloadGeneration.current;
    if (!runtimeName) {
      setTriggers([]);
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const loaded = await getApiClient().listTriggers(runtimeName);
      if (reloadGeneration.current === generation) {
        setTriggers(loaded);
      }
    } catch (e) {
      if (reloadGeneration.current !== generation) return;
      // A not-yet-deployed runtime returns 403/404. A 401 is deliberately
      // surfaced because it means the signed-in session is no longer usable.
      if (isNotReadyError(e)) {
        setTriggers([]);
      } else {
        setError(getErrorMessage(e));
      }
    } finally {
      if (reloadGeneration.current === generation) {
        setLoading(false);
      }
    }
  }, [runtimeName]);

  useEffect(() => {
    void reload();
  }, [reload, refreshKey]);

  const handleCreate = async () => {
    if (!runtimeName || createInFlight.current) return;
    setError(null);

    const input: CreateTriggerInput = { type: createType };
    if (createType === 'cron') {
      const schedule = cronSchedule.trim();
      if (!schedule) {
        setError('A cron schedule is required.');
        return;
      }
      input.schedule = schedule;
    }

    if (createType === 'eventbridge' || createType === 's3') {
      if (!eventPattern.trim()) {
        setError('An event pattern is required for EventBridge and S3 triggers.');
        return;
      }
      try {
        const parsed = JSON.parse(eventPattern) as unknown;
        if (
          parsed === null ||
          Array.isArray(parsed) ||
          typeof parsed !== 'object'
        ) {
          throw new Error('Event pattern must be a JSON object.');
        }
        const pattern = parsed as Record<string, unknown>;
        if (createType === 's3' && !isS3EventPattern(pattern)) {
          throw new Error(
            'An S3 trigger pattern must contain exactly "source": ["aws.s3"].',
          );
        }
        input.pattern = pattern;
      } catch (e) {
        setError(
          e instanceof SyntaxError
            ? 'Event pattern must be valid JSON.'
            : e instanceof Error
              ? e.message
              : 'Event pattern must be a JSON object.',
        );
        return;
      }
    }

    if (callbackUrl.trim()) {
      input.webhook_out_url = callbackUrl.trim();
    }

    createInFlight.current = true;
    setCreating(true);
    try {
      const created = await getApiClient().createTrigger(runtimeName, input);
      let responseContractError: string | null = null;
      if (created.type === 'webhook') {
        if (created.webhook_path && created.webhook_signing_secret) {
          setOneTimeWebhook({
            runtimeName,
            triggerId: created.trigger_id,
            endpoint: webhookEndpoint(created.webhook_path),
            signingSecret: created.webhook_signing_secret,
          });
        } else {
          responseContractError =
            'The webhook was created, but its one-time credentials were missing from the response. Delete it and create a new webhook trigger.';
        }
      }

      setCronSchedule('');
      setEventPattern('');
      setCallbackUrl('');
      await reload();
      if (responseContractError) setError(responseContractError);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      createInFlight.current = false;
      setCreating(false);
    }
  };

  const handleDelete = async (triggerId: string) => {
    if (!runtimeName || deleteInFlight.current) return;
    deleteInFlight.current = true;
    setActing(triggerId);
    setError(null);
    try {
      await getApiClient().deleteTrigger(runtimeName, triggerId);
      if (oneTimeWebhook?.triggerId === triggerId) {
        setOneTimeWebhook(null);
      }
      await reload();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      deleteInFlight.current = false;
      setActing(null);
    }
  };

  const copyValue = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      setError(`Could not copy the ${label}. Select and copy it manually.`);
    }
  };

  if (!runtimeName) {
    return (
      <div className="p-5 text-sm text-gray-500">
        Deploy this agent at least once to manage triggers.
      </div>
    );
  }

  const visibleOneTimeWebhook =
    oneTimeWebhook?.runtimeName === runtimeName ? oneTimeWebhook : null;

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h4 className="text-sm font-semibold text-gray-800">Triggers</h4>
          <p className="text-xs text-gray-500">
            Create live cron, EventBridge, S3, or HMAC-authenticated webhook
            triggers. Each trigger stays pinned to the exact deployed runtime
            version selected when it was created; a later promotion does not
            silently retarget it.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void reload()}
          disabled={loading}
          className="shrink-0 text-xs px-2 py-1 rounded border border-gray-200 hover:bg-gray-50 disabled:opacity-50"
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {visibleOneTimeWebhook && (
        <section
          aria-label="One-time webhook credentials"
          className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-3 space-y-2"
        >
          <div className="flex items-start justify-between gap-3">
            <div>
              <h5 className="text-xs font-semibold text-amber-900">
                Save these webhook credentials now
              </h5>
              <p className="text-[11px] text-amber-800">
                The signing secret is shown once and cannot be retrieved after
                this panel is dismissed or reloaded.
              </p>
            </div>
            <button
              type="button"
              onClick={() => setOneTimeWebhook(null)}
              className="text-[11px] text-amber-900 underline"
            >
              Dismiss
            </button>
          </div>

          <div>
            <div className="flex items-center justify-between gap-2">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-amber-900">
                Webhook endpoint
              </span>
              <button
                type="button"
                aria-label="Copy webhook endpoint"
                onClick={() =>
                  void copyValue(
                    visibleOneTimeWebhook.endpoint,
                    'webhook endpoint',
                  )
                }
                className="text-[11px] text-amber-900 underline"
              >
                Copy
              </button>
            </div>
            <code className="block break-all rounded bg-white/70 p-2 text-[11px] text-gray-800">
              {visibleOneTimeWebhook.endpoint}
            </code>
          </div>

          <div>
            <div className="flex items-center justify-between gap-2">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-amber-900">
                Signing secret
              </span>
              <button
                type="button"
                aria-label="Copy webhook signing secret"
                onClick={() =>
                  void copyValue(
                    visibleOneTimeWebhook.signingSecret,
                    'webhook signing secret',
                  )
                }
                className="text-[11px] text-amber-900 underline"
              >
                Copy
              </button>
            </div>
            <code className="block break-all rounded bg-white/70 p-2 text-[11px] text-gray-800">
              {visibleOneTimeWebhook.signingSecret}
            </code>
          </div>

          <p className="text-[11px] text-amber-900">
            Send the Unix timestamp, a unique delivery ID, and{' '}
            <code>X-AgentCore-Signature</code> headers. The signature is{' '}
            <code>
              v1=HMAC-SHA256(secret, timestamp.delivery-id.raw-body)
            </code>
            .
          </p>
        </section>
      )}

      <div className="rounded-lg border border-gray-200 bg-white px-3 py-3 space-y-2.5">
        <div className="space-y-1">
          <label
            htmlFor="trigger-type"
            className="text-xs font-medium text-gray-700"
          >
            Type
          </label>
          <select
            id="trigger-type"
            value={createType}
            onChange={(e) =>
              setCreateType(e.target.value as CreateTriggerInput['type'])
            }
            disabled={creating}
            className="w-full text-xs px-2 py-1.5 rounded border border-gray-200 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
          >
            <option value="cron">Cron</option>
            <option value="webhook">Webhook</option>
            <option value="eventbridge">EventBridge</option>
            <option value="s3">S3</option>
          </select>
        </div>

        {createType === 'cron' && (
          <div className="space-y-1">
            <label
              htmlFor="cron-schedule"
              className="text-xs font-medium text-gray-700"
            >
              Schedule
            </label>
            <input
              id="cron-schedule"
              type="text"
              value={cronSchedule}
              onChange={(e) => setCronSchedule(e.target.value)}
              disabled={creating}
              placeholder="cron(0 9 * * ? *)"
              className="w-full text-xs px-2 py-1.5 rounded border border-gray-200 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none font-mono"
            />
            <p className="text-[11px] text-gray-500">
              Use an EventBridge cron expression in UTC.
            </p>
          </div>
        )}

        {(createType === 'eventbridge' || createType === 's3') && (
          <div className="space-y-1">
            <label
              htmlFor="trigger-event-pattern"
              className="text-xs font-medium text-gray-700"
            >
              Event pattern (JSON)
            </label>
            <textarea
              id="trigger-event-pattern"
              value={eventPattern}
              onChange={(e) => setEventPattern(e.target.value)}
              disabled={creating}
              rows={6}
              placeholder={
                createType === 's3'
                  ? '{"source":["aws.s3"],"detail":{"bucket":{"name":["my-bucket"]}}}'
                  : '{"source":["my.application"],"detail-type":["Order created"]}'
              }
              className="w-full resize-y text-xs px-2 py-1.5 rounded border border-gray-200 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none font-mono"
            />
            {createType === 's3' && (
              <p className="text-[11px] text-amber-700">
                Enable Amazon EventBridge notifications on every matching S3
                bucket first. The pattern must contain exactly{' '}
                <code>&quot;source&quot;: [&quot;aws.s3&quot;]</code>.
              </p>
            )}
          </div>
        )}

        <div className="space-y-1">
          <label
            htmlFor="trigger-callback-url"
            className="text-xs font-medium text-gray-700"
          >
            Result callback URL (optional)
          </label>
          <input
            id="trigger-callback-url"
            type="url"
            value={callbackUrl}
            onChange={(e) => setCallbackUrl(e.target.value)}
            disabled={creating}
            placeholder="https://example.com/agent-result"
            className="w-full text-xs px-2 py-1.5 rounded border border-gray-200 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
          />
          <p className="text-[11px] text-gray-500">
            After the agent runs, the platform makes a best-effort POST of the
            result to this public HTTPS endpoint.
          </p>
        </div>

        <button
          type="button"
          onClick={() => void handleCreate()}
          disabled={creating}
          className="text-xs px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {creating ? 'Creating…' : 'Add trigger'}
        </button>
      </div>

      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
        >
          {error}
        </div>
      )}

      {loading && triggers.length === 0 ? (
        <div className="text-xs text-gray-500">Loading triggers…</div>
      ) : triggers.length === 0 ? (
        <div className="text-xs text-gray-500">No triggers yet.</div>
      ) : (
        <ul className="space-y-2">
          {triggers.map((trigger) => {
            const endpoint = trigger.webhook_path
              ? webhookEndpoint(trigger.webhook_path)
              : null;
            const retryDelete =
              trigger.status === 'deleting' || trigger.status === 'error';

            return (
              <li
                key={trigger.trigger_id}
                className="rounded-lg border border-gray-200 bg-white px-3 py-2.5 text-xs space-y-1.5"
              >
                <div className="flex items-center gap-2">
                  <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-blue-100 text-blue-700">
                    {trigger.type}
                  </span>
                  <span
                    className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${
                      STATUS_STYLES[trigger.status]
                    }`}
                  >
                    {trigger.status}
                  </span>
                </div>

                {trigger.status === 'registered' && (
                  <p className="text-[11px] text-amber-700">
                    This legacy definition has no provisioned AWS resource.
                    Delete and recreate it to make it live.
                  </p>
                )}

                {trigger.last_error_code && (
                  <DetailRow label="Error code">
                    <code className="font-mono text-red-700">
                      {trigger.last_error_code}
                    </code>
                  </DetailRow>
                )}

                {trigger.schedule && (
                  <DetailRow label="Schedule">
                    <code className="font-mono bg-gray-50 px-1 py-0.5 rounded">
                      {trigger.schedule}
                    </code>
                  </DetailRow>
                )}

                {trigger.pattern && (
                  <div className="text-[11px] text-gray-700">
                    <span className="font-medium">Event pattern:</span>
                    <pre className="mt-1 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-50 p-1.5 font-mono">
                      {JSON.stringify(trigger.pattern, null, 2)}
                    </pre>
                  </div>
                )}

                {endpoint && (
                  <DetailRow label="Webhook endpoint">
                    <code className="font-mono break-all">{endpoint}</code>
                  </DetailRow>
                )}

                {trigger.webhook_out_url && (
                  <DetailRow label="Result callback">
                    <code className="font-mono break-all">
                      {trigger.webhook_out_url}
                    </code>
                  </DetailRow>
                )}

                <DetailRow label="Pinned runtime">
                  <code className="font-mono break-all">
                    {trigger.target_runtime_arn}
                  </code>
                </DetailRow>

                <div className="text-[11px] text-gray-500">
                  Created {new Date(trigger.created_at).toLocaleString()}
                </div>

                <div className="pt-1">
                  <button
                    type="button"
                    onClick={() => void handleDelete(trigger.trigger_id)}
                    disabled={acting !== null}
                    className="text-[11px] px-2 py-0.5 rounded border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-40"
                  >
                    {acting === trigger.trigger_id
                      ? 'Deleting…'
                      : retryDelete
                        ? 'Retry delete'
                        : 'Delete'}
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
