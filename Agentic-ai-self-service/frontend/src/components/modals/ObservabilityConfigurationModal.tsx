/**
 * ObservabilityConfiguration modal — configures OTLP export to the
 * AgentCore-native CloudWatch sidecar, Langfuse, or any other OTLP-HTTP
 * backend via the custom provider.
 *
 * Credentials are POSTed to /api/observability/credentials, which writes
 * them to AWS Secrets Manager and returns an ARN. The agent runtime
 * resolves the secret at boot via boto3, so plaintext keys never live in
 * runtime environment variables.
 */

import { useState, useCallback, useMemo, useEffect } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, SelectField, SliderField, FormSection, CheckboxField } from './FormFields';
import type {
  ObservabilityConfiguration,
  ObservabilityProvider,
} from '../../types/components';

interface PlatformDefaults {
  enabled: boolean;
  endpoint?: string;
  sample_rate?: number;
  service_name_prefix?: string;
}

// ============================================================================
// Provider presets
// ============================================================================

interface ProviderPreset {
  value: ObservabilityProvider;
  label: string;
  description: string;
  defaultEndpoint?: string;
  authFields: Array<{
    key: 'public_key' | 'secret_key' | 'api_key' | 'header_value';
    label: string;
    placeholder: string;
    type?: 'password';
  }>;
}

const PROVIDER_PRESETS: ProviderPreset[] = [
  {
    value: 'langfuse',
    label: 'Langfuse Cloud',
    description: 'LLM observability platform. Auto-parses GenAI semantic conventions for cost & token rollups.',
    defaultEndpoint: 'https://cloud.langfuse.com/api/public/otel',
    authFields: [
      { key: 'public_key', label: 'Public Key', placeholder: 'pk-lf-...' },
      { key: 'secret_key', label: 'Secret Key', placeholder: 'sk-lf-...', type: 'password' },
    ],
  },
  {
    value: 'custom',
    label: 'Custom OTLP Endpoint',
    description: 'Any other OTLP-HTTP backend. Provide the auth header verbatim (Header-Name=Value).',
    authFields: [
      { key: 'header_value', label: 'Auth Header (Header=Value)', placeholder: 'Authorization=Bearer xyz' },
    ],
  },
];

const PROVIDER_OPTIONS = PROVIDER_PRESETS.map((p) => ({ value: p.value, label: p.label }));

// ============================================================================
// Defaults
// ============================================================================

export function createDefaultObservabilityConfig(): ObservabilityConfiguration {
  return {
    name: 'Observability',
    enableOtel: true,
    provider: 'langfuse',
    otlpEndpoint: 'https://cloud.langfuse.com/api/public/otel',
    otlpProtocol: 'http/protobuf',
    serviceName: undefined,
    sampleRate: 1.0,
    resourceAttributes: {},
    extraHeaders: {},
  };
}

// ============================================================================
// Props
// ============================================================================

export interface ObservabilityConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: ObservabilityConfiguration) => void;
  initialConfig?: Partial<ObservabilityConfiguration>;
  apiBaseUrl?: string;
}

// ============================================================================
// Component
// ============================================================================

