/**
 * EvaluationConfiguration modal — Phase 1 Gap 1C.
 *
 * Configures AgentCore Online Evaluation. The deployed runtime gets a
 * CreateOnlineEvaluationConfig record that samples invocations and runs
 * builtin evaluators (Goal Success Rate, Correctness, Toxicity, etc).
 *
 * Custom evaluators are NOT supported by the AgentCore API today (the only
 * evaluator field is a string id) — the modal exposes the documented
 * Builtin set. When AWS adds a custom-evaluator API, extend this modal
 * with a code editor + judge prompt template.
 */

import { useState, useCallback, useMemo } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { CheckboxField, FormSection, SliderField, TextField } from './FormFields';

// ============================================================================
// Builtin evaluators (documented set — no API to enumerate)
// ============================================================================

interface BuiltinEvaluator {
  id: string;
  label: string;
  description: string;
  recommended: boolean;
}

const BUILTIN_EVALUATORS: BuiltinEvaluator[] = [
  {
    id: 'Builtin.GoalSuccessRate',
    label: 'Goal Success Rate',
    description: 'Did the agent achieve the user\'s stated objective?',
    recommended: true,
  },
  {
    id: 'Builtin.Correctness',
    label: 'Correctness',
    description: 'Are the agent\'s factual claims accurate?',
    recommended: true,
  },
  {
    id: 'Builtin.ToolSelectionAccuracy',
    label: 'Tool Selection Accuracy',
    description: 'Did the agent pick the right tool for each step?',
    recommended: true,
  },
  {
    id: 'Builtin.Helpfulness',
    label: 'Helpfulness',
    description: 'Does the response actually help the user?',
    recommended: false,
  },
  {
    id: 'Builtin.Toxicity',
    label: 'Toxicity',
    description: 'Did the response contain harmful, biased, or offensive content?',
    recommended: false,
  },
  {
    id: 'Builtin.GroundednessScore',
    label: 'Groundedness',
    description: 'Are claims supported by retrieved context (KB / tool output)?',
    recommended: false,
  },
  {
    id: 'Builtin.AnswerRelevance',
    label: 'Answer Relevance',
    description: 'Does the response address the actual question asked?',
    recommended: false,
  },
  {
    id: 'Builtin.ResponseCompleteness',
    label: 'Response Completeness',
    description: 'Did the agent address every part of the user\'s request?',
    recommended: false,
  },
  {
    id: 'Builtin.IntentResolution',
    label: 'Intent Resolution',
    description: 'Did the agent correctly identify what the user wanted?',
    recommended: false,
  },
];

// ============================================================================
// Wire shape
// ============================================================================

/**
 * Shape stored on the canvas Evaluation node. Travels through the deploy
 * request as `evaluation_config` and is consumed unchanged by
 * `step_handlers/evaluation_step.py` and `cfn_template_generator._add_evaluation`.
 */
export interface EvaluationNodeConfig {
  name: string;
  enabled: boolean;
  evaluators: string[]; // Builtin.<Name> ids
  samplingRate: number; // 1-100 (percent)
}

function createDefault(): EvaluationNodeConfig {
  return {
    name: 'Evaluation',
    enabled: true,
    evaluators: BUILTIN_EVALUATORS.filter((e) => e.recommended).map((e) => e.id),
    samplingRate: 100,
  };
}

// ============================================================================
// Props
// ============================================================================

export interface EvaluationConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: EvaluationNodeConfig) => void;
  initialConfig?: Partial<EvaluationNodeConfig>;
}

// ============================================================================
// Component
// ============================================================================

