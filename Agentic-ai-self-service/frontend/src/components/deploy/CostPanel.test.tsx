import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CostPanel } from './CostPanel';

const getCost = vi.fn();
const upsertOwnerBudget = vi.fn();

vi.mock('../../services/api', () => ({
  getApiClient: () => ({ getCost, upsertOwnerBudget }),
  getErrorMessage: (error: unknown) =>
    error instanceof Error ? error.message : String(error),
  isNotReadyError: (error: unknown) =>
    [403, 404].includes((error as { status?: number })?.status ?? 0),
}));

describe('CostPanel cache accounting', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    upsertOwnerBudget.mockResolvedValue({
      scope: 'owner',
      key: 'self',
      limit_usd: 100,
      warn_pct: 80,
    });
  });

  it('shows every billed input category and warns when cache telemetry is incomplete', async () => {
    getCost.mockResolvedValue({
      total_cost: 0.1234,
      total_in: 1_000,
      total_cache_read: 4_000,
      total_cache_write: 500,
      total_input_tokens: 5_500,
      total_out: 800,
      cache_reporting: {
        cache_read_complete: true,
        cache_write_complete: false,
        invocations: 2,
        cache_read_reports: 2,
        cache_write_reports: 1,
      },
      by_model: {
        'global.anthropic.claude-sonnet-5': {
          input_tokens: 1_000,
          cache_read_input_tokens: 4_000,
          cache_write_input_tokens: 500,
          total_input_tokens: 5_500,
          output_tokens: 800,
          cost: 0.1234,
          count: 2,
          cache_read_complete: true,
          cache_write_complete: false,
        },
      },
    });

    render(<CostPanel runtimeName="cached-agent" />);

    expect(await screen.findByText('Total Cost')).toBeInTheDocument();
    expect(screen.getByText('Total input:').parentElement).toHaveTextContent(
      '5,500',
    );
    expect(screen.getByText('Uncached input:').parentElement).toHaveTextContent(
      '1,000',
    );
    expect(screen.getAllByText('Cache reads:')[0].parentElement).toHaveTextContent(
      '4,000',
    );
    expect(screen.getAllByText('Cache writes:')[0].parentElement).toHaveTextContent(
      '500',
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'Cache-write token telemetry is incomplete',
    );

    const modelRow = screen
      .getByText('global.anthropic.claude-sonnet-5')
      .closest('li');
    expect(modelRow).toHaveTextContent('Total in: 5,500');
    expect(modelRow).toHaveTextContent('Cache reads: 4,000');
    expect(modelRow).toHaveTextContent('Cache writes: 500');
    expect(modelRow).toHaveTextContent('2 invocations');
  });

  it('remains compatible with an older backend that returns no cache fields', async () => {
    getCost.mockResolvedValue({
      total_cost: 0.012,
      total_in: 1_000,
      total_out: 200,
      by_model: {
        'anthropic.claude-sonnet-5': {
          input_tokens: 1_000,
          output_tokens: 200,
          cost: 0.012,
        },
      },
    });

    render(<CostPanel runtimeName="legacy-agent" />);

    expect(await screen.findByText('Total Cost')).toBeInTheDocument();
    expect(screen.getByText('Input tokens:').parentElement).toHaveTextContent(
      '1,000',
    );
    expect(screen.queryByText('Cache reads:')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('lets a user set the required owner budget before any usage exists', async () => {
    getCost.mockResolvedValue({
      total_cost: 0,
      total_in: 0,
      total_out: 0,
      by_model: {},
    });

    render(<CostPanel runtimeName="new-agent" />);
    await screen.findByText(/No usage recorded yet/i);
    fireEvent.change(screen.getByLabelText('Monthly invocation authorization limit'), {
      target: { value: '50' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Set budget' }));

    await waitFor(() => expect(upsertOwnerBudget).toHaveBeenCalledWith(50, 80));
    expect(await screen.findByText('Monthly invocation authorization set to $50.00.')).toBeInTheDocument();
    expect(screen.getByText(/not an actual AWS bill cap/i)).toBeInTheDocument();
    expect(screen.getByText(/downstream tool\/vendor charges/i)).toBeInTheDocument();
  });

  it('shows authorized, reserved, committed, and remaining capacity', async () => {
    getCost.mockResolvedValue({
      total_cost: 1,
      total_in: 10,
      total_out: 5,
      by_model: {},
      owner_budget: {
        spend: 1,
        limit: 10,
        used_pct: 10,
        status: 'ok',
        authorized: 4,
        reserved: 1,
        committed: 3,
        remaining_authorization: 6,
        reservation_unit: 1,
      },
    });

    render(<CostPanel runtimeName="budgeted-agent" />);

    expect(await screen.findByText('Authorized units: $4.00')).toBeInTheDocument();
    expect(screen.getByText('Reserved now: $1.00')).toBeInTheDocument();
    expect(screen.getByText('Committed units: $3.00')).toBeInTheDocument();
    expect(screen.getByText('Remaining: $6.00')).toBeInTheDocument();
  });
});
