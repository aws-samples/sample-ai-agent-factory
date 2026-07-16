/**
 * A2AConfiguration modal for configuring Agent-to-Agent (A2A) communication.
 * Requirements: Phase 3 Gap 3A
 */

import { useState, useCallback, useMemo } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, FormSection } from './FormFields';
import type { A2AConfiguration } from '../../types/components';

// ============================================================================
// Props Interface
// ============================================================================

export interface A2AConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: A2AConfiguration) => void;
  initialConfig?: Partial<A2AConfiguration>;
}

// ============================================================================
// Default Configuration
// ============================================================================

const DEFAULT_CONFIG: A2AConfiguration = {
  name: 'agent_a2a',
  enabled: true,
  pattern: 'peer_to_peer',
  agentEndpoints: [],
  timeoutSeconds: 30,
  maxRetries: 3,
  enableParallelExecution: false,
  enableMessageRouting: false,
  routingStrategy: 'capability_based',
  shareContext: true,
  contextWindowSize: 4096,
  capabilities: [],
  advertisedDescription: '',
  peerAllowlist: [],
};

// ============================================================================
// A2AConfigurationModal Component
// ============================================================================

export function A2AConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: A2AConfigurationModalProps) {
  const [config, setConfig] = useState<A2AConfiguration>(() => ({
    ...DEFAULT_CONFIG,
    ...initialConfig,
  }));

  // Reset config when modal opens with new initial config (adjust state during render pattern)
  const [lastInitial, setLastInitial] = useState<typeof initialConfig | symbol>(Symbol('unset'));
  if (isOpen && initialConfig !== lastInitial) {
    setLastInitial(initialConfig);
    setConfig({ ...DEFAULT_CONFIG, ...initialConfig });
  }

  // Validation
  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];

    if (!config.name.trim()) {
      errors.push({ field: 'name', message: 'Name is required' });
    }

    // Validate peer allowlist URLs
    if (config.peerAllowlist && config.peerAllowlist.length > 0) {
      for (const url of config.peerAllowlist) {
        if (!url.startsWith('https://')) {
          errors.push({ field: 'peerAllowlist', message: 'All peer URLs must use https://' });
          break;
        }
      }
    }

    return errors;
  }, [config]);

  // Update handlers
  const updateConfig = useCallback(<K extends keyof A2AConfiguration>(
    key: K,
    value: A2AConfiguration[K]
  ) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  }, []);

  // Handle save
  const handleSave = useCallback(() => {
    // `config` is already a complete A2AConfiguration (DEFAULT_CONFIG merged with
    // initialConfig + edits), so emit it whole — matches the other config modals
    // whose onSave passes a full ComponentConfiguration to handleSaveConfig.
    onSave(config);
    onClose();
  }, [config, onSave, onClose]);

  // Capability management
  const [newCapability, setNewCapability] = useState('');

  const handleAddCapability = useCallback(() => {
    if (newCapability.trim() && !config.capabilities?.includes(newCapability.trim())) {
      updateConfig('capabilities', [...(config.capabilities || []), newCapability.trim()]);
      setNewCapability('');
    }
  }, [newCapability, config.capabilities, updateConfig]);

  const handleRemoveCapability = useCallback((capability: string) => {
    updateConfig('capabilities', (config.capabilities || []).filter(c => c !== capability));
  }, [config.capabilities, updateConfig]);

  // Peer allowlist management
  const [newPeerUrl, setNewPeerUrl] = useState('');

  const handleAddPeer = useCallback(() => {
    if (newPeerUrl.trim() && !config.peerAllowlist?.includes(newPeerUrl.trim())) {
      updateConfig('peerAllowlist', [...(config.peerAllowlist || []), newPeerUrl.trim()]);
      setNewPeerUrl('');
    }
  }, [newPeerUrl, config.peerAllowlist, updateConfig]);

  const handleRemovePeer = useCallback((url: string) => {
    updateConfig('peerAllowlist', (config.peerAllowlist || []).filter(p => p !== url));
  }, [config.peerAllowlist, updateConfig]);

  // Build tabs
  const tabs = useMemo(() => {
    const getFieldError = (field: string) =>
      validationErrors.find((e) => e.field === field)?.message;

    return [
    {
      id: 'general',
      label: 'General',
      hasError: validationErrors.some((e) => e.field === 'name'),
      content: (
        <div className="space-y-6">
          <FormSection title="Basic Configuration">
            <TextField
              id="name"
              label="Name"
              value={config.name}
              onChange={(value) => updateConfig('name', value)}
              placeholder="Enter A2A configuration name"
              required
              error={getFieldError('name')}
            />

            <div className="space-y-2">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={config.enabled}
                  onChange={(e) => updateConfig('enabled', e.target.checked)}
                  className="text-console-blue"
                />
                <span className="text-sm font-medium text-gray-700">Enable A2A Communication</span>
              </label>
              <p className="text-xs text-gray-500 ml-6">
                Allow this agent to communicate with other agents via the Agent-to-Agent protocol
              </p>
            </div>
          </FormSection>
        </div>
      ),
    },
    {
      id: 'capabilities',
      label: 'Capabilities',
      hasError: false,
      content: (
        <div className="space-y-6">
          <FormSection
            title="Agent Capabilities"
            description="Define what this agent can do. Other agents use this to route requests appropriately."
          >
            <div className="space-y-3">
              <div className="flex gap-2">
                <input
                  type="text"
                  value={newCapability}
                  onChange={(e) => setNewCapability(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      handleAddCapability();
                    }
                  }}
                  placeholder="e.g., research, summarize, code_generation"
                  className="flex-1 border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-console-blue focus:border-transparent"
                />
                <button
                  type="button"
                  onClick={handleAddCapability}
                  className="px-4 py-2 text-sm font-medium text-white bg-console-blue rounded-md hover:bg-console-blue-dark transition-colors"
                >
                  Add
                </button>
              </div>

              {config.capabilities && config.capabilities.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {config.capabilities.map((capability) => (
                    <div
                      key={capability}
                      className="flex items-center gap-1.5 px-2.5 py-1.5 bg-gray-100 rounded-md text-sm text-gray-700"
                    >
                      <span>{capability}</span>
                      <button
                        type="button"
                        onClick={() => handleRemoveCapability(capability)}
                        className="text-gray-500 hover:text-red-600 transition-colors"
                        aria-label={`Remove ${capability}`}
                      >
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                        </svg>
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="space-y-2 mt-4">
              <label htmlFor="advertisedDescription" className="block text-sm font-medium text-gray-700">
                Agent Description
              </label>
              <textarea
                id="advertisedDescription"
                value={config.advertisedDescription || ''}
                onChange={(e) => updateConfig('advertisedDescription', e.target.value)}
                placeholder="Describe what this agent does. This description is shown to other agents when they discover this agent via A2A."
                rows={4}
                className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-console-blue focus:border-transparent"
              />
              <p className="text-xs text-gray-500">
                This description helps other agents understand when to delegate tasks to this agent.
              </p>
            </div>
          </FormSection>
        </div>
      ),
    },
    {
      id: 'security',
      label: 'Security',
      hasError: validationErrors.some((e) => e.field === 'peerAllowlist'),
      content: (
        <div className="space-y-6">
          <FormSection
            title="Peer Allowlist"
            description="Only agents at these base URLs are permitted to communicate with this agent. The allowlist is fail-closed: unlisted peers are rejected."
          >
            <div className="space-y-3">
              <div className="flex gap-2">
                <input
                  type="text"
                  value={newPeerUrl}
                  onChange={(e) => setNewPeerUrl(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      handleAddPeer();
                    }
                  }}
                  placeholder="https://agent.example.com"
                  className="flex-1 border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-console-blue focus:border-transparent"
                />
                <button
                  type="button"
                  onClick={handleAddPeer}
                  className="px-4 py-2 text-sm font-medium text-white bg-console-blue rounded-md hover:bg-console-blue-dark transition-colors"
                >
                  Add
                </button>
              </div>

              {getFieldError('peerAllowlist') && (
                <p className="text-xs text-red-600">{getFieldError('peerAllowlist')}</p>
              )}

              <p className="text-xs text-gray-500">
                All peer base URLs must use https:// protocol. Peers must be on this allowlist to communicate with this agent.
              </p>

              {config.peerAllowlist && config.peerAllowlist.length > 0 && (
                <div className="space-y-2">
                  {config.peerAllowlist.map((url) => (
                    <div
                      key={url}
                      className="flex items-center justify-between px-3 py-2 bg-gray-50 rounded-md border border-gray-200"
                    >
                      <span className="text-sm text-gray-700 font-mono">{url}</span>
                      <button
                        type="button"
                        onClick={() => handleRemovePeer(url)}
                        className="text-gray-500 hover:text-red-600 transition-colors"
                        aria-label={`Remove ${url}`}
                      >
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                        </svg>
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </FormSection>
        </div>
      ),
    },
    ];
  }, [config, validationErrors, updateConfig, newCapability, handleAddCapability, handleRemoveCapability, newPeerUrl, handleAddPeer, handleRemovePeer]);

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title="Configure Agent-to-Agent (A2A)"
      tabs={tabs}
      validationErrors={validationErrors}
    />
  );
}

export default A2AConfigurationModal;
