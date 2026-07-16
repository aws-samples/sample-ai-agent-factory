/**
 * GuardrailsConfiguration modal for configuring Amazon Bedrock Guardrails.
 * Supports "existing" mode (reference an existing guardrail) and "create_new" mode
 * (create a guardrail with content filters, PII filters, denied topics, and word filters).
 */

import { useState, useCallback, useMemo } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, FormSection } from './FormFields';
import type {
  GuardrailsConfiguration,
  GuardrailFilterStrength,
} from '../../types/components';

// ============================================================================
// Props
// ============================================================================

export interface GuardrailsConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: GuardrailsConfiguration) => void;
  initialConfig?: Partial<GuardrailsConfiguration>;
}

// ============================================================================
// Constants
// ============================================================================

const FILTER_STRENGTHS: GuardrailFilterStrength[] = ['NONE', 'LOW', 'MEDIUM', 'HIGH'];

const CONTENT_FILTER_CATEGORIES = [
  { key: 'hate', label: 'Hate Speech' },
  { key: 'insults', label: 'Insults' },
  { key: 'sexual', label: 'Sexual Content' },
  { key: 'violence', label: 'Violence' },
  { key: 'misconduct', label: 'Misconduct' },
  { key: 'prompt_attack', label: 'Prompt Attacks' },
] as const;

const PII_TYPES = [
  'EMAIL', 'PHONE', 'NAME', 'SSN', 'ADDRESS', 'CREDIT_DEBIT_CARD_NUMBER',
  'US_BANK_ACCOUNT_NUMBER', 'US_SOCIAL_SECURITY_NUMBER', 'IP_ADDRESS', 'URL',
  'AGE', 'USERNAME', 'PASSWORD', 'DRIVER_ID', 'LICENSE_PLATE',
] as const;

const DEFAULT_CONFIG: GuardrailsConfiguration = {
  name: 'Guardrails',
  enabled: true,
  mode: 'create_new',
  contentFilters: {
    hate: 'HIGH',
    insults: 'HIGH',
    sexual: 'HIGH',
    violence: 'HIGH',
    misconduct: 'HIGH',
    prompt_attack: 'HIGH',
  },
  piiFilters: [],
  deniedTopics: [],
  wordFilters: [],
};

// ============================================================================
// Component
// ============================================================================

