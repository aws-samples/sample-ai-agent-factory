/**
 * Phase B — AgentCore Harness authoring.
 *
 * A parallel, FORM-based authoring path (the visual canvas stays the default
 * and is untouched). Instead of wiring nodes on a canvas, the user fills a
 * compact config form: model provider/id, instructions (system prompt), a
 * memory toggle, and a tools section that REUSES the Phase A connector catalog
 * + built-in tools (PALETTE_ITEMS) — no duplicated tool/connector definitions.
 *
 * On deploy this maps the form onto the SAME deploy-payload shape the canvas
 * path emits (a RuntimeConfiguration-shaped `config`, plus `connectors` /
 * `connectedTools` / `gatewayTools` / `memoryConfig`), and reuses DeployPanel
 * with deploymentMode="harness" so the backend receives deployment_mode and
 * the existing status polling / chat / monitor UI works unchanged.
 */

import { useMemo, useState, useRef } from 'react';
import { SelectField, TextField, TextArea, FormSection, Toggle } from '../modals/FormFields';
import {
  PROVIDER_OPTIONS,
  getModelsForProvider,
  createDefaultRuntimeConfig,
} from '../../utils/runtimeConfig';
import { PALETTE_ITEMS } from '../palette/ComponentPalette';
import { CONNECTOR_TOOL_PREFIX } from '../../types/components';
import type { RuntimeConfiguration, StrandsModelProvider, ConnectorConfiguration } from '../../types/components';
import { DeployPanel } from '../deploy/DeployPanel';
import type { DeployConnector } from '../deploy/DeployPanel';
import { ConnectorConfigModal } from '../modals/ConnectorConfigModal';

// Harness names follow the backend regex [a-zA-Z][a-zA-Z0-9_]{0,39}
// (underscores only, <=40 chars, must start with a letter). We sanitize here
// so the name surfaced in the form is already deploy-safe; harness_deployer
// re-sanitizes server-side as the source of truth.
function sanitizeHarnessName(raw: string): string {
  const cleaned = raw.replace(/[^a-zA-Z0-9_]/g, '_').replace(/^[^a-zA-Z]+/, '');
  return (cleaned || 'agent_harness').slice(0, 40);
}

// Built-in (gateway) tools vs. SaaS connectors both live in PALETTE_ITEMS as
// `tool`-typed entries; connectors are the ones whose toolId is prefixed with
// "connector:". Split them so the form can render two tidy sections that mirror
// the palette's own "Tools" / "Connectors" grouping.
const BUILTIN_TOOLS = PALETTE_ITEMS.filter(
  (i) => i.type === 'tool' && i.toolId && !i.toolId.startsWith(CONNECTOR_TOOL_PREFIX) && i.toolId !== 'knowledge_base',
);
const CONNECTOR_TOOLS = PALETTE_ITEMS.filter(
  (i) => i.type === 'tool' && i.toolId?.startsWith(CONNECTOR_TOOL_PREFIX),
);

