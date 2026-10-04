import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { TriggersPanel } from './TriggersPanel';

const listTriggers = vi.fn();
const createTrigger = vi.fn();
const deleteTrigger = vi.fn();

vi.mock('../../services/api', () => ({
  API_BASE_URL: 'https://agents.example.com',
  getApiClient: () => ({
    listTriggers,
    createTrigger,
    deleteTrigger,
  }),
  getErrorMessage: (error: unknown) => {
    if (
      typeof error === 'object' &&
      error !== null &&
      'message' in error &&
      typeof error.message === 'string'
    ) {
      return error.message;
    }
    return String(error);
  },
  isNotReadyError: (error: unknown) =>
    [403, 404].includes((error as { status?: number })?.status ?? 0),
}));

function trigger(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    runtime_name: 'orders_agent',
    trigger_id: 'trigger-1',
    type: 'cron',
    status: 'active',
    target_runtime_arn:
      'arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/orders-v1',
    schedule: 'cron(0 9 * * ? *)',
    pattern: null,
    webhook_out_url: null,
    webhook_path: null,
    webhook_signing_secret: null,
    last_error_code: null,
    created_at: Date.UTC(2026, 8, 24, 8, 30),
    updated_at: Date.UTC(2026, 8, 24, 8, 30),
    ...overrides,
  };
}

