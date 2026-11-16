/**
 * CodeInterpreterConfiguration modal.
 *
 * A Code Interpreter node runs the agent's generated code in an AgentCore
 * sandbox session. It has nothing to configure beyond a display name and an
 * enabled flag, but it still gets a real dialog so the node is editable from the
 * Configure button / double-click like every other node (before this the node
 * had no modal at all and could not be opened).
 */

import { useState, useCallback, useMemo } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, CheckboxField, FormSection } from './FormFields';
import type { CodeInterpreterConfiguration } from '../../types/components';

export interface CodeInterpreterConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: CodeInterpreterConfiguration) => void;
  initialConfig?: Partial<CodeInterpreterConfiguration>;
}

function createDefaultConfig(): CodeInterpreterConfiguration {
  return { name: 'CodeInterpreter', enabled: true };
}

export function CodeInterpreterConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: CodeInterpreterConfigurationModalProps) {
  const [config, setConfig] = useState<CodeInterpreterConfiguration>(() => ({
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
    <K extends keyof CodeInterpreterConfiguration>(field: K, value: CodeInterpreterConfiguration[K]) => {
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
            title="Code Interpreter"
            description="Runs the agent's generated code in a secure AgentCore sandbox session. No network egress unless the runtime's VPC allows it."
          >
            <TextField
              label="Name"
              id="code-interpreter-name"
              value={config.name}
              onChange={(v) => updateField('name', v)}
              placeholder="CodeInterpreter"
              required
              error={validationErrors.find((e) => e.field === 'name')?.message}
              helpText="A label for this node on the canvas."
            />
            <div className="mt-4">
              <CheckboxField
                id="code-interpreter-enabled"
                label="Enabled"
                checked={config.enabled}
                onChange={(checked) => updateField('enabled', checked)}
                helpText="When enabled, the deployed agent can execute code in the sandbox."
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
      title="Configure Code Interpreter"
      tabs={tabs}
      validationErrors={validationErrors}
    />
  );
}
