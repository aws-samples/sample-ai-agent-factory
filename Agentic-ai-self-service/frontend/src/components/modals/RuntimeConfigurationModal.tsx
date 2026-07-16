/**
 * RuntimeConfiguration modal for configuring AgentCore Runtime components.
 * Strands-only with model provider selection and multi-agent pattern support.
 */

import { useState, useMemo, useEffect } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, TextArea, SelectField, SliderField, FormSection, CheckboxField } from './FormFields';
import type { RuntimeConfiguration, StrandsModelProvider, MultiAgentPattern, AgentDefinition } from '../../types/components';
import type { DeploymentType, PythonRuntime, AgentServerProtocol } from '../../types/workflow';
import {
  getModelsForProvider,
  estimateTokenCount,
  formatTokenCount,
  createDefaultRuntimeConfig,
  PROVIDER_OPTIONS,
} from '../../utils/runtimeConfig';

// ============================================================================
// Default prompt
// ============================================================================

const DEFAULT_PROMPT = 'You are a helpful AI assistant powered by AWS Strands Agents. You have access to various tools and can help users accomplish their tasks efficiently.';

// ============================================================================
// Props Interface
// ============================================================================

export interface RuntimeConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: RuntimeConfiguration) => void;
  initialConfig?: Partial<RuntimeConfiguration>;
}

// ============================================================================
// RuntimeConfigurationModal Component
// ============================================================================