export function GuardrailsConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: GuardrailsConfigurationModalProps) {
  const [config, setConfig] = useState<GuardrailsConfiguration>(() => ({
    ...DEFAULT_CONFIG,
    ...initialConfig,
  }));

  // Reset config when modal opens with new initial config (adjust state during render pattern)
  const [lastInitial, setLastInitial] = useState<typeof initialConfig | symbol>(Symbol('unset'));
  if (isOpen && initialConfig !== lastInitial) {
    setLastInitial(initialConfig);
    setConfig({ ...DEFAULT_CONFIG, ...initialConfig });
  }

  const updateField = useCallback(<K extends keyof GuardrailsConfiguration>(
    field: K,
    value: GuardrailsConfiguration[K]
  ) => {
    setConfig((prev) => ({ ...prev, [field]: value }));
  }, []);

  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];
    if (config.mode === 'existing') {
      if (!config.guardrailId?.trim()) {
        errors.push({ field: 'guardrailId', message: 'Guardrail ID is required' });
      }
    }
    return errors;
  }, [config]);

  const handleSave = useCallback(() => {
    onSave(config);
  }, [config, onSave]);

  // -- Tab 1: Mode Selection --
  const modeTab = (
    <div className="space-y-5">
      <FormSection title="Guardrail Source">
        <div className="space-y-2">
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="radio"
              name="guardrail-mode"
              value="create_new"
              checked={config.mode === 'create_new'}
              onChange={() => updateField('mode', 'create_new')}
              className="text-console-blue"
            />
            <span className="text-sm font-medium text-gray-700">Create New Guardrail</span>
          </label>
          <p className="text-xs text-gray-500 ml-6">Create a new Bedrock Guardrail with custom filters</p>

          <label className="flex items-center gap-2 cursor-pointer mt-3">
            <input
              type="radio"
              name="guardrail-mode"
              value="existing"
              checked={config.mode === 'existing'}
              onChange={() => updateField('mode', 'existing')}
              className="text-console-blue"
            />
            <span className="text-sm font-medium text-gray-700">Use Existing Guardrail</span>
          </label>
          <p className="text-xs text-gray-500 ml-6">Reference a guardrail already created in your account</p>
        </div>
      </FormSection>

      {config.mode === 'existing' && (
        <FormSection title="Existing Guardrail">
          <TextField
            label="Guardrail ID"
            id="guardrail-id"
            value={config.guardrailId || ''}
            onChange={(v) => updateField('guardrailId', v)}
            placeholder="e.g., abc123def456"
            error={validationErrors.find((e) => e.field === 'guardrailId')?.message}
          />
          <TextField
            label="Version"
            id="guardrail-version"
            value={config.guardrailVersion || 'DRAFT'}
            onChange={(v) => updateField('guardrailVersion', v)}
            placeholder="DRAFT or version number"
          />
        </FormSection>
      )}
    </div>
  );

  // -- Tab 2: Content Filters --
  const contentFiltersTab = (
    <div className="space-y-5">
      <FormSection title="Content Filter Strengths">
        <p className="text-xs text-gray-500 mb-3">
          Set the filtering strength for each content category. NONE disables filtering.
        </p>
        <div className="space-y-3">
          {CONTENT_FILTER_CATEGORIES.map(({ key, label }) => (
            <div key={key} className="flex items-center justify-between">
              <span className="text-sm text-gray-700 w-32">{label}</span>
              <div className="flex gap-1">
                {FILTER_STRENGTHS.map((strength) => (
                  <button
                    key={strength}
                    type="button"
                    onClick={() => updateField('contentFilters', {
                      ...config.contentFilters,
                      [key]: strength,
                    })}
                    className={`px-2 py-1 text-xs rounded border transition-colors ${
                      (config.contentFilters as Record<string, string>)?.[key] === strength
                        ? 'bg-console-blue text-white border-console-blue'
                        : 'bg-white text-gray-600 border-gray-300 hover:border-gray-400'
                    }`}
                  >
                    {strength}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </FormSection>
    </div>
  );

  // -- Tab 3: PII & Word Filters --
  const piiTab = (
    <div className="space-y-5">
      <FormSection title="PII Detection">
        <p className="text-xs text-gray-500 mb-3">
          Select PII types to detect and whether to block or anonymize them.
        </p>
        <div className="max-h-40 overflow-y-auto space-y-1">
          {PII_TYPES.map((piiType) => {
            const existing = config.piiFilters?.find((f) => f.type === piiType);
            return (
              <label key={piiType} className="flex items-center gap-2 text-xs cursor-pointer">
                <input
                  type="checkbox"
                  checked={!!existing}
                  onChange={(e) => {
                    const current = config.piiFilters || [];
                    if (e.target.checked) {
                      updateField('piiFilters', [...current, { type: piiType, action: 'ANONYMIZE' as const }]);
                    } else {
                      updateField('piiFilters', current.filter((f) => f.type !== piiType));
                    }
                  }}
                  className="text-console-blue"
                />
                <span className="text-gray-700 flex-1">{piiType.replace(/_/g, ' ')}</span>
                {existing && (
                  <select
                    value={existing.action}
                    onChange={(e) => {
                      const current = config.piiFilters || [];
                      updateField('piiFilters', current.map((f) =>
                        f.type === piiType ? { ...f, action: e.target.value as 'BLOCK' | 'ANONYMIZE' } : f
                      ));
                    }}
                    className="text-xs border rounded px-1 py-0.5"
                  >
                    <option value="ANONYMIZE">Anonymize</option>
                    <option value="BLOCK">Block</option>
                  </select>
                )}
              </label>
            );
          })}
        </div>
      </FormSection>

      <FormSection title="Word Filters">
        <p className="text-xs text-gray-500 mb-2">Comma-separated list of words to block</p>
        <textarea
          value={(config.wordFilters || []).join(', ')}
          onChange={(e) => updateField('wordFilters', e.target.value.split(',').map((w) => w.trim()).filter(Boolean))}
          className="w-full border rounded px-3 py-2 text-sm"
          rows={2}
          placeholder="word1, word2, word3"
        />
      </FormSection>

      <FormSection title="Denied Topics">
        <p className="text-xs text-gray-500 mb-2">Topics the agent should refuse to discuss</p>
        {(config.deniedTopics || []).map((topic, i) => (
          <div key={i} className="flex gap-2 mb-2">
            <input
              value={topic.name}
              onChange={(e) => {
                const topics = [...(config.deniedTopics || [])];
                topics[i] = { ...topics[i], name: e.target.value };
                updateField('deniedTopics', topics);
              }}
              className="flex-1 border rounded px-2 py-1 text-sm"
              placeholder="Topic name"
            />
            <input
              value={topic.definition}
              onChange={(e) => {
                const topics = [...(config.deniedTopics || [])];
                topics[i] = { ...topics[i], definition: e.target.value };
                updateField('deniedTopics', topics);
              }}
              className="flex-2 border rounded px-2 py-1 text-sm"
              placeholder="Topic definition"
            />
            <button
              type="button"
              onClick={() => {
                const topics = (config.deniedTopics || []).filter((_, idx) => idx !== i);
                updateField('deniedTopics', topics);
              }}
              className="text-red-500 text-sm hover:text-red-700"
            >
              Remove
            </button>
          </div>
        ))}
        <button
          type="button"
          onClick={() => updateField('deniedTopics', [...(config.deniedTopics || []), { name: '', definition: '' }])}
          className="text-xs text-console-blue hover:underline"
        >
          + Add Denied Topic
        </button>
      </FormSection>
    </div>
  );

  const tabs = config.mode === 'existing'
    ? [{ id: 'mode', label: 'Configuration', content: modeTab, hasError: validationErrors.length > 0 }]
    : [
        { id: 'mode', label: 'Configuration', content: modeTab, hasError: validationErrors.length > 0 },
        { id: 'content', label: 'Content Filters', content: contentFiltersTab, hasError: false },
        { id: 'pii', label: 'PII & Words', content: piiTab, hasError: false },
      ];

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title="Configure Bedrock Guardrails"
      tabs={tabs}
      validationErrors={validationErrors}
    />
  );
}
