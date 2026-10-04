/**
 * RuntimeConfiguration modal for configuring AgentCore Runtime components.
 * Strands-only with model provider selection and multi-agent pattern support.
 */

import { useState, useMemo, useEffect, useCallback } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, TextArea, SelectField, SliderField, FormSection, CheckboxField } from './FormFields';
import type {
  AgentDefinition,
  ModelConfiguration,
  MultiAgentPattern,
  RuntimeConfiguration,
  StrandsModelProvider,
} from '../../types/components';
import type { DeploymentType, PythonRuntime, AgentServerProtocol } from '../../types/workflow';
import {
  getModelsForProvider,
  estimateTokenCount,
  formatTokenCount,
  createDefaultRuntimeConfig,
  PROVIDER_OPTIONS,
} from '../../utils/runtimeConfig';
import { authFetch } from '../../auth/authFetch';
import { apiErrorFromResponse, getErrorMessage } from '../../services/api/client';
import type { PromptSelection } from './PromptLibraryModal';
import { stripStandaloneMcpModelFields } from '../../utils/runtimeRequestConfig';
import { usePlatformObservabilityPolicy } from '../../hooks/usePlatformObservabilityPolicy';
import { PlatformObservabilityPolicyNotice } from './PlatformObservabilityPolicyNotice';

// ============================================================================
// Default prompt
// ============================================================================

const DEFAULT_PROMPT = 'You are a helpful AI assistant powered by AWS Strands Agents. You have access to various tools and can help users accomplish their tasks efficiently.';

function normalizeMcpToolServerConfig(
  config: RuntimeConfiguration,
): RuntimeConfiguration {
  if (config.protocol !== 'MCP') return config;

  const normalized = stripStandaloneMcpModelFields(config);
  delete normalized.observability;
  return {
    ...normalized,
    protocol: 'MCP',
    enableOtel: false,
  };
}

function createModalRuntimeConfig(
  initialConfig?: Partial<RuntimeConfiguration>,
): RuntimeConfiguration {
  return normalizeMcpToolServerConfig({
    ...createDefaultRuntimeConfig(),
    ...initialConfig,
  });
}

// ============================================================================
// Props Interface
// ============================================================================

export interface RuntimeConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: RuntimeConfiguration) => void;
  initialConfig?: Partial<RuntimeConfiguration>;
  onOpenPromptLibrary?: (onSelect: (selection: PromptSelection) => void) => void;
}

// ============================================================================
// RuntimeConfigurationModal Component
// ============================================================================