export function RuntimeConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: RuntimeConfigurationModalProps) {
  const [config, setConfig] = useState<RuntimeConfiguration>(() => ({
    ...createDefaultRuntimeConfig(),
    ...initialConfig,
  }));

  // Reset config when modal opens with new initial config (adjust state during render pattern)
  const [lastInitial, setLastInitial] = useState<typeof initialConfig | symbol>(Symbol('unset'));
  if (isOpen && initialConfig !== lastInitial) {
    setLastInitial(initialConfig);
    setConfig({ ...createDefaultRuntimeConfig(), ...initialConfig });
  }

  // Platform-managed OTEL defaults — when on, the per-runtime "Enable OTEL"
  // checkbox is meaningless (every agent emits traces regardless).
  const [platformOtelEnabled, setPlatformOtelEnabled] = useState(false);
  useEffect(() => {
    if (!isOpen) return;
    const apiBase = (import.meta.env.VITE_API_BASE_URL ?? '') as string;
    fetch(`${apiBase}/api/observability/platform-defaults`)
      .then((r) => (r.ok ? r.json() : { enabled: false }))
      .then((data: { enabled: boolean }) => setPlatformOtelEnabled(data.enabled))
      .catch(() => setPlatformOtelEnabled(false));
  }, [isOpen]);

  const provider = config.modelProvider || 'bedrock';
  const providerInfo = PROVIDER_OPTIONS.find((p) => p.value === provider);
  const availableModels = useMemo(() => getModelsForProvider(provider), [provider]);
  const tokenCount = useMemo(() => estimateTokenCount(config.systemPrompt), [config.systemPrompt]);

  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];
    if (!config.name.trim()) errors.push({ field: 'name', message: 'Name is required' });
    if (!config.systemPrompt.trim()) errors.push({ field: 'systemPrompt', message: 'System prompt is required' });
    if (!config.model.modelId) errors.push({ field: 'model', message: 'Model selection is required' });
    return errors;
  }, [config]);

  const updateConfig = <K extends keyof RuntimeConfiguration>(key: K, value: RuntimeConfiguration[K]) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  };

  const updateModel = <K extends keyof RuntimeConfiguration['model']>(key: K, value: RuntimeConfiguration['model'][K]) => {
    setConfig((prev) => ({ ...prev, model: { ...prev.model, [key]: value } }));
  };

  const handleProviderChange = (newProvider: StrandsModelProvider) => {
    const models = getModelsForProvider(newProvider);
    setConfig((prev) => ({
      ...prev,
      modelProvider: newProvider,
      model: models.length > 0
        ? { ...prev.model, provider: newProvider, modelId: models[0].modelId }
        : { ...prev.model, provider: newProvider, modelId: '' },
      providerApiKeyRef: undefined,
    }));
  };

  const handlePatternChange = (pattern: MultiAgentPattern) => {
    setConfig((prev) => ({
      ...prev,
      multiAgentPattern: pattern,
      multiAgentConfig: pattern === 'none' ? undefined : (prev.multiAgentConfig || { agents: [] }),
    }));
  };

  const handleAddAgent = () => {
    setConfig((prev) => {
      const existing = prev.multiAgentConfig || { agents: [] };
      const idx = existing.agents.length + 1;
      const newAgent: AgentDefinition = {
        agentId: `agent-${idx}`,
        name: `Agent ${idx}`,
        systemPrompt: `You are Agent ${idx}.`,
        modelProvider: prev.modelProvider || 'bedrock',
        modelId: prev.model.modelId,
        tools: [],
      };
      return {
        ...prev,
        multiAgentConfig: { ...existing, agents: [...existing.agents, newAgent] },
      };
    });
  };

  const handleRemoveAgent = (idx: number) => {
    setConfig((prev) => {
      const existing = prev.multiAgentConfig || { agents: [] };
      return {
        ...prev,
        multiAgentConfig: { ...existing, agents: existing.agents.filter((_, i) => i !== idx) },
      };
    });
  };

  const handleUpdateAgent = (idx: number, field: keyof AgentDefinition, value: string | string[]) => {
    setConfig((prev) => {
      const existing = prev.multiAgentConfig || { agents: [] };
      const agents = [...existing.agents];
      agents[idx] = { ...agents[idx], [field]: value };
      return { ...prev, multiAgentConfig: { ...existing, agents } };
    });
  };

  const handleSave = () => {
    onSave(config);
    onClose();
  };

  const getFieldError = (field: string) => validationErrors.find((e) => e.field === field)?.message;

  const multiAgentAgents = config.multiAgentConfig?.agents || [];

  const tabs = useMemo(() => [
    {
      id: 'provider',
      label: 'Provider',
      hasError: false,
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
                    <div className="text-sm text-gray-500 mt-0.5">{p.description}</div>
                  </div>
                </label>
              ))}
            </div>
          </FormSection>

          {providerInfo?.requiresApiKey && (
            <FormSection title="API Key Configuration" description={`Store your ${providerInfo.envVar} in AWS Secrets Manager and provide the ARN.`}>
              <TextField
                id="providerApiKeyRef"
                label="Secrets Manager ARN"
                value={config.providerApiKeyRef || ''}
                onChange={(value) => updateConfig('providerApiKeyRef', value)}
                placeholder="arn:aws:secretsmanager:us-east-1:123456789:secret:my-api-key"
                helpText={`Will be injected as ${providerInfo.envVar} environment variable`}
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
              onChange={(value) => updateConfig('protocol', value as AgentServerProtocol)}
              options={[
                { value: 'HTTP', label: 'HTTP - Standard REST API' },
                { value: 'MCP', label: 'MCP - Model Context Protocol' },
                { value: 'A2A', label: 'A2A - Agent-to-Agent' },
              ]}
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
              value={config.systemPrompt}
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
              value={config.model.modelId}
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
              value={config.model.temperature}
              onChange={(value) => updateModel('temperature', value)}
              min={0}
              max={2}
              step={0.1}
              helpText="Higher = more creative, Lower = more deterministic"
            />
            <SliderField
              id="topP"
              label="Top P (Nucleus Sampling)"
              value={config.model.topP}
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
                    ${config.multiAgentPattern === p.value
                      ? 'border-blue-500 bg-blue-50'
                      : 'border-gray-200 hover:border-gray-300 hover:bg-gray-50'}
                  `}
                >
                  <input
                    type="radio"
                    name="multiAgentPattern"
                    value={p.value}
                    checked={config.multiAgentPattern === p.value}
                    onChange={() => handlePatternChange(p.value)}
                    className="mt-1"
                  />
                  <div>
                    <div className="font-medium text-gray-900 text-sm">{p.label}</div>
                    <div className="text-xs text-gray-500">{p.desc}</div>
                  </div>
                </label>
              ))}
            </div>
          </FormSection>

          {config.multiAgentPattern !== 'none' && (
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

          {config.multiAgentPattern === 'graph' && multiAgentAgents.length >= 2 && (
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
            {platformOtelEnabled ? (
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
  ], [config, validationErrors, availableModels, tokenCount, provider, providerInfo, multiAgentAgents, platformOtelEnabled, updateConfig, updateModel, handleProviderChange, handlePatternChange, handleAddAgent, handleRemoveAgent, handleUpdateAgent, getFieldError]);

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title={`Configure Runtime: ${config.name || 'New Runtime'}`}
      tabs={tabs}
      validationErrors={validationErrors}
    />
  );
}

export default RuntimeConfigurationModal;
