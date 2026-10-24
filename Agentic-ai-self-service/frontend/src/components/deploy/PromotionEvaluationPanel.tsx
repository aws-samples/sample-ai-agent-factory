import { useMemo, useState } from 'react';
import {
  getApiClient,
  getErrorMessage,
  type PromotionEvidence,
  type PromotionEvaluationScenario,
  type PromotionPolicy,
} from '../../services/api';

interface PromotionEvaluationPanelProps {
  runtimeName: string;
  versionId: string;
  policy: PromotionPolicy | null | undefined;
  evidence: PromotionEvidence | null | undefined;
  onEvidenceRecorded: () => Promise<void>;
}

function exampleScenarios(count: number): string {
  return JSON.stringify(
    Array.from({ length: Math.max(1, count) }, (_, index) => ({
      sessionId: `replace-with-staging-session-${index + 1}`,
      testScenarioId: `release-scenario-${index + 1}`,
      assertions: ['The response satisfies this scenario\'s release acceptance criteria.'],
      expectedToolNames: [],
    })),
    null,
    2,
  );
}

function parseScenarios(value: string): PromotionEvaluationScenario[] {
  const parsed: unknown = JSON.parse(value);
  if (!Array.isArray(parsed) || parsed.length === 0) {
    throw new Error('Scenarios must be a non-empty JSON array.');
  }
  for (const [index, item] of parsed.entries()) {
    if (!item || typeof item !== 'object') {
      throw new Error(`Scenario ${index + 1} must be an object.`);
    }
    const scenario = item as Record<string, unknown>;
    if (typeof scenario.sessionId !== 'string' || !scenario.sessionId.trim()) {
      throw new Error(`Scenario ${index + 1} needs a staging sessionId.`);
    }
    if (scenario.sessionId.startsWith('replace-with-')) {
      throw new Error(`Replace the placeholder sessionId in scenario ${index + 1}.`);
    }
    if (typeof scenario.testScenarioId !== 'string' || !scenario.testScenarioId.trim()) {
      throw new Error(`Scenario ${index + 1} needs a testScenarioId.`);
    }
  }
  return parsed as PromotionEvaluationScenario[];
}

export function PromotionEvaluationPanel({
  runtimeName,
  versionId,
  policy,
  evidence,
  onEvidenceRecorded,
}: PromotionEvaluationPanelProps) {
  const minimumSessions = policy?.minimum_sessions ?? 5;
  const [scenarioJson, setScenarioJson] = useState(() => exampleScenarios(minimumSessions));
  const [batchId, setBatchId] = useState('');
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const thresholds = useMemo(
    () => Object.entries(policy?.evaluators ?? {}),
    [policy],
  );

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const scenarios = parseScenarios(scenarioJson);
      if (scenarios.length < minimumSessions) {
        throw new Error(`Platform policy requires at least ${minimumSessions} scenarios.`);
      }
      const result = await getApiClient().startPromotionEvaluation(runtimeName, versionId, scenarios);
      setBatchId(result.batch_evaluation_id);
      setStatus(result.status);
    } catch (caught) {
      setError(getErrorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const checkAndAttest = async () => {
    if (!batchId) return;
    setBusy(true);
    setError(null);
    try {
      const result = await getApiClient().getPromotionEvaluation(runtimeName, versionId, batchId);
      setStatus(result.status);
      if (result.status === 'COMPLETED') {
        await getApiClient().attestPromotionEvaluation(runtimeName, versionId, batchId);
        await onEvidenceRecorded();
      } else if (!['PENDING', 'IN_PROGRESS', 'STOPPING'].includes(result.status)) {
        throw new Error(`Batch evaluation ended in ${result.status}; production remains blocked.`);
      }
    } catch (caught) {
      setError(getErrorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  if (evidence) {
    return (
      <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-[11px] text-emerald-800 space-y-1">
        <div className="font-medium">Production evidence verified</div>
        <div>
          {evidence.session_summary.completed} scenarios · expires{' '}
          {new Date(evidence.expires_at_epoch * 1000).toLocaleString()}
        </div>
        {evidence.evaluator_summaries.map((summary) => (
          <div key={summary.evaluator_id}>
            {summary.evaluator_id}: {summary.average_score.toFixed(3)} ≥ {summary.minimum_score.toFixed(3)}
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="rounded-md border border-blue-200 bg-blue-50 px-3 py-3 text-[11px] text-gray-700 space-y-2">
      <div className="font-medium text-blue-900">Production evaluation gate</div>
      <div>
        Invoke this staging version with at least {minimumSessions} representative sessions, then paste their
        session IDs and ground truth below. AgentCore evaluates the exact runtime; the backend—not this browser—
        verifies scores and writes immutable evidence.
      </div>
      {thresholds.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {thresholds.map(([evaluatorId, threshold]) => (
            <span key={evaluatorId} className="rounded bg-white border border-blue-200 px-1.5 py-0.5">
              {evaluatorId.replace('Builtin.', '')} ≥ {threshold.toFixed(2)}
            </span>
          ))}
        </div>
      )}
      <label className="block">
        <span className="font-medium">Release scenarios (JSON)</span>
        <textarea
          value={scenarioJson}
          onChange={(event) => setScenarioJson(event.target.value)}
          rows={10}
          spellCheck={false}
          className="mt-1 w-full rounded border border-blue-200 bg-white p-2 font-mono text-[10px] text-gray-800"
        />
      </label>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => void start()}
          disabled={busy || !policy}
          className="rounded bg-blue-700 px-2 py-1 text-white hover:bg-blue-800 disabled:opacity-40"
        >
          {busy && !batchId ? 'Starting…' : 'Start batch evaluation'}
        </button>
        {batchId && (
          <button
            type="button"
            onClick={() => void checkAndAttest()}
            disabled={busy}
            className="rounded border border-blue-300 bg-white px-2 py-1 text-blue-800 hover:bg-blue-100 disabled:opacity-40"
          >
            {busy ? 'Checking…' : 'Check and attest'}
          </button>
        )}
        {status && <span>Status: {status}</span>}
      </div>
      {!policy && <div className="text-red-700">This version has no valid frozen release policy.</div>}
      {error && <div className="rounded border border-red-200 bg-red-50 px-2 py-1 text-red-700">{error}</div>}
    </div>
  );
}
