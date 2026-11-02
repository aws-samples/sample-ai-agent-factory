import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PromotionPolicy } from '../../services/api';
import { PromotionEvaluationPanel } from './PromotionEvaluationPanel';

const api = vi.hoisted(() => ({
  startPromotionEvaluation: vi.fn(),
  getPromotionEvaluation: vi.fn(),
  attestPromotionEvaluation: vi.fn(),
}));

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    getApiClient: () => api,
    getErrorMessage: (error: unknown) => error instanceof Error ? error.message : String(error),
  };
});

const policy: PromotionPolicy = {
  schema_version: 1,
  minimum_sessions: 2,
  maximum_failed_sessions: 0,
  maximum_ignored_sessions: 0,
  maximum_evaluator_failures: 0,
  max_evidence_age_seconds: 86_400,
  evaluators: { 'Builtin.Correctness': 0.8 },
  policy_digest: 'a'.repeat(64),
};

const scenarios = [
  {
    sessionId: 'session-1',
    testScenarioId: 'scenario-1',
    assertions: ['The answer is correct.'],
    expectedToolNames: [],
  },
  {
    sessionId: 'session-2',
    testScenarioId: 'scenario-2',
    assertions: ['The answer is complete.'],
    expectedToolNames: [],
  },
];

function renderPanel(onEvidenceRecorded = vi.fn(async () => undefined)) {
  render(
    <PromotionEvaluationPanel
      runtimeName="release_bot"
      versionId="version-1"
      policy={policy}
      evidence={null}
      onEvidenceRecorded={onEvidenceRecorded}
    />,
  );
  return onEvidenceRecorded;
}

describe('PromotionEvaluationPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.startPromotionEvaluation.mockResolvedValue({
      batch_evaluation_id: 'batch-1',
      batch_evaluation_arn: 'arn:batch-1',
      status: 'PENDING',
      policy,
      scenario_count: 2,
      request_digest: 'b'.repeat(64),
    });
    api.getPromotionEvaluation.mockResolvedValue({
      batch_evaluation_id: 'batch-1',
      status: 'COMPLETED',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      total_sessions: 2,
      completed_sessions: 2,
      failed_sessions: 0,
      ignored_sessions: 0,
      evaluator_summaries: [],
    });
    api.attestPromotionEvaluation.mockResolvedValue({
      runtime_name: 'release_bot',
      version_id: 'version-1',
      production_gate_required: true,
      promotion_policy: policy,
      promotion_evidence: null,
    });
  });

  it('refuses placeholder session ids before any API call', async () => {
    renderPanel();

    fireEvent.click(screen.getByRole('button', { name: /start batch evaluation/i }));

    expect(await screen.findByText(/replace the placeholder sessionId/i)).toBeInTheDocument();
    expect(api.startPromotionEvaluation).not.toHaveBeenCalled();
  });

  it('sends scenarios but never a client-authored verdict or score', async () => {
    renderPanel();
    fireEvent.change(screen.getByLabelText(/release scenarios/i), {
      target: { value: JSON.stringify(scenarios) },
    });

    fireEvent.click(screen.getByRole('button', { name: /start batch evaluation/i }));

    await waitFor(() => expect(api.startPromotionEvaluation).toHaveBeenCalledWith(
      'release_bot',
      'version-1',
      scenarios,
    ));
    expect(JSON.stringify(api.startPromotionEvaluation.mock.calls[0])).not.toContain('verdict');
    expect(JSON.stringify(api.startPromotionEvaluation.mock.calls[0])).not.toContain('average_score');
  });

  it('attests only after the server reports a completed batch', async () => {
    const onEvidenceRecorded = renderPanel();
    fireEvent.change(screen.getByLabelText(/release scenarios/i), {
      target: { value: JSON.stringify(scenarios) },
    });
    fireEvent.click(screen.getByRole('button', { name: /start batch evaluation/i }));
    await screen.findByRole('button', { name: /check and attest/i });

    fireEvent.click(screen.getByRole('button', { name: /check and attest/i }));

    await waitFor(() => expect(api.getPromotionEvaluation).toHaveBeenCalledWith(
      'release_bot',
      'version-1',
      'batch-1',
    ));
    expect(api.attestPromotionEvaluation).toHaveBeenCalledWith('release_bot', 'version-1', 'batch-1');
    expect(onEvidenceRecorded).toHaveBeenCalledOnce();
  });
});
