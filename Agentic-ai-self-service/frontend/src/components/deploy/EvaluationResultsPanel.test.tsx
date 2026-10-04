/**
 * EvaluationResultsPanel regression tests.
 *
 * Guards the bug where the Evaluation tab spun on "Loading evaluation config…"
 * forever. The API correctly 404s when a runtime has no eval config — which is
 * every agent deployed without an Evaluation node, i.e. the common case — and
 * `isNotReadyError` deliberately swallows 404 so the user never sees a red error.
 * That left `cfg` and `cfgError` both null, which is indistinguishable from the
 * initial pre-fetch state, so the component fell through to its loading text and
 * stayed there. The amber "No evaluation config registered" empty state was
 * unreachable for the one condition it was written for.
 *
 * Observed live against the throwaway stack: GET
 * /api/runtimes/web_search_agent/evaluation-config -> 404, tab stuck loading.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { EvaluationResultsPanel } from './EvaluationResultsPanel';

const getEvaluationConfig = vi.fn();
const listEvaluationResults = vi.fn();
const getDashboardUrl = vi.fn();

vi.mock('../../services/api', () => ({
  getApiClient: () => ({
    getEvaluationConfig,
    listEvaluationResults,
    getDashboardUrl,
  }),
  getErrorMessage: (e: unknown) => (e instanceof Error ? e.message : String(e)),
  // Mirrors the real implementation: 401/403/404 are "not ready", not errors.
  isNotReadyError: (e: unknown) => [401, 403, 404].includes((e as { status?: number })?.status ?? 0),
}));

function httpError(status: number, message: string) {
  return Object.assign(new Error(message), { status });
}

describe('EvaluationResultsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getDashboardUrl.mockRejectedValue(httpError(404, 'Not found'));
  });

  it('shows the empty state — not a permanent spinner — when the config 404s', async () => {
    getEvaluationConfig.mockRejectedValue(httpError(404, 'No evaluation config found for this runtime'));
    listEvaluationResults.mockRejectedValue(httpError(404, 'Not found'));

    render(<EvaluationResultsPanel runtimeName="web_search_agent" />);

    await waitFor(() => {
      expect(screen.getByText(/No evaluation config registered for this runtime/)).toBeInTheDocument();
    });
    expect(screen.queryByText(/Loading evaluation config/)).not.toBeInTheDocument();
  });

  it('also settles the results block on a swallowed 404', async () => {
    getEvaluationConfig.mockRejectedValue(httpError(404, 'Not found'));
    listEvaluationResults.mockRejectedValue(httpError(404, 'Not found'));

    render(<EvaluationResultsPanel runtimeName="web_search_agent" />);

    await waitFor(() => {
      expect(screen.getByText(/No evaluation results available for this runtime yet/)).toBeInTheDocument();
    });
    expect(screen.queryByText(/Loading results/)).not.toBeInTheDocument();
  });

  it('still surfaces a REAL error in red rather than hiding it as an empty state', async () => {
    getEvaluationConfig.mockRejectedValue(httpError(500, 'AgentCore control plane unavailable'));
    listEvaluationResults.mockRejectedValue(httpError(500, 'AgentCore control plane unavailable'));

    render(<EvaluationResultsPanel runtimeName="web_search_agent" />);

    await waitFor(() => {
      expect(screen.getAllByText(/AgentCore control plane unavailable/).length).toBeGreaterThan(0);
    });
    expect(screen.queryByText(/No evaluation config registered/)).not.toBeInTheDocument();
  });

  it('renders the config when one exists', async () => {
    getEvaluationConfig.mockResolvedValue({
      runtime_name: 'web_search_agent',
      version_id: 'v1',
      runtime_id: 'web_search_agent_abc-XYZ',
      config_id: 'oec-123',
      config_name: 'eval_web_search_agent',
      evaluators: ['builtin.helpfulness', 'builtin.faithfulness'],
      sampling_rate: 10,
      status: 'ACTIVE',
    });
    listEvaluationResults.mockResolvedValue({
      runtime_name: 'web_search_agent',
      version_id: 'v1',
      runtime_id: 'web_search_agent_abc-XYZ',
      log_group_name: '/aws/bedrock-agentcore/runtimes/web_search_agent_abc-XYZ-DEFAULT',
      from_ts: 0,
      to_ts: 1,
      results: [{ eid: 'builtin.helpfulness', runs: '3', avg_score: '0.81', latest_score: '0.9' }],
    });

    render(<EvaluationResultsPanel runtimeName="web_search_agent" />);

    await waitFor(() => {
      expect(screen.getByText('oec-123', { exact: false })).toBeInTheDocument();
    });
    expect(screen.getByText(/builtin.helpfulness, builtin.faithfulness/)).toBeInTheDocument();
    expect(screen.queryByText(/No evaluation config registered/)).not.toBeInTheDocument();
  });

  it('prompts a deploy instead of calling the API with no runtime name', () => {
    render(<EvaluationResultsPanel runtimeName={null} />);
    expect(screen.getByText(/Deploy this agent at least once/)).toBeInTheDocument();
    expect(getEvaluationConfig).not.toHaveBeenCalled();
  });
});