export function ObservabilityConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
  apiBaseUrl = '',
}: ObservabilityConfigurationModalProps) {
  const [config, setConfig] = useState<ObservabilityConfiguration>(() => ({
    ...createDefaultObservabilityConfig(),
    ...initialConfig,
  }));

  // Local credential state — never stored in config; sent to the secret API
  // and replaced with the returned ARN.
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [credentialError, setCredentialError] = useState<string | null>(null);
  const [savingCredentials, setSavingCredentials] = useState(false);

  // Platform-managed OTEL defaults (admin-set at deploy time). When enabled,
  // endpoint/secret/sample are LOCKED — the user can only edit resource
  // attributes. Fetched once when the modal opens.
  const [platformDefaults, setPlatformDefaults] = useState<PlatformDefaults>({ enabled: false });

  useEffect(() => {
    if (isOpen) {
      setConfig({ ...createDefaultObservabilityConfig(), ...initialConfig });
      setCredentials({});
      setCredentialError(null);
      // Fire-and-forget: silently fall back to per-canvas mode on error.
      fetch(`${apiBaseUrl}/api/observability/platform-defaults`)
        .then((r) => (r.ok ? r.json() : { enabled: false }))
        .then((data: PlatformDefaults) => setPlatformDefaults(data))
        .catch(() => setPlatformDefaults({ enabled: false }));
    }
  }, [isOpen, initialConfig, apiBaseUrl]);

  const preset = useMemo(
    () => PROVIDER_PRESETS.find((p) => p.value === config.provider) ?? PROVIDER_PRESETS[0],
    [config.provider]
  );

  const update = useCallback(<K extends keyof ObservabilityConfiguration>(
    key: K,
    value: ObservabilityConfiguration[K]
  ) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  }, []);

  const handleProviderChange = useCallback((newProvider: string) => {
    const next = PROVIDER_PRESETS.find((p) => p.value === newProvider) ?? PROVIDER_PRESETS[0];
    setConfig((prev) => ({
      ...prev,
      provider: next.value,
      otlpEndpoint: next.defaultEndpoint ?? prev.otlpEndpoint,
      authHeaderSecretArn: undefined,
    }));
    setCredentials({});
    setCredentialError(null);
  }, []);

  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];
    if (config.enableOtel && !config.otlpEndpoint?.trim()) {
      errors.push({ field: 'otlpEndpoint', message: 'OTLP endpoint is required when telemetry is enabled' });
    }
    return errors;
  }, [config]);

  const handleStoreCredentials = useCallback(async () => {
    setSavingCredentials(true);
    setCredentialError(null);
    try {
      const body: Record<string, unknown> = { provider: config.provider };
      preset.authFields.forEach(({ key }) => {
        if (credentials[key]) body[key] = credentials[key];
      });
      const resp = await fetch(`${apiBaseUrl}/api/observability/credentials`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({}));
        throw new Error(detail?.detail || `Failed (${resp.status})`);
      }
      const data = (await resp.json()) as { secret_arn: string };
      update('authHeaderSecretArn', data.secret_arn);
      setCredentials({});
    } catch (err) {
      setCredentialError(err instanceof Error ? err.message : 'Could not store credentials');
    } finally {
      setSavingCredentials(false);
    }
  }, [apiBaseUrl, config.provider, credentials, preset.authFields, update]);

  const handleSave = useCallback(() => onSave(config), [config, onSave]);

  // -- Tab 1: Backend --
  // When platform-managed OTEL is on, the operator has set the endpoint /
  // secret / sample rate at deploy time and per-canvas overrides are dropped
  // server-side. Show the values read-only and hide the credentials section.
  const backendTab = platformDefaults.enabled ? (
    <div className="space-y-5">
      <div className="rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900">
        <div className="font-medium">Platform-managed observability</div>
        <p className="mt-1">
          Every agent deployed by this platform sends traces to the admin-configured backend.
          The endpoint, credentials, and sample rate cannot be changed per agent. You can still
          add custom <span className="font-mono">resource_attributes</span> on the next tab — those
          are merged on top of the platform defaults.
        </p>
      </div>

      <FormSection title="Backend (platform-managed)">
        <TextField
          id="otlpEndpoint"
          label="OTLP Endpoint URL"
          value={platformDefaults.endpoint ?? ''}
          onChange={() => { /* read-only */ }}
          disabled
          helpText="Set at platform deploy time via OTEL_ENDPOINT."
        />
      </FormSection>
    </div>
  ) : (
    <div className="space-y-5">
      <FormSection title="Telemetry">
        <CheckboxField
          id="enableOtel"
          label="Enable OTLP telemetry"
          checked={config.enableOtel}
          onChange={(checked) => update('enableOtel', checked)}
          helpText="Emit OpenTelemetry traces to your selected backend."
        />
      </FormSection>

      <FormSection title="Backend">
        <SelectField
          id="provider"
          label="Provider"
          value={config.provider}
          onChange={handleProviderChange}
          options={PROVIDER_OPTIONS}
        />
        <p className="text-xs text-gray-500 mt-1">{preset.description}</p>

        <TextField
          id="otlpEndpoint"
          label="OTLP Endpoint URL"
          value={config.otlpEndpoint ?? ''}
          onChange={(v) => update('otlpEndpoint', v)}
          placeholder="https://cloud.langfuse.com/api/public/otel"
          required={config.enableOtel}
        />

        <SelectField
          id="otlpProtocol"
          label="Protocol"
          value={config.otlpProtocol}
          onChange={(v) => update('otlpProtocol', v as 'http/protobuf' | 'grpc')}
          options={[
            { value: 'http/protobuf', label: 'HTTP/protobuf (recommended)' },
            { value: 'grpc', label: 'gRPC' },
          ]}
        />
      </FormSection>

      {preset.authFields.length > 0 && (
        <FormSection title="Authentication">
          {config.authHeaderSecretArn ? (
            <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
              <div className="font-medium">Credentials stored in AWS Secrets Manager</div>
              <div className="font-mono text-xs mt-1 break-all">{config.authHeaderSecretArn}</div>
              <button
                type="button"
                className="mt-2 text-xs text-emerald-700 hover:underline"
                onClick={() => update('authHeaderSecretArn', undefined)}
              >
                Replace credentials
              </button>
            </div>
          ) : (
            <div className="space-y-3">
              {preset.authFields.map((field) => (
                <TextField
                  key={field.key}
                  id={`cred-${field.key}`}
                  label={field.label}
                  value={credentials[field.key] ?? ''}
                  onChange={(v) => setCredentials((c) => ({ ...c, [field.key]: v }))}
                  placeholder={field.placeholder}
                  type={field.type === 'password' ? 'password' : 'text'}
                />
              ))}
              <button
                type="button"
                disabled={savingCredentials || preset.authFields.some((f) => !credentials[f.key]?.trim())}
                onClick={handleStoreCredentials}
                className="rounded-md bg-console-blue px-3 py-1.5 text-sm font-medium text-white hover:bg-console-blue-hover disabled:opacity-50"
              >
                {savingCredentials ? 'Storing in Secrets Manager…' : 'Store credentials'}
              </button>
              {credentialError && (
                <div className="text-xs text-red-600">{credentialError}</div>
              )}
              <p className="text-xs text-gray-500">
                Credentials are written to AWS Secrets Manager. The runtime resolves them at boot —
                they never appear in plaintext environment variables.
              </p>
            </div>
          )}
        </FormSection>
      )}
    </div>
  );

  // -- Tab 2: Sampling & resource --
  const tuningTab = (
    <div className="space-y-5">
      <FormSection title="Sampling">
        {platformDefaults.enabled ? (
          <div className="text-sm text-gray-700">
            Platform-managed sample rate:{' '}
            <span className="font-mono">
              {Math.round((platformDefaults.sample_rate ?? 1.0) * 100)}%
            </span>
          </div>
        ) : (
          <SliderField
            id="sampleRate"
            label="Trace sample rate"
            value={Math.round(config.sampleRate * 100)}
            onChange={(v) => update('sampleRate', v / 100)}
            min={0}
            max={100}
            step={5}
            helpText="Percentage of invocations sampled. 100% = every invocation traced."
          />
        )}
      </FormSection>

      <FormSection title="Resource Attributes">
        <TextField
          id="serviceName"
          label="Service name"
          value={config.serviceName ?? ''}
          onChange={(v) => update('serviceName', v || undefined)}
          placeholder="my-agent (defaults to runtime name)"
          disabled={platformDefaults.enabled}
          helpText={platformDefaults.enabled
            ? `Will be sent as ${platformDefaults.service_name_prefix ?? 'platform'}-{this name}.`
            : undefined}
        />
        <ResourceAttributesEditor
          attrs={config.resourceAttributes}
          onChange={(next) => update('resourceAttributes', next)}
        />
      </FormSection>

    </div>
  );

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title={`Configure Observability: ${config.name}`}
      tabs={[
        { id: 'backend', label: 'Backend', content: backendTab },
        { id: 'tuning', label: 'Sampling & Resource', content: tuningTab },
      ]}
      validationErrors={validationErrors}
    />
  );
}