export function HarnessAuthoring() {
  const defaults = useMemo(() => createDefaultRuntimeConfig(), []);

  const [name, setName] = useState('agent_harness');
  const [provider, setProvider] = useState<StrandsModelProvider>(defaults.model.provider);
  const [modelId, setModelId] = useState(defaults.model.modelId);
  const [instructions, setInstructions] = useState('');
  const [memoryEnabled, setMemoryEnabled] = useState(false);
  // Selected built-in gateway tools (by toolId) and SaaS connectors (by id).
  const [selectedTools, setSelectedTools] = useState<Set<string>>(new Set());
  const [selectedConnectors, setSelectedConnectors] = useState<Set<string>>(new Set());
  // Bug 193b — a connector needs credentials (api key / OAuth) BEFORE it can
  // deploy. The harness form previously sent connector_id with no secret, so any
  // harness+connector deploy failed ("requires a secret_arn or secret_value").
  // Clicking a connector chip now opens the same ConnectorConfigModal the canvas
  // uses; the full config (incl. the transient secretValue) is held HERE in an
  // in-memory ref keyed by chip id (never persisted) and read into connectors[].
  const connectorConfigsRef = useRef<Record<string, ConnectorConfiguration>>({});
  const [connectorModalId, setConnectorModalId] = useState<string | null>(null);
  // Bump to force the connectors useMemo to recompute after a modal save.
  const [connectorRev, setConnectorRev] = useState(0);

  const [showDeployPanel, setShowDeployPanel] = useState(false);

  const availableModels = useMemo(() => getModelsForProvider(provider), [provider]);
  const providerInfo = useMemo(
    () => PROVIDER_OPTIONS.find((p) => p.value === provider),
    [provider],
  );

  const handleProviderChange = (next: string) => {
    const nextProvider = next as StrandsModelProvider;
    setProvider(nextProvider);
    const models = getModelsForProvider(nextProvider);
    setModelId(models[0]?.modelId ?? '');
  };

  const toggleSet = (
    setter: React.Dispatch<React.SetStateAction<Set<string>>>,
    key: string,
  ) => {
    setter((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  // Map the form onto a RuntimeConfiguration so the deploy payload shape is
  // identical to the canvas path. The harness backend reads name / model /
  // systemPrompt from `config`; connectors+memory flow through the same keys.
  const deployConfig: RuntimeConfiguration = useMemo(
    () => ({
      ...defaults,
      name: sanitizeHarnessName(name),
      systemPrompt: instructions,
      modelProvider: provider,
      model: { ...defaults.model, provider, modelId },
    }),
    [defaults, name, instructions, provider, modelId],
  );

  // Connected built-in tools wire through the gateway exactly like the canvas
  // path's gatewayTools; a `gateway` entry in connectedTools signals one is
  // needed. Memory toggles the memory tool + memoryConfig the harness step reads.
  const gatewayTools = useMemo(() => Array.from(selectedTools), [selectedTools]);
  const connectors: DeployConnector[] = useMemo(
    () =>
      Array.from(selectedConnectors).map((id) => {
        const connectorId = id.slice(CONNECTOR_TOOL_PREFIX.length);
        const cfg = connectorConfigsRef.current[id];
        const isOauth = cfg?.authMethod === 'oauth2_cc';
        return {
          connector_id: cfg?.connectorId || connectorId,
          auth_method: (cfg?.authMethod ?? 'api_key') as DeployConnector['auth_method'],
          // Transient secret from the modal — minted into Secrets Manager
          // backend-side then dropped; never persisted client-side.
          secret_value: cfg?.secretValue || undefined,
          secret_arn: cfg?.secretArn || undefined,
          spec_url: cfg?.specUrl || undefined,
          spec_inline: cfg?.specContent || undefined,
          scopes: isOauth ? (cfg?.scopes || []) : undefined,
          client_id: isOauth ? (cfg?.clientId || undefined) : undefined,
          oauth_vendor: isOauth ? (cfg?.oauthVendor || undefined) : undefined,
          discovery_url: isOauth ? (cfg?.discoveryUrl || undefined) : undefined,
          credential_location: !isOauth ? (cfg?.credentialLocation || undefined) : undefined,
          credential_parameter_name: !isOauth ? (cfg?.credentialParameterName || undefined) : undefined,
          credential_prefix: !isOauth ? (cfg?.credentialPrefix || undefined) : undefined,
        };
      }),
    // connectorRev forces recompute after a modal save mutates the ref.
    [selectedConnectors, connectorRev],
  );

  // Whether every selected connector has been configured with a credential.
  const connectorsNeedingConfig = useMemo(
    () =>
      Array.from(selectedConnectors).filter((id) => {
        const cfg = connectorConfigsRef.current[id];
        return !cfg || (!cfg.secretValue && !cfg.secretArn);
      }),
    [selectedConnectors, connectorRev],
  );

  const connectedTools = useMemo(() => {
    const tools: string[] = [];
    if (memoryEnabled) tools.push('memory');
    if (gatewayTools.length > 0 || connectors.length > 0) tools.push('gateway');
    return tools;
  }, [memoryEnabled, gatewayTools, connectors]);

  const memoryConfig = memoryEnabled ? { enabled: true } : null;

  const canDeploy =
    !!deployConfig.name && !!instructions.trim() && !!modelId
    && connectorsNeedingConfig.length === 0;

  return (
    <div className="flex-1 relative flex flex-col bg-[#f2f3f3]">
      {/* Top header bar — mirrors the visual-canvas header */}
      <div className="h-12 bg-[#232f3e] flex items-center justify-between px-4 z-20 border-b border-white/10">
        <div className="flex items-center gap-2 text-xs text-white/60">
          <span className="px-2 py-0.5 bg-white/10 rounded font-medium">Harness Authoring</span>
        </div>
        <button
          onClick={() => setShowDeployPanel(true)}
          disabled={!canDeploy}
          className={`
            px-4 py-1.5 rounded-md font-semibold transition-all duration-200 flex items-center gap-2 text-sm
            ${canDeploy
              ? 'bg-[#ff9900] text-[#232f3e] hover:bg-[#ec7211] hover:scale-105 active:scale-95'
              : 'bg-white/10 text-white/30 cursor-not-allowed'}
          `}
          style={{
            boxShadow: canDeploy ? '0 1px 2px rgba(0, 0, 0, 0.2), 0 2px 4px rgba(255, 153, 0, 0.3)' : 'none',
            transitionTimingFunction: 'var(--ease-out-quint)',
          }}
          title={
            connectorsNeedingConfig.length > 0
              ? 'Add credentials to the highlighted connector(s) first'
              : !canDeploy
                ? 'Set a name, model, and instructions first'
                : 'Deploy as AgentCore Harness'
          }
          aria-label={!canDeploy ? 'Complete required fields before deploying' : 'Deploy agent as AgentCore Harness'}
        >
          <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7z" />
          </svg>
          Deploy
        </button>
      </div>

      {/* Form body */}
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-2xl mx-auto p-6 space-y-6">
          <div className="rounded-xl border border-[#e9ebed] bg-white p-5 space-y-5">
            <div>
              <h2 className="text-base font-semibold text-[#16191f]">Configure your Harness agent</h2>
              <p className="text-sm text-[#5f6b7a] mt-1">
                A managed AgentCore Harness runs your agent without packaging code — pick a model,
                write instructions, and connect tools. Deploys via the additive harness path.
              </p>
            </div>

            <TextField
              id="harness-name"
              label="Name"
              value={name}
              onChange={(v) => setName(v)}
              required
              placeholder="agent_harness"
              helpText="Letters, numbers, and underscores only (sanitized on deploy)."
            />

            <FormSection title="Model">
              <SelectField
                id="harness-provider"
                label="Provider"
                value={provider}
                onChange={handleProviderChange}
                options={PROVIDER_OPTIONS.map((p) => ({ value: p.value, label: p.label }))}
                helpText={providerInfo?.description}
              />
              <SelectField
                id="harness-model"
                label="Model"
                value={modelId}
                onChange={setModelId}
                required
                options={availableModels.map((m) => ({ value: m.modelId, label: m.label }))}
                placeholder={availableModels.length === 0 ? 'No models for this provider' : undefined}
                helpText={
                  providerInfo?.requiresApiKey
                    ? `Requires ${providerInfo.envVar} configured on the runtime.`
                    : undefined
                }
              />
            </FormSection>

            <FormSection title="Instructions" description="System prompt that steers the agent.">
              <TextArea
                id="harness-instructions"
                label="System prompt"
                value={instructions}
                onChange={setInstructions}
                required
                rows={6}
                placeholder="You are a helpful assistant that…"
              />
            </FormSection>

            <FormSection title="Memory" description="Persist conversation context across turns.">
              <Toggle
                id="harness-memory"
                label="Enable AgentCore Memory"
                checked={memoryEnabled}
                onChange={setMemoryEnabled}
                description="Attaches a memory resource to the harness."
              />
            </FormSection>

            <FormSection
              title="Tools"
              description="Reuses the same built-in tools and SaaS connectors as the visual canvas."
            >
              <div className="space-y-3">
                <div>
                  <div className="text-xs font-semibold uppercase tracking-wide text-[#8d99a8] mb-2">Built-in tools</div>
                  <div className="grid grid-cols-2 gap-2">
                    {BUILTIN_TOOLS.map((tool) => {
                      const id = tool.toolId!;
                      const active = selectedTools.has(id);
                      return (
                        <button
                          key={id}
                          type="button"
                          aria-pressed={active}
                          onClick={() => toggleSet(setSelectedTools, id)}
                          className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-left text-xs transition-colors ${
                            active
                              ? 'border-[#0972d3] bg-[#0972d3]/8 text-[#0972d3]'
                              : 'border-[#e9ebed] bg-white text-[#16191f] hover:border-[#0972d3]/40'
                          }`}
                        >
                          <span className="text-base">{tool.icon}</span>
                          <span className="font-medium truncate">{tool.label}</span>
                        </button>
                      );
                    })}
                  </div>
                </div>

                <div>
                  <div className="text-xs font-semibold uppercase tracking-wide text-[#8d99a8] mb-2">Connectors</div>
                  <div className="grid grid-cols-2 gap-2">
                    {CONNECTOR_TOOLS.map((tool) => {
                      const id = tool.toolId!;
                      const active = selectedConnectors.has(id);
                      const cfg = connectorConfigsRef.current[id];
                      const configured = !!(cfg && (cfg.secretValue || cfg.secretArn));
                      return (
                        <button
                          key={id}
                          type="button"
                          aria-pressed={active}
                          onClick={() => {
                            // Toggle off if already active+configured-known; otherwise
                            // select it and open the modal to collect credentials.
                            if (active) {
                              setSelectedConnectors((prev) => {
                                const next = new Set(prev);
                                next.delete(id);
                                return next;
                              });
                              delete connectorConfigsRef.current[id];
                              setConnectorRev((r) => r + 1);
                            } else {
                              setSelectedConnectors((prev) => new Set(prev).add(id));
                              setConnectorModalId(id);
                            }
                          }}
                          className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-left text-xs transition-colors ${
                            active
                              ? configured
                                ? 'border-indigo-500 bg-indigo-50 text-indigo-700'
                                : 'border-amber-500 bg-amber-50 text-amber-700'
                              : 'border-[#e9ebed] bg-white text-[#16191f] hover:border-indigo-300'
                          }`}
                          title={active && !configured ? 'Click to add credentials' : undefined}
                        >
                          <span className="text-base">{tool.icon}</span>
                          <span className="font-medium truncate">{tool.label}</span>
                          {active && !configured && <span className="ml-auto text-[10px]">⚠ creds</span>}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            </FormSection>
          </div>
        </div>
      </div>

      {/* Deploy & Test — reuse the existing panel + status polling, harness mode */}
      <DeployPanel
        config={deployConfig}
        nodeId="harness"
        connectedTools={connectedTools}
        gatewayTools={gatewayTools}
        connectors={connectors}
        memoryConfig={memoryConfig}
        deploymentMode="harness"
        isVisible={showDeployPanel}
        onClose={() => setShowDeployPanel(false)}
      />

      {/* Connector credential modal — same component the canvas uses (Bug 193b). */}
      {connectorModalId && (
        <ConnectorConfigModal
          isOpen={true}
          initialConfig={{
            ...(connectorConfigsRef.current[connectorModalId] ?? {}),
            connectorId: connectorModalId.slice(CONNECTOR_TOOL_PREFIX.length) as ConnectorConfiguration['connectorId'],
            toolId: connectorModalId,
          }}
          onSave={(cfg) => {
            connectorConfigsRef.current[connectorModalId] = cfg;
            setConnectorRev((r) => r + 1);
            setConnectorModalId(null);
          }}
          onClose={() => {
            // Cancelling without credentials de-selects the connector so the user
            // can't deploy an unconfigured connector by accident.
            const cfg = connectorConfigsRef.current[connectorModalId];
            if (!cfg || (!cfg.secretValue && !cfg.secretArn)) {
              setSelectedConnectors((prev) => {
                const next = new Set(prev);
                next.delete(connectorModalId);
                return next;
              });
            }
            setConnectorModalId(null);
          }}
        />
      )}
    </div>
  );
}

export default HarnessAuthoring;