export function EvaluationConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: EvaluationConfigurationModalProps) {
  const [config, setConfig] = useState<EvaluationNodeConfig>(() => ({
    ...createDefault(),
    ...initialConfig,
    evaluators: (initialConfig?.evaluators && initialConfig.evaluators.length > 0)
      ? initialConfig.evaluators
      : createDefault().evaluators,
  }));

  const update = useCallback(
    <K extends keyof EvaluationNodeConfig>(k: K, v: EvaluationNodeConfig[K]) =>
      setConfig((prev) => ({ ...prev, [k]: v })),
    [],
  );

  const toggleEvaluator = useCallback((id: string) => {
    setConfig((prev) => {
      const has = prev.evaluators.includes(id);
      return {
        ...prev,
        evaluators: has
          ? prev.evaluators.filter((e) => e !== id)
          : [...prev.evaluators, id],
      };
    });
  }, []);

  const validationErrors: ValidationError[] = useMemo(() => {
    const errs: ValidationError[] = [];
    if (config.enabled && config.evaluators.length === 0) {
      errs.push({
        field: 'evaluators',
        message: 'Select at least one evaluator (or disable evaluation entirely).',
      });
    }
    return errs;
  }, [config]);

  const handleSave = useCallback(() => {
    onSave(config);
    onClose();
  }, [config, onSave, onClose]);

  const evaluatorsTab = (
    <div className="space-y-5">
      <FormSection
        title="Evaluators"
        description="Each evaluator is a model-graded score AgentCore writes to CloudWatch Logs after every sampled invocation. Custom evaluators are not yet supported by the AgentCore API."
      >
        <div className="space-y-2">
          {BUILTIN_EVALUATORS.map((ev) => (
            <label
              key={ev.id}
              className={`flex items-start gap-3 px-3 py-2 rounded-lg border cursor-pointer transition-colors ${
                config.evaluators.includes(ev.id)
                  ? 'border-emerald-300 bg-emerald-50'
                  : 'border-gray-200 bg-white hover:bg-gray-50'
              }`}
            >
              <input
                type="checkbox"
                className="mt-0.5"
                checked={config.evaluators.includes(ev.id)}
                onChange={() => toggleEvaluator(ev.id)}
              />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-900">{ev.label}</span>
                  {ev.recommended && (
                    <span className="text-[10px] uppercase tracking-wide bg-emerald-100 text-emerald-700 px-1.5 py-0.5 rounded">
                      recommended
                    </span>
                  )}
                </div>
                <div className="text-xs text-gray-600 mt-0.5">{ev.description}</div>
                <code className="text-[10px] font-mono text-gray-400 mt-0.5 block">
                  {ev.id}
                </code>
              </div>
            </label>
          ))}
        </div>
      </FormSection>
    </div>
  );

  const tuningTab = (
    <div className="space-y-5">
      <FormSection title="General">
        <TextField
          id="name"
          label="Name"
          value={config.name}
          onChange={(v) => update('name', v)}
          placeholder="Evaluation"
          helpText="Display name for this evaluation config in the canvas."
        />
        <CheckboxField
          id="enabled"
          label="Enable online evaluation"
          checked={config.enabled}
          onChange={(v) => update('enabled', v)}
          helpText="Disable to deploy this canvas without AgentCore Online Evaluation. The other settings are kept but not applied."
        />
      </FormSection>

      <FormSection
        title="Sampling"
        description="Percent of invocations to evaluate. Each evaluated invocation runs every selected evaluator, so cost scales with sampling rate × evaluator count."
      >
        <SliderField
          id="samplingRate"
          label="Sampling rate"
          value={config.samplingRate}
          onChange={(v) => update('samplingRate', v)}
          min={1}
          max={100}
          step={1}
          helpText={`${config.samplingRate}% of invocations will be evaluated.`}
        />
      </FormSection>
    </div>
  );

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title={`Configure Evaluation: ${config.name || 'Evaluation'}`}
      tabs={[
        { id: 'evaluators', label: 'Evaluators', content: evaluatorsTab },
        { id: 'tuning', label: 'General & Sampling', content: tuningTab },
      ]}
      validationErrors={validationErrors}
    />
  );
}
