/**
 * ToolConfigModal — configuration for `tool` nodes (built-in and custom).
 *
 * Until now the only `tool`-typed modal was KnowledgeBaseConfigModal, gated on
 * `isKnowledgeBase`. A plain custom tool (isCustom=true) or a built-in tool had
 * NO modal, so its "Configure" action opened nothing. This modal fills that gap.
 *
 * Scope is deliberately "basic editable": the display name, description, and
 * enabled flag are editable; the generated Lambda code and input schema are
 * shown read-only. Editing generated Lambda by hand is fragile and out of scope
 * — regenerate the tool via the AI Tool Generator to change its behaviour.
 * `toolId` / `isCustom` and every other field are preserved verbatim on save so
 * the deploy-time tool extraction (App.tsx) keeps working.
 */

import { useState, useCallback, useMemo } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { FormSection, TextField, TextArea, Toggle } from './FormFields';
import type { ToolConfiguration } from '../../types/components';

// ============================================================================
// Props
// ============================================================================

export interface ToolConfigModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: ToolConfiguration) => void;
  initialConfig?: Partial<ToolConfiguration>;
}

// ============================================================================
// Component
// ============================================================================

export function ToolConfigModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: ToolConfigModalProps) {
  // Preserve every field we don't surface (toolId, isCustom, lambdaCode,
  // inputSchema, ...) by spreading initialConfig as the base, then backfilling
  // only the fields that are missing.
  const [config, setConfig] = useState<ToolConfiguration>(() => ({
    ...(initialConfig as ToolConfiguration),
    name: initialConfig?.name ?? '',
    toolId: initialConfig?.toolId ?? '',
    description: initialConfig?.description ?? '',
    enabled: initialConfig?.enabled ?? true,
  }));

  const update = useCallback(
    <K extends keyof ToolConfiguration>(k: K, v: ToolConfiguration[K]) =>
      setConfig((prev) => ({ ...prev, [k]: v })),
    [],
  );

  const isCustom = !!config.isCustom;
  const displayName = config.displayName ?? config.name ?? '';

  const validationErrors: ValidationError[] = useMemo(() => {
    const errs: ValidationError[] = [];
    if (!displayName.trim()) {
      errs.push({ field: 'displayName', message: 'A tool name is required.' });
    }
    return errs;
  }, [displayName]);

  const handleSave = useCallback(() => {
    // Keep `name` and `displayName` in sync — the deploy extraction falls back
    // from displayName -> toolId, and the canvas label reads `name`.
    const next: ToolConfiguration = {
      ...config,
      name: displayName.trim(),
      displayName: displayName.trim(),
    };
    onSave(next);
    onClose();
  }, [config, displayName, onSave, onClose]);

  const inputSchemaJson = useMemo(() => {
    if (!config.inputSchema) return '';
    try {
      return JSON.stringify(config.inputSchema, null, 2);
    } catch {
      return '';
    }
  }, [config.inputSchema]);

  const generalTab = (
    <div className="space-y-5">
      <FormSection title="General">
        <TextField
          id="displayName"
          label="Tool name"
          required
          value={displayName}
          onChange={(v) => update('displayName', v)}
          placeholder="My Tool"
          helpText="Display name shown on the canvas node."
        />
        <TextField
          id="toolId"
          label="Tool ID"
          value={config.toolId}
          onChange={() => undefined}
          disabled
          helpText={
            isCustom
              ? 'Stable identifier used to generate the tool Lambda. Not editable.'
              : 'Built-in tool identifier. Not editable.'
          }
        />
        <TextArea
          id="description"
          label="Description"
          value={config.description}
          onChange={(v) => update('description', v)}
          rows={3}
          placeholder="What this tool does — the agent uses this to decide when to call it."
          helpText="A precise description helps the agent call the tool correctly."
        />
        <Toggle
          id="enabled"
          label="Enabled"
          checked={config.enabled}
          onChange={(v) => update('enabled', v)}
          description="Disable to keep the tool on the canvas without deploying it."
        />
      </FormSection>
    </div>
  );

  // Custom tools carry a generated input schema + Lambda code. Show them
  // read-only so users can inspect what was generated; regeneration is the
  // supported way to change them.
  const implementationTab = (
    <div className="space-y-5">
      <FormSection
        title="Input schema"
        description="The parameters the agent passes when calling this tool (read-only — regenerate the tool to change it)."
      >
        {inputSchemaJson ? (
          <pre className="text-xs font-mono bg-gray-50 border border-gray-200 rounded-lg p-3 overflow-auto max-h-48 whitespace-pre">
            {inputSchemaJson}
          </pre>
        ) : (
          <p className="text-xs text-gray-500">No input schema defined.</p>
        )}
      </FormSection>

      <FormSection
        title="Lambda code"
        description="The generated implementation that runs when the tool is invoked (read-only)."
      >
        {config.lambdaCode ? (
          <pre className="text-xs font-mono bg-gray-50 border border-gray-200 rounded-lg p-3 overflow-auto max-h-64 whitespace-pre">
            {config.lambdaCode}
          </pre>
        ) : (
          <p className="text-xs text-gray-500">No generated code for this tool.</p>
        )}
      </FormSection>
    </div>
  );

  const tabs = [{ id: 'general', label: 'General', content: generalTab }];
  if (isCustom) {
    tabs.push({ id: 'implementation', label: 'Implementation', content: implementationTab });
  }

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title={`Configure Tool: ${displayName || config.toolId || 'Tool'}`}
      tabs={tabs}
      validationErrors={validationErrors}
    />
  );
}

export default ToolConfigModal;
