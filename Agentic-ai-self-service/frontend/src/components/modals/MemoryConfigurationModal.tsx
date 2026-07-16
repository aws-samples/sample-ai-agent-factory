/**
 * MemoryConfiguration modal for configuring AgentCore Memory components.
 * Supports SEMANTIC, SUMMARY, and EPISODIC extraction strategies.
 */

import { useState, useCallback, useMemo } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, FormSection } from './FormFields';
import type { MemoryConfiguration, MemoryStrategyConfig, ExtractionStrategy } from '../../types/components';

// ============================================================================
// Props Interface
// ============================================================================

export interface MemoryConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: MemoryConfiguration) => void;
  initialConfig?: Partial<MemoryConfiguration>;
}

// ============================================================================
// Strategy Options
// ============================================================================

const STRATEGY_OPTIONS: { type: ExtractionStrategy; label: string; description: string }[] = [
  {
    type: 'semantic',
    label: 'Semantic',
    description: 'Extract and store semantically meaningful information from conversations',
  },
  {
    type: 'summary',
    label: 'Summary',
    description: 'Generate concise summaries of conversation sessions',
  },
  {
    type: 'episodic',
    label: 'Episodic',
    description: 'Store complete conversation episodes for contextual recall',
  },
  {
    type: 'user_preferences',
    label: 'User Preferences',
    description: 'Learn and store user preferences over time',
  },
];

function createDefaultMemoryConfig(): MemoryConfiguration {
  return {
    name: 'AgentMemory',
    enabled: true,
    eventExpiryDuration: 90,
    strategies: [
      {
        type: 'semantic',
        name: 'semantic_strategy',
        description: 'Semantic extraction strategy',
      },
    ],
  };
}

// ============================================================================
// MemoryConfigurationModal Component
// ============================================================================

export function MemoryConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: MemoryConfigurationModalProps) {
  const [config, setConfig] = useState<MemoryConfiguration>(() => ({
    ...createDefaultMemoryConfig(),
    ...initialConfig,
  }));

  // Reset config when modal opens with new initial config (adjust state during render pattern)
  const [lastInitial, setLastInitial] = useState<typeof initialConfig | symbol>(Symbol('unset'));
  if (isOpen && initialConfig !== lastInitial) {
    setLastInitial(initialConfig);
    setConfig({ ...createDefaultMemoryConfig(), ...initialConfig });
  }

  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];
    if (!config.name.trim()) {
      errors.push({ field: 'name', message: 'Memory name is required' });
    }
    if (!/^[a-zA-Z][a-zA-Z0-9_]*$/.test(config.name)) {
      errors.push({ field: 'name', message: 'Name must start with a letter and contain only letters, numbers, and underscores (no hyphens)' });
    }
    return errors;
  }, [config]);

  const updateField = useCallback(<K extends keyof MemoryConfiguration>(field: K, value: MemoryConfiguration[K]) => {
    setConfig((prev) => ({ ...prev, [field]: value }));
  }, []);

  const toggleStrategy = useCallback((strategyType: ExtractionStrategy) => {
    setConfig((prev) => {
      const strategies = prev.strategies || [];
      const existing = strategies.find((s) => s.type === strategyType);
      if (existing) {
        return { ...prev, strategies: strategies.filter((s) => s.type !== strategyType) };
      }
      return {
        ...prev,
        strategies: [
          ...strategies,
          {
            type: strategyType,
            name: `${strategyType}_strategy`,
            description: `${strategyType.charAt(0).toUpperCase() + strategyType.slice(1)} extraction strategy`,
          },
        ],
      };
    });
  }, []);

  const updateStrategy = useCallback((strategyType: ExtractionStrategy, field: keyof MemoryStrategyConfig, value: string) => {
    setConfig((prev) => ({
      ...prev,
      strategies: (prev.strategies || []).map((s) =>
        s.type === strategyType ? { ...s, [field]: value } : s
      ),
    }));
  }, []);

  const handleSave = useCallback(() => {
    onSave(config);
  }, [config, onSave]);

  const activeStrategies = new Set((config.strategies || []).map((s) => s.type));

  const tabs = [
    {
      id: 'general',
      label: 'General',
      content: (
        <div className="space-y-6">
          <FormSection title="Memory Settings">
            <TextField
              label="Memory Name"
              id="memory-name"
              value={config.name}
              onChange={(v) => updateField('name', v)}
              placeholder="AgentMemory"
              required
              helpText="Must start with a letter, only letters/numbers/underscores (no hyphens)"
              error={validationErrors.find((e) => e.field === 'name')?.message}
            />
            <div className="mt-4">
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Event Expiry Duration
              </label>
              <select
                className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm"
                value={config.eventExpiryDuration ?? 90}
                onChange={(e) => updateField('eventExpiryDuration', Number(e.target.value))}
              >
                {[7, 30, 60, 90, 180, 365].map((d) => (
                  <option key={d} value={d}>{d} days</option>
                ))}
              </select>
              <p className="mt-1 text-xs text-gray-500">How long raw conversation events are retained (3–365 days)</p>
            </div>
          </FormSection>
        </div>
      ),
    },
    {
      id: 'strategies',
      label: 'Strategies',
      content: (
        <div className="space-y-6">
          <FormSection title="Extraction Strategies" description="Select which memory strategies to enable. Each strategy processes conversations differently.">
            <div className="space-y-3">
              {STRATEGY_OPTIONS.map((opt) => {
                const isActive = activeStrategies.has(opt.type);
                const strategyConfig = (config.strategies || []).find((s) => s.type === opt.type);
                return (
                  <div key={opt.type} className={`border rounded-lg p-4 transition-colors ${isActive ? 'border-blue-300 bg-blue-50/50' : 'border-gray-200'}`}>
                    <label className="flex items-start gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={isActive}
                        onChange={() => toggleStrategy(opt.type)}
                        className="mt-1 w-4 h-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500"
                      />
                      <div className="flex-1">
                        <div className="font-medium text-sm text-gray-800">{opt.label}</div>
                        <div className="text-xs text-gray-500 mt-0.5">{opt.description}</div>
                      </div>
                    </label>
                    {isActive && strategyConfig && (
                      <div className="mt-3 ml-7 space-y-3">
                        <TextField
                          label="Strategy Name"
                          id={`strategy-name-${opt.type}`}
                          value={strategyConfig.name}
                          onChange={(v) => updateStrategy(opt.type, 'name', v)}
                          placeholder={`${opt.type}-strategy`}
                        />
                        <TextField
                          label="Description"
                          id={`strategy-desc-${opt.type}`}
                          value={strategyConfig.description}
                          onChange={(v) => updateStrategy(opt.type, 'description', v)}
                          placeholder={`${opt.label} extraction strategy`}
                        />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </FormSection>
        </div>
      ),
    },
  ];

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title="Configure Memory"
      tabs={tabs}
      validationErrors={validationErrors}
    />
  );
}