describe('TriggersPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listTriggers.mockResolvedValue([]);
    createTrigger.mockResolvedValue(trigger());
    deleteTrigger.mockResolvedValue({
      success: true,
      trigger_id: 'trigger-1',
      message: 'deleted',
    });
  });

  it('describes live, version-pinned triggers without the stale preview claim', async () => {
    render(<TriggersPanel runtimeName="orders_agent" />);

    expect(screen.queryByText('Preview')).not.toBeInTheDocument();
    expect(
      screen.getByText(/Create live cron, EventBridge, S3/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/exact deployed runtime version/i)).toBeInTheDocument();
    expect(screen.getByText(/does not silently retarget/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(listTriggers).toHaveBeenCalledWith('orders_agent'),
    );
  });

  it('requires a non-empty cron expression before calling the API', async () => {
    render(<TriggersPanel runtimeName="orders_agent" />);
    await waitFor(() => expect(listTriggers).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: 'Add trigger' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'A cron schedule is required.',
    );
    expect(createTrigger).not.toHaveBeenCalled();
  });

  it('submits a parsed EventBridge pattern and optional result callback', async () => {
    render(<TriggersPanel runtimeName="orders_agent" />);
    await waitFor(() => expect(listTriggers).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText('Type'), {
      target: { value: 'eventbridge' },
    });
    fireEvent.change(screen.getByLabelText('Event pattern (JSON)'), {
      target: {
        value: '{"source":["my.application"],"detail":{"state":["ready"]}}',
      },
    });
    fireEvent.change(screen.getByLabelText('Result callback URL (optional)'), {
      target: { value: 'https://hooks.example.com/result' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add trigger' }));

    await waitFor(() =>
      expect(createTrigger).toHaveBeenCalledWith('orders_agent', {
        type: 'eventbridge',
        pattern: {
          source: ['my.application'],
          detail: { state: ['ready'] },
        },
        webhook_out_url: 'https://hooks.example.com/result',
      }),
    );
  });

  it('rejects malformed and non-object event patterns locally', async () => {
    render(<TriggersPanel runtimeName="orders_agent" />);
    await waitFor(() => expect(listTriggers).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText('Type'), {
      target: { value: 'eventbridge' },
    });
    fireEvent.change(screen.getByLabelText('Event pattern (JSON)'), {
      target: { value: '["not","an","object"]' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add trigger' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Event pattern must be a JSON object.',
    );
    expect(createTrigger).not.toHaveBeenCalled();
  });

  it('explains the S3 prerequisite and enforces the exact S3 source', async () => {
    render(<TriggersPanel runtimeName="orders_agent" />);
    await waitFor(() => expect(listTriggers).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText('Type'), {
      target: { value: 's3' },
    });
    expect(
      screen.getByText(/Enable Amazon EventBridge notifications/i),
    ).toBeInTheDocument();

    const pattern = screen.getByLabelText('Event pattern (JSON)');
    fireEvent.change(pattern, {
      target: { value: '{"source":["aws.s3","custom.source"]}' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add trigger' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'must contain exactly "source": ["aws.s3"]',
    );
    expect(createTrigger).not.toHaveBeenCalled();

    fireEvent.change(pattern, {
      target: {
        value:
          '{"source":["aws.s3"],"detail":{"bucket":{"name":["orders"]}}}',
      },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add trigger' }));

    await waitFor(() =>
      expect(createTrigger).toHaveBeenCalledWith('orders_agent', {
        type: 's3',
        pattern: {
          source: ['aws.s3'],
          detail: { bucket: { name: ['orders'] } },
        },
      }),
    );
  });

  it('shows webhook endpoint and signing secret once, across list refreshes, until dismissed', async () => {
    const secret = 'a'.repeat(64);
    createTrigger.mockResolvedValue(
      trigger({
        trigger_id: 'webhook-1',
        type: 'webhook',
        schedule: null,
        webhook_path: '/hooks/orders_agent/webhook-1',
        webhook_signing_secret: secret,
      }),
    );
    listTriggers
      .mockResolvedValueOnce([])
      .mockResolvedValue([
        trigger({
          trigger_id: 'webhook-1',
          type: 'webhook',
          schedule: null,
          webhook_path: '/hooks/orders_agent/webhook-1',
          webhook_signing_secret: null,
        }),
      ]);

    render(<TriggersPanel runtimeName="orders_agent" />);
    await waitFor(() => expect(listTriggers).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText('Type'), {
      target: { value: 'webhook' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add trigger' }));

    const credentials = await screen.findByLabelText(
      'One-time webhook credentials',
    );
    expect(credentials).toHaveTextContent(
      'https://agents.example.com/hooks/orders_agent/webhook-1',
    );
    expect(credentials).toHaveTextContent(secret);
    expect(credentials).toHaveTextContent(/shown once/i);
    await waitFor(() => expect(listTriggers).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(listTriggers).toHaveBeenCalledTimes(3));
    expect(
      screen.getByLabelText('One-time webhook credentials'),
    ).toHaveTextContent(secret);

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(
      screen.queryByLabelText('One-time webhook credentials'),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(secret)).not.toBeInTheDocument();
  });

  it('never renders an internal webhook secret ARN from a list response', async () => {
    const internalArn =
      'arn:aws:secretsmanager:us-east-1:111122223333:secret:agentcore-trigger/private';
    listTriggers.mockResolvedValue([
      trigger({
        type: 'webhook',
        schedule: null,
        webhook_path: '/hooks/orders_agent/trigger-1',
        // Deliberately simulate an older backend payload. The UI must ignore it.
        webhook_secret_ref: internalArn,
      }),
    ]);

    render(<TriggersPanel runtimeName="orders_agent" />);
    await waitFor(() => expect(listTriggers).toHaveBeenCalled());

    expect(
      screen.getByText(
        'https://agents.example.com/hooks/orders_agent/trigger-1',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(internalArn)).not.toBeInTheDocument();
  });

  it('renders provisioning errors, legacy status guidance, and retryable deletion', async () => {
    listTriggers.mockResolvedValue([
      trigger({
        trigger_id: 'broken-trigger',
        status: 'error',
        last_error_code: 'EVENTBRIDGE_PUT_RULE_FAILED',
      }),
      trigger({
        trigger_id: 'legacy-trigger',
        status: 'registered',
      }),
    ]);
    deleteTrigger.mockRejectedValue({
      status: 409,
      message: 'A trigger delivery is still in progress; retry shortly',
    });

    render(<TriggersPanel runtimeName="orders_agent" />);
    await waitFor(() => expect(listTriggers).toHaveBeenCalled());

    expect(screen.getByText('EVENTBRIDGE_PUT_RULE_FAILED')).toBeInTheDocument();
    expect(
      screen.getByText(/legacy definition has no provisioned AWS resource/i),
    ).toBeInTheDocument();
    const retryButtons = screen.getAllByRole('button', {
      name: 'Retry delete',
    });
    fireEvent.click(retryButtons[0]);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'A trigger delivery is still in progress',
    );
    expect(deleteTrigger).toHaveBeenCalledWith(
      'orders_agent',
      'broken-trigger',
    );
  });

  it('prevents duplicate create requests while provisioning is in flight', async () => {
    let resolveCreate: ((value: Record<string, unknown>) => void) | undefined;
    createTrigger.mockReturnValue(
      new Promise((resolve) => {
        resolveCreate = resolve;
      }),
    );
    render(<TriggersPanel runtimeName="orders_agent" />);
    await waitFor(() => expect(listTriggers).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText('Schedule'), {
      target: { value: 'cron(0 9 * * ? *)' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add trigger' }));

    const creating = screen.getByRole('button', { name: 'Creating…' });
    expect(creating).toBeDisabled();
    fireEvent.click(creating);
    expect(createTrigger).toHaveBeenCalledTimes(1);

    resolveCreate?.(trigger());
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Add trigger' })).toBeEnabled(),
    );
  });

  it('does not let a stale runtime list response overwrite the current runtime', async () => {
    let resolveOld:
      | ((value: Array<Record<string, unknown>>) => void)
      | undefined;
    listTriggers
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
      )
      .mockResolvedValueOnce([
        trigger({
          runtime_name: 'new_agent',
          trigger_id: 'new-trigger',
          schedule: 'cron(0 12 * * ? *)',
        }),
      ]);

    const { rerender } = render(
      <TriggersPanel runtimeName="old_agent" refreshKey={0} />,
    );
    await waitFor(() =>
      expect(listTriggers).toHaveBeenCalledWith('old_agent'),
    );

    rerender(<TriggersPanel runtimeName="new_agent" refreshKey={0} />);
    expect(
      await screen.findByText('cron(0 12 * * ? *)'),
    ).toBeInTheDocument();

    resolveOld?.([
      trigger({
        runtime_name: 'old_agent',
        trigger_id: 'old-trigger',
        schedule: 'cron(0 1 * * ? *)',
      }),
    ]);
    await waitFor(() =>
      expect(screen.queryByText('cron(0 1 * * ? *)')).not.toBeInTheDocument(),
    );
    expect(screen.getByText('cron(0 12 * * ? *)')).toBeInTheDocument();
  });

  it('treats an undeployed runtime as empty but surfaces a dead session', async () => {
    listTriggers.mockRejectedValueOnce({ status: 404, message: 'Not found' });
    const { rerender } = render(
      <TriggersPanel runtimeName="orders_agent" refreshKey={0} />,
    );
    expect(await screen.findByText('No triggers yet.')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    listTriggers.mockRejectedValueOnce({
      status: 401,
      message: 'Your session has expired.',
    });
    rerender(<TriggersPanel runtimeName="orders_agent" refreshKey={1} />);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Your session has expired.',
    );
  });
});
