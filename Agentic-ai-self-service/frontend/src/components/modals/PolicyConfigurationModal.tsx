/**
 * PolicyConfiguration modal for configuring AgentCore Policy Engine.
 * Supports Cedar policy statement editing.
 */

import { useState, useCallback, useMemo, useEffect } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, FormSection } from './FormFields';
import type { PolicyConfiguration, PolicyRule } from '../../types/components';

// ============================================================================
// Props Interface
// ============================================================================

export interface PolicyConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: PolicyConfiguration) => void;
  initialConfig?: Partial<PolicyConfiguration>;
}

function createDefaultPolicyConfig(): PolicyConfiguration {
  return {
    name: 'GatewayPolicy',
    enabled: true,
    rules: [
      {
        ruleId: 'default-permit-all',
        effect: 'permit',
        description: 'Default permit-all policy for gateway tools',
        conditions: [],
      },
    ],
    defaultEffect: 'permit',
    enableNlAuthoring: false,
    strictValidation: false,
    enableAuditLog: false,
  };
}

// ============================================================================
// Cedar Policy Preview
// ============================================================================

function toCedarStatement(rule: PolicyRule, gatewayArn?: string): string {
  const effect = rule.effect || 'permit';
  const principal = rule.principal || 'principal';
  const action = rule.action || 'action';
  let resource = rule.resource || 'resource';

  // If no explicit resource and we have a gateway ARN, use it
  if (!rule.resource && gatewayArn) {
    resource = `resource == AgentCore::Gateway::"${gatewayArn}"`;
  }

  return `${effect}(${principal}, ${action}, ${resource});`;
}

// ============================================================================
// PolicyConfigurationModal Component
// ============================================================================

export function PolicyConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: PolicyConfigurationModalProps) {
  const [config, setConfig] = useState<PolicyConfiguration>(() => ({
    ...createDefaultPolicyConfig(),
    ...initialConfig,
  }));

  useEffect(() => {
    if (isOpen) {
      setConfig({
        ...createDefaultPolicyConfig(),
        ...initialConfig,
      });
    }
  }, [isOpen, initialConfig]);

  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];
    if (!config.name.trim()) {
      errors.push({ field: 'name', message: 'Policy engine name is required' });
    }
    if (config.rules.length === 0) {
      errors.push({ field: 'rules', message: 'At least one policy rule is required' });
    }
    return errors;
  }, [config]);

  const updateField = useCallback(<K extends keyof PolicyConfiguration>(field: K, value: PolicyConfiguration[K]) => {
    setConfig((prev) => ({ ...prev, [field]: value }));
  }, []);

  const updateRule = useCallback((index: number, updates: Partial<PolicyRule>) => {
    setConfig((prev) => ({
      ...prev,
      rules: prev.rules.map((r, i) => (i === index ? { ...r, ...updates } : r)),
    }));
  }, []);

  const addRule = useCallback(() => {
    setConfig((prev) => ({
      ...prev,
      rules: [
        ...prev.rules,
        {
          ruleId: `rule-${Date.now()}`,
          effect: prev.defaultEffect,
          description: '',
          conditions: [],
        },
      ],
    }));
  }, []);

  const removeRule = useCallback((index: number) => {
    setConfig((prev) => ({
      ...prev,
      rules: prev.rules.filter((_, i) => i !== index),
    }));
  }, []);

  const handleSave = useCallback(() => {
    onSave(config);
  }, [config, onSave]);

  const tabs = [
    {
      id: 'general',
      label: 'General',
      content: (
        <div className="space-y-6">
          <FormSection title="Policy Engine Settings">
            <TextField
              label="Policy Engine Name"
              id="policy-name"
              value={config.name}
              onChange={(v) => updateField('name', v)}
              placeholder="GatewayPolicy"
              required
            />
            <div className="space-y-1">
              <label className="block text-sm font-medium text-gray-700">Default Effect</label>
              <select
                value={config.defaultEffect}
                onChange={(e) => updateField('defaultEffect', e.target.value as 'permit' | 'forbid')}
                className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-blue-500 focus:border-blue-500"
              >
                <option value="permit">Permit (allow by default)</option>
                <option value="forbid">Forbid (deny by default)</option>
              </select>
              <p className="text-xs text-gray-500">The default effect when no policy matches</p>
            </div>
          </FormSection>
        </div>
      ),
    },
    {
      id: 'rules',
      label: 'Policy Rules',
      content: (
        <div className="space-y-6">
          <FormSection title="Cedar Policies" description="Define Cedar policy rules that control access to gateway tools. Each rule generates a Cedar policy statement.">
            <div className="space-y-4">
              {config.rules.map((rule, index) => (
                <div key={rule.ruleId} className="border border-gray-200 rounded-lg p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium text-gray-700">Rule {index + 1}</span>
                    {config.rules.length > 1 && (
                      <button
                        onClick={() => removeRule(index)}
                        className="text-xs text-red-500 hover:text-red-700"
                      >
                        Remove
                      </button>
                    )}
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <label className="block text-xs font-medium text-gray-600">Effect</label>
                      <select
                        value={rule.effect}
                        onChange={(e) => updateRule(index, { effect: e.target.value as 'permit' | 'forbid' })}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 rounded-md"
                      >
                        <option value="permit">Permit</option>
                        <option value="forbid">Forbid</option>
                      </select>
                    </div>
                    <TextField
                      label="Description"
                      id={`rule-desc-${index}`}
                      value={rule.description || ''}
                      onChange={(v) => updateRule(index, { description: v })}
                      placeholder="Describe this policy rule"
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="block text-xs font-medium text-gray-600">Resource (optional)</label>
                    <input
                      value={rule.resource || ''}
                      onChange={(e) => updateRule(index, { resource: e.target.value })}
                      placeholder='resource == AgentCore::Gateway::"{gateway_arn}"'
                      className="w-full px-2 py-1.5 text-xs font-mono border border-gray-300 rounded-md"
                    />
                    <p className="text-xs text-gray-400">Leave empty to auto-fill with the deployed gateway ARN</p>
                  </div>
                  {/* Cedar preview */}
                  <div className="bg-gray-50 rounded p-2">
                    <div className="text-xs text-gray-500 mb-1">Cedar Preview:</div>
                    <code className="text-xs font-mono text-gray-700 break-all">
                      {toCedarStatement(rule)}
                    </code>
                  </div>
                </div>
              ))}
              <button
                onClick={addRule}
                className="w-full py-2 text-sm text-blue-600 border border-dashed border-blue-300 rounded-lg hover:bg-blue-50 transition-colors"
              >
                + Add Rule
              </button>
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
      title="Configure Policy Engine"
      tabs={tabs}
      validationErrors={validationErrors}
    />
  );
}