// ============================================================================
// Resource Attributes editor (key/value rows)
// ============================================================================

function ResourceAttributesEditor({
  attrs,
  onChange,
}: {
  attrs: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
}) {
  const entries = Object.entries(attrs);

  return (
    <div className="space-y-2">
      {entries.map(([k, v], idx) => (
        <div key={`${k}-${idx}`} className="flex gap-2 items-center">
          <input
            type="text"
            value={k}
            onChange={(e) => {
              const next: Record<string, string> = {};
              entries.forEach(([kk, vv], i) => { next[i === idx ? e.target.value : kk] = vv; });
              onChange(next);
            }}
            placeholder="key (e.g. env)"
            className="flex-1 px-2 py-1 text-sm border rounded"
          />
          <input
            type="text"
            value={v}
            onChange={(e) => onChange({ ...attrs, [k]: e.target.value })}
            placeholder="value (e.g. prod)"
            className="flex-1 px-2 py-1 text-sm border rounded"
          />
          <button
            type="button"
            onClick={() => {
              const next = { ...attrs };
              delete next[k];
              onChange(next);
            }}
            className="text-xs text-red-600 hover:underline"
          >
            Remove
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={() => onChange({ ...attrs, '': '' })}
        className="text-xs text-console-blue hover:underline"
      >
        + Add attribute
      </button>
    </div>
  );
}

export default ObservabilityConfigurationModal;