export function RuntimeConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
  onOpenPromptLibrary,
}: RuntimeConfigurationModalProps) {
  const [config, setConfig] = useState<RuntimeConfiguration>(() =>
    createModalRuntimeConfig(initialConfig),
  );
  const [providerApiKey, setProviderApiKey] = useState('');
  const [savingProviderCredential, setSavingProviderCredential] = useState(false);
  const [providerCredentialError, setProviderCredentialError] = useState<string | null>(null);
  const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? '') as string;

  // Reset config when modal opens with new initial config (adjust state during render pattern)
  const [lastInitial, setLastInitial] = useState<typeof initialConfig | symbol>(Symbol('unset'));
  if (isOpen && initialConfig !== lastInitial) {
    setLastInitial(initialConfig);
    setConfig(createModalRuntimeConfig(initialConfig));
  }

  // Platform-managed OTEL defaults — when on, the per-runtime "Enable OTEL"
  // checkbox is meaningless (every agent emits traces regardless). Unknown
  // policy state must not masquerade as "disabled".
  const {
    state: platformPolicy,
    retry: retryPlatformPolicy,
  } = usePlatformObservabilityPolicy(isOpen, apiBaseUrl);
  const platformOtelEnabled =
    platformPolicy.status === 'ready' && platformPolicy.defaults.enabled;

  useEffect(() => {
    if (!isOpen) return;
    setProviderApiKey('');
    setProviderCredentialError(null);
  }, [isOpen]);

  const isMcpToolServer = config.protocol === 'MCP';
  const defaultModel = useMemo(
    () => createDefaultRuntimeConfig().model as ModelConfiguration,
    [],
  );
  const model = config.model || defaultModel;
  const systemPrompt = config.systemPrompt || '';
  const multiAgentPattern = config.multiAgentPattern || 'none';
  const provider = config.modelProvider || model.provider || 'bedrock';
  const providerInfo = PROVIDER_OPTIONS.find((p) => p.value === provider);
  const availableModels = useMemo(() => getModelsForProvider(provider), [provider]);
  const tokenCount = useMemo(() => estimateTokenCount(systemPrompt), [systemPrompt]);

  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];
    if (!config.name.trim()) errors.push({ field: 'name', message: 'Name is required' });
    if (!isMcpToolServer) {
      if (!systemPrompt.trim()) {
        errors.push({ field: 'systemPrompt', message: 'System prompt is required' });
      }
      if (!model.modelId) {
        errors.push({ field: 'model', message: 'Model selection is required' });
      }
      if (providerInfo?.requiresApiKey && !config.providerApiKeyRef?.trim()) {
        errors.push({
          field: 'providerApiKeyRef',
          message: `Store a ${providerInfo.label} API key before saving this runtime`,
        });
      }
    }
    return errors;
  }, [config.name, config.providerApiKeyRef, isMcpToolServer, model.modelId, providerInfo, systemPrompt]);

  const updateConfig = useCallback(function updateConfig<K extends keyof RuntimeConfiguration>(
    key: K,
    value: RuntimeConfiguration[K],
  ) {
    setConfig((prev) => ({ ...prev, [key]: value }));
  }, []);

  const updateModel = useCallback(function updateModel<K extends keyof ModelConfiguration>(
    key: K,
    value: ModelConfiguration[K],
  ) {
    setConfig((prev) => ({
      ...prev,
      model: { ...(prev.model || defaultModel), [key]: value },
    }));
  }, [defaultModel]);

  const handleProviderChange = useCallback((newProvider: StrandsModelProvider) => {
    const models = getModelsForProvider(newProvider);
    setConfig((prev) => ({
      ...prev,
      modelProvider: newProvider,
      model: models.length > 0
        ? { ...(prev.model || defaultModel), provider: newProvider, modelId: models[0].modelId }
        : { ...(prev.model || defaultModel), provider: newProvider, modelId: '' },
      providerApiKeyRef: undefined,
    }));
    setProviderApiKey('');
    setProviderCredentialError(null);
  }, [defaultModel]);

  const handleStoreProviderCredential = useCallback(async () => {
    if (!providerInfo?.requiresApiKey || !providerApiKey.trim()) {
      setProviderCredentialError('Enter an API key before storing it.');
      return;
    }

    setSavingProviderCredential(true);
    setProviderCredentialError(null);
    try {
      const response = await authFetch(`${apiBaseUrl}/api/provider-credentials`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider,
          api_key: providerApiKey,
        }),
      });
      if (!response.ok) {
        throw await apiErrorFromResponse(response);
      }
      const data = (await response.json()) as { secret_arn?: unknown };
      if (typeof data.secret_arn !== 'string' || !data.secret_arn) {
        throw new Error('The credential service returned no secret ARN.');
      }
      updateConfig('providerApiKeyRef', data.secret_arn);
      setProviderApiKey('');
    } catch (error) {
      setProviderCredentialError(getErrorMessage(error));
    } finally {
      setSavingProviderCredential(false);
    }
  }, [apiBaseUrl, provider, providerApiKey, providerInfo?.requiresApiKey, updateConfig]);

  const handlePatternChange = useCallback((pattern: MultiAgentPattern) => {
    setConfig((prev) => ({
      ...prev,
      multiAgentPattern: pattern,
      multiAgentConfig: pattern === 'none' ? undefined : (prev.multiAgentConfig || { agents: [] }),
    }));
  }, []);

  const handleAddAgent = useCallback(() => {
    setConfig((prev) => {
      const existing = prev.multiAgentConfig || { agents: [] };
      const idx = existing.agents.length + 1;
      const newAgent: AgentDefinition = {
        agentId: `agent-${idx}`,
        name: `Agent ${idx}`,
        systemPrompt: `You are Agent ${idx}.`,
        modelProvider: prev.modelProvider || 'bedrock',
        modelId: prev.model?.modelId || defaultModel.modelId,
        tools: [],
      };
      return {
        ...prev,
        multiAgentConfig: { ...existing, agents: [...existing.agents, newAgent] },
      };
    });
  }, [defaultModel.modelId]);

  const handleRemoveAgent = useCallback((idx: number) => {
    setConfig((prev) => {
      const existing = prev.multiAgentConfig || { agents: [] };
      return {
        ...prev,
        multiAgentConfig: { ...existing, agents: existing.agents.filter((_, i) => i !== idx) },
      };
    });
  }, []);

  const handleUpdateAgent = useCallback((idx: number, field: keyof AgentDefinition, value: string | string[]) => {
    setConfig((prev) => {
      const existing = prev.multiAgentConfig || { agents: [] };
      const agents = [...existing.agents];
      agents[idx] = { ...agents[idx], [field]: value };
      return { ...prev, multiAgentConfig: { ...existing, agents } };
    });
  }, []);

  const handleSave = () => {
    if (platformPolicy.status !== 'ready') return;
    onSave(config);
    onClose();
  };

  const getFieldError = useCallback(
    (field: string) => validationErrors.find((e) => e.field === field)?.message,
    [validationErrors],
  );

  const multiAgentAgents = useMemo(
    () => config.multiAgentConfig?.agents ?? [],
    [config.multiAgentConfig?.agents],
  );

  const tabs = useMemo(() => {
    const allTabs = [
    {
      id: 'provider',
      label: 'Provider',
      hasError: validationErrors.some((e) => e.field === 'providerApiKeyRef'),
      content: (
        <div className="space-y-4">
          <FormSection title="Model Provider" description="Choose where your model runs. Bedrock is default (AWS-native, no API key).">
            <div className="grid grid-cols-1 gap-2 max-h-[400px] overflow-y-auto pr-2">
              {PROVIDER_OPTIONS.map((p) => (
                <label
                  key={p.value}
                  className={`
                    flex items-start gap-3 p-3 rounded-lg border-2 cursor-pointer transition-all
                    ${provider === p.value
                      ? 'border-blue-500 bg-blue-50'
                      : 'border-gray-200 hover:border-gray-300 hover:bg-gray-50'}
                  `}
                >
                  <input
                    type="radio"
                    name="provider"
                    value={p.value}
                    checked={provider === p.value}
                    onChange={() => handleProviderChange(p.value)}
                    className="mt-1"
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-gray-900">{p.label}</span>
                      {p.requiresApiKey && (
                        <span className="text-xs bg-yellow-100 text-yellow-800 px-1.5 py-0.5 rounded">API Key</span>
                      )}
                    </div>
                    <div className="text-sm text-gray-600 mt-0.5">{p.description}</div>
                  </div>
                </label>
              ))}
            </div>
          </FormSection>

          {providerInfo?.requiresApiKey && (
            <FormSection
              title="API Key Configuration"
              description={`Store your ${providerInfo.envVar} securely. The plaintext key is accepted once and never saved in the canvas.`}
            >
              <TextField
                id="providerApiKey"
                label={`${providerInfo.label} API key`}
                type="password"
                value={providerApiKey}
                onChange={setProviderApiKey}
                placeholder="Paste the API key"
                maxLength={8192}
                helpText="The key is sent over the authenticated API directly to AWS Secrets Manager."
              />
              <button
                type="button"
                onClick={() => void handleStoreProviderCredential()}
                disabled={savingProviderCredential || !providerApiKey.trim()}
                className="rounded bg-blue-600 px-3 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-blue-300"
              >
                {savingProviderCredential ? 'Storing…' : 'Store API key'}
              </button>
              {providerCredentialError && (
                <p className="text-sm text-red-600" role="alert">
                  {providerCredentialError}
                </p>
              )}
              {config.providerApiKeyRef && !providerCredentialError && (
                <p className="text-sm text-green-700" role="status">
                  Credential stored. Only its ARN is saved with this runtime.
                </p>
              )}
              <TextField
                id="providerApiKeyRef"
                label="Stored credential ARN"
                value={config.providerApiKeyRef || ''}
                onChange={(value) => updateConfig('providerApiKeyRef', value)}
                placeholder="arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-provider/..."
                error={getFieldError('providerApiKeyRef')}
                helpText="At deploy time, the platform verifies this source belongs to you and this stack, then copies it into a deployment-bound secret in the target account. The runtime receives only that copied ARN and resolves it when needed; the plaintext key is never placed in runtime environment variables or Step Functions history."
              />
            </FormSection>
          )}
        </div>
      ),
    },
    {
      id: 'general',
      label: 'General',
      hasError: validationErrors.some((e) => ['name', 'entrypoint'].includes(e.field)),
      content: (
        <div className="space-y-6">
          <FormSection title="Basic Information">
            <TextField
              id="name"
              label="Runtime Name"
              value={config.name}
              onChange={(value) => updateConfig('name', value)}
              placeholder="My Agent Runtime"
              required
              error={getFieldError('name')}
            />
            <TextField
              id="entrypoint"
              label="Entrypoint"
              value={config.entrypoint}
              onChange={(value) => updateConfig('entrypoint', value)}
              placeholder="agent.py"
              helpText="The Python file containing your agent code"
            />
            {isMcpToolServer && (
              <div
                className="rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900"
                role="status"
              >
                <div className="font-medium">Model-free MCP tool server</div>
                <p className="mt-1 text-xs">
                  This runtime exposes typed tools directly over MCP. It does not
                  use a model provider, model parameters, system prompt, or
                  multi-agent orchestration.
                </p>
              </div>
            )}
          </FormSection>

          <FormSection title="Deployment Settings">
            <div className="grid grid-cols-2 gap-4">
              <SelectField
                id="deploymentType"
                label="Deployment Type"
                value={config.deploymentType}
                onChange={(value) => updateConfig('deploymentType', value as DeploymentType)}
                options={[
                  { value: 'direct_code_deploy', label: 'Direct Code Deploy' },
                  { value: 'container', label: 'Container' },
                ]}
              />
              <SelectField
                id="pythonRuntime"
                label="Python Runtime"
                value={config.pythonRuntime}
                onChange={(value) => updateConfig('pythonRuntime', value as PythonRuntime)}
                options={[
                  { value: 'PYTHON_3_10', label: 'Python 3.10' },
                  { value: 'PYTHON_3_11', label: 'Python 3.11' },
                  { value: 'PYTHON_3_12', label: 'Python 3.12' },
                  { value: 'PYTHON_3_13', label: 'Python 3.13' },
                ]}
              />
            </div>
            <SelectField
              id="protocol"
              label="Server Protocol"
              value={config.protocol}
              onChange={(value) =>
                updateConfig('protocol', value as AgentServerProtocol)
              }
              options={
                isMcpToolServer
                  ? [{ value: 'MCP', label: 'MCP - Model Context Protocol' }]
                  : [
                      { value: 'HTTP', label: 'HTTP - Standard REST API' },
                      { value: 'A2A', label: 'A2A - Agent-to-Agent' },
                    ]
              }
              disabled={isMcpToolServer}
              helpText={
                isMcpToolServer
                  ? 'The selected MCP tool-server template fixes this protocol.'
                  : 'MCP is available through the dedicated MCP runtime templates, which generate a real MCP server.'
              }
            />
          </FormSection>
        </div>
      ),
    },
    {
      id: 'prompt',
      label: 'System Prompt',
      hasError: validationErrors.some((e) => e.field === 'systemPrompt'),
      content: (
        <div className="space-y-6">
          <FormSection title="System Prompt" description="Define the behavior and personality of your agent">
            <TextArea
              id="systemPrompt"
              label="System Prompt"
              value={systemPrompt}
              onChange={(value) => updateConfig('systemPrompt', value)}
              placeholder="You are a helpful AI assistant..."
              rows={10}
              required
              error={getFieldError('systemPrompt')}
            />
            <div className="flex justify-between text-sm text-gray-500">
              <span>Estimated tokens: {formatTokenCount(tokenCount)}</span>
              <span className={tokenCount > 4000 ? 'text-yellow-600' : ''}>
                {tokenCount > 4000 && 'Long prompts may increase latency'}
              </span>
            </div>
          </FormSection>

          <FormSection title="Quick Templates">
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => updateConfig('systemPrompt', DEFAULT_PROMPT)}
                className="p-2 text-sm text-left border rounded hover:bg-gray-50"
              >
                Strands Default
              </button>
              <button
                type="button"
                onClick={() => updateConfig('systemPrompt', 'You are a helpful AI assistant. Answer questions accurately and concisely. Always be polite and professional.')}
                className="p-2 text-sm text-left border rounded hover:bg-gray-50"
              >
                General Assistant
              </button>
              <button
                type="button"
                onClick={() => updateConfig('systemPrompt', 'You are an expert software engineer. Help users write, debug, and explain code. Provide working examples with clear explanations.')}
                className="p-2 text-sm text-left border rounded hover:bg-gray-50"
              >
                Code Assistant
              </button>
              <button
                type="button"
                onClick={() => updateConfig('systemPrompt', 'You are a data analyst expert. Help users analyze data, create visualizations, and derive actionable insights from their datasets.')}
                className="p-2 text-sm text-left border rounded hover:bg-gray-50"
              >
                Data Analyst
              </button>
            </div>
            {onOpenPromptLibrary && (
              <button
                type="button"
                onClick={() =>
                  onOpenPromptLibrary((selection) =>
                    updateConfig('systemPrompt', selection.body),
                  )
                }
                className="mt-3 w-full rounded border border-blue-300 px-3 py-2 text-sm font-medium text-blue-600 hover:bg-blue-50"
              >
                Use from prompt library
              </button>
            )}
          </FormSection>
        </div>
      ),
    },
    {
      id: 'model',
      label: 'Model',
      hasError: validationErrors.some((e) => e.field === 'model'),
      content: (
        <div className="space-y-6">
          <FormSection title="Model Selection" description={`Models available from ${providerInfo?.label || provider}`}>
            <SelectField
              id="model"
              label="Model"
              value={model.modelId}
              onChange={(modelId) => {
                const model = availableModels.find((m) => m.modelId === modelId);
                if (model) {
                  updateModel('provider', model.provider);
                  updateModel('modelId', model.modelId);
                }
              }}
              options={availableModels.map((m) => ({ value: m.modelId, label: m.label }))}
              required
              error={getFieldError('model')}
            />
            {availableModels.length === 0 && (
              <div className="text-sm text-yellow-600 bg-yellow-50 p-3 rounded">
                No models available for this provider. Select a different provider.
              </div>
            )}
          </FormSection>

          <FormSection title="Model Parameters">
            <SliderField
              id="temperature"
              label="Temperature"
              value={model.temperature}
              onChange={(value) => updateModel('temperature', value)}
              min={0}
              max={2}
              step={0.1}
              helpText="Higher = more creative, Lower = more deterministic"
            />
            <SliderField
              id="topP"
              label="Top P (Nucleus Sampling)"
              value={model.topP}
              onChange={(value) => updateModel('topP', value)}
              min={0}
              max={1}
              step={0.05}
              helpText="Controls diversity of token selection"
            />
          </FormSection>
        </div>
      ),
    },
    {
      id: 'multiagent',
      label: 'Multi-Agent',
      hasError: false,
      content: (
        <div className="space-y-6">
          <FormSection title="Multi-Agent Pattern" description="Configure multiple sub-agents orchestrated by Strands">
            <div className="grid grid-cols-2 gap-2">
              {([
                { value: 'none' as MultiAgentPattern, label: 'Single Agent', desc: 'One agent handles all tasks' },
                { value: 'graph' as MultiAgentPattern, label: 'Graph', desc: 'Nodes + edges with conditional routing' },
                { value: 'swarm' as MultiAgentPattern, label: 'Swarm', desc: 'Autonomous agent handoffs' },
                { value: 'workflow' as MultiAgentPattern, label: 'Workflow', desc: 'DAG with parallel execution' },
              ]).map((p) => (
                <label
                  key={p.value}
                  className={`
                    flex items-start gap-2 p-3 rounded-lg border-2 cursor-pointer transition-all
                    ${multiAgentPattern === p.value
                      ? 'border-blue-500 bg-blue-50'
                      : 'border-gray-200 hover:border-gray-300 hover:bg-gray-50'}
                  `}
                >
                  <input
                    type="radio"
                    name="multiAgentPattern"
                    value={p.value}
                    checked={multiAgentPattern === p.value}
                    onChange={() => handlePatternChange(p.value)}
                    className="mt-1"
                  />
                  <div>
                    <div className="font-medium text-gray-900 text-sm">{p.label}</div>
                    <div className="text-xs text-gray-600">{p.desc}</div>
                  </div>
                </label>
              ))}
            </div>
          </FormSection>

          {multiAgentPattern !== 'none' && (
            <FormSection title="Sub-Agents" description="Define the agents in your multi-agent system">
              <div className="space-y-3">
                {multiAgentAgents.map((agent, idx) => (
                  <div key={agent.agentId} className="border rounded-lg p-3 space-y-2">
                    <div className="flex justify-between items-center">
                      <span className="font-medium text-sm text-gray-700">Agent {idx + 1}</span>
                      <button
                        type="button"
                        onClick={() => handleRemoveAgent(idx)}
                        className="text-red-500 text-xs hover:text-red-700"
                      >
                        Remove
                      </button>
                    </div>
                    <TextField
                      id={`agent-name-${idx}`}
                      label="Name"
                      value={agent.name}
                      onChange={(v) => handleUpdateAgent(idx, 'name', v)}
                      placeholder="Agent name"
                    />
                    <TextArea
                      id={`agent-prompt-${idx}`}
                      label="System Prompt"
                      value={agent.systemPrompt}
                      onChange={(v) => handleUpdateAgent(idx, 'systemPrompt', v)}
                      rows={3}
                      placeholder="Agent system prompt..."
                    />
                    <SelectField
                      id={`agent-model-${idx}`}
                      label="Model"
                      value={agent.modelId}
                      onChange={(v) => handleUpdateAgent(idx, 'modelId', v)}
                      options={availableModels.map((m) => ({ value: m.modelId, label: m.label }))}
                    />
                  </div>
                ))}
                <button
                  type="button"
                  onClick={handleAddAgent}
                  className="w-full p-2 border-2 border-dashed border-gray-300 rounded-lg text-sm text-gray-600 hover:border-blue-400 hover:text-blue-600 transition-colors"
                >
                  + Add Agent
                </button>
              </div>
            </FormSection>
          )}

          {multiAgentPattern === 'graph' && multiAgentAgents.length >= 2 && (
            <FormSection title="Entry Point" description="Which agent starts the graph?">
              <SelectField
                id="entryPoint"
                label="Entry Point Agent"
                value={config.multiAgentConfig?.entryPoint || multiAgentAgents[0]?.agentId || ''}
                onChange={(v) => {
                  setConfig((prev) => ({
                    ...prev,
                    multiAgentConfig: { ...(prev.multiAgentConfig || { agents: [] }), entryPoint: v },
                  }));
                }}
                options={multiAgentAgents.map((a) => ({ value: a.agentId, label: a.name }))}
              />
            </FormSection>
          )}
        </div>
      ),
    },
    {
      id: 'advanced',
      label: 'Advanced',
      hasError: false,
      content: (
        <div className="space-y-6">
          <FormSection title="Runtime Limits">
            <SliderField
              id="idleTimeout"
              label="Idle Timeout (seconds)"
              value={config.idleTimeout}
              onChange={(value) => updateConfig('idleTimeout', value)}
              min={60}
              max={3600}
              step={60}
              helpText="Time before idle runtime is stopped"
            />
            <SliderField
              id="maxLifetime"
              label="Max Lifetime (seconds)"
              value={config.maxLifetime}
              onChange={(value) => updateConfig('maxLifetime', value)}
              min={60}
              max={28800}
              step={300}
              helpText="Maximum runtime lifetime"
            />
          </FormSection>

          <FormSection title="Features">
            {platformPolicy.status !== 'ready' ? null : isMcpToolServer ? (
              <div className="rounded-md border border-gray-200 bg-gray-50 p-3 text-sm text-gray-700">
                <div className="font-medium">
                  Generic agent telemetry is unavailable
                </div>
                <p className="mt-1 text-xs">
                  The standalone MCP server does not use the Strands agent
                  telemetry integration.
                  {platformOtelEnabled
                    ? ' Platform-managed telemetry is enabled, so deployment will be refused before provisioning until an administrator disables that default.'
                    : ''}
                </p>
              </div>
            ) : platformOtelEnabled ? (
              <div className="rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900">
                <div className="font-medium">OpenTelemetry: platform-managed</div>
                <p className="mt-1 text-xs">
                  Every agent on this platform automatically emits traces to the admin-configured backend.
                </p>
              </div>
            ) : (
              <CheckboxField
                id="enableOtel"
                label="Enable OpenTelemetry"
                checked={config.enableOtel}
                onChange={(checked) => updateConfig('enableOtel', checked)}
                helpText="Distributed tracing and observability"
              />
            )}
          </FormSection>
        </div>
      ),
    },
    ];

    if (isMcpToolServer) {
      return allTabs.filter((tab) =>
        ['general', 'advanced'].includes(tab.id),
      );
    }
    return allTabs;
  }, [config, validationErrors, availableModels, tokenCount, provider, providerInfo, model.modelId, model.temperature, model.topP, systemPrompt, multiAgentPattern, multiAgentAgents, platformOtelEnabled, platformPolicy.status, providerApiKey, savingProviderCredential, providerCredentialError, onOpenPromptLibrary, updateConfig, updateModel, handleProviderChange, handleStoreProviderCredential, handlePatternChange, handleAddAgent, handleRemoveAgent, handleUpdateAgent, getFieldError, isMcpToolServer]);

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title={`Configure Runtime: ${config.name || 'New Runtime'}`}
      tabs={tabs}
      validationErrors={validationErrors}
      isSaveDisabled={platformPolicy.status !== 'ready'}
      notice={
        platformPolicy.status !== 'ready' ? (
          <PlatformObservabilityPolicyNotice
            state={platformPolicy}
            onRetry={retryPlatformPolicy}
          />
        ) : undefined
      }
    />
  );
}

export default RuntimeConfigurationModal;
