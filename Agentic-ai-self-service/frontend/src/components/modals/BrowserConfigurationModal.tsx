/**
 * BrowserConfiguration modal.
 *
 * A Browser node gives the agent a cloud browser session (AgentCore Browser
 * Tool) for web interactions. Like Code Interpreter it has only a display name
 * and an enabled flag, but it gets a real dialog so the node is editable from
 * the Configure button / double-click (before this the node had no modal).
 */

import { useState, useCallback, useMemo } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, CheckboxField, FormSection } from './FormFields';
import type { BrowserConfiguration } from '../../types/components';

export interface BrowserConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: BrowserConfiguration) => void;
  initialConfig?: Partial<BrowserConfiguration>;
}

function createDefaultConfig(): BrowserConfiguration {
  return { name: 'BrowserTool', enabled: true };
}

export function BrowserConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: BrowserConfigurationModalProps) {
  const [config, setConfig] = useState<BrowserConfiguration>(() => ({
    ...createDefaultConfig(),
    ...initialConfig,
  }));

  const [lastInitial, setLastInitial] = useState<typeof initialConfig | symbol>(Symbol('unset'));
  if (isOpen && initialConfig !== lastInitial) {
    setLastInitial(initialConfig);
    setConfig({ ...createDefaultConfig(), ...initialConfig });
  }

  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];
    if (!config.name.trim()) {
      errors.push({ field: 'name', message: 'Name is required' });
    }
    return errors;
  }, [config.name]);

  const updateField = useCallback(
    <K extends keyof BrowserConfiguration>(field: K, value: BrowserConfiguration[K]) => {
      setConfig((prev) => ({ ...prev, [field]: value }));
    },
    [],
  );

  const handleSave = useCallback(() => onSave(config), [config, onSave]);

  const tabs = [
    {
      id: 'general',
      label: 'General',
      content: (
        <div className="space-y-6">
          <FormSection
            title="Browser Tool"
            description="Gives the deployed agent a managed cloud browser session for web interactions. Outbound reach follows the runtime's network placement."
          >
            <TextField
              label="Name"
              id="browser-name"
              value={config.name}
              onChange={(v) => updateField('name', v)}
              placeholder="BrowserTool"
              required
              error={validationErrors.find((e) => e.field === 'name')?.message}
              helpText="A label for this node on the canvas."
            />
            <div className="mt-4">
              <CheckboxField
                id="browser-enabled"
                label="Enabled"
                checked={config.enabled}
                onChange={(checked) => updateField('enabled', checked)}
                helpText="When enabled, the deployed agent can drive a cloud browser."
              />
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
      title="Configure Browser Tool"
      tabs={tabs}
      validationErrors={validationErrors}
    />
  );
}
