import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { authFetch } from '../../auth/authFetch';
import { ObservabilityConfigurationModal } from './ObservabilityConfigurationModal';

vi.mock('../../auth/authFetch', () => ({
  authFetch: vi.fn(),
}));

const mockedAuthFetch = vi.mocked(authFetch);

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('ObservabilityConfigurationModal platform policy', () => {
  beforeEach(() => {
    mockedAuthFetch.mockReset();
  });

  it('does not expose editable OTEL controls or Save while the policy is loading', () => {
    mockedAuthFetch.mockImplementation(() => new Promise(() => {}));

    render(
      <ObservabilityConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    expect(
      screen.getByText('Checking platform observability policy…'),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('checkbox', { name: 'Enable OTLP telemetry' }),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('modal-save-button')).toBeDisabled();
    expect(
      screen.queryByText('Please fix the following errors:'),
    ).not.toBeInTheDocument();
  });

  it('fails closed on an unreadable policy and recovers only after an explicit retry', async () => {
    const onSave = vi.fn();
    mockedAuthFetch
      .mockResolvedValueOnce(
        jsonResponse(
          { detail: 'Platform observability settings are temporarily unavailable' },
          503,
        ),
      )
      .mockResolvedValueOnce(jsonResponse({ enabled: false }));

    render(
      <ObservabilityConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={onSave}
      />,
    );

    await waitFor(() =>
      expect(
        screen.getByText('Platform observability policy unavailable'),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole('checkbox', { name: 'Enable OTLP telemetry' }),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('modal-save-button')).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Retry policy check' }));

    await waitFor(() =>
      expect(
        screen.getByRole('checkbox', { name: 'Enable OTLP telemetry' }),
      ).toBeInTheDocument(),
    );
    expect(screen.getByTestId('modal-save-button')).toBeEnabled();

    fireEvent.click(screen.getByTestId('modal-save-button'));
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(mockedAuthFetch).toHaveBeenCalledTimes(2);
  });

  it('keeps platform-managed settings read-only after a successful policy read', async () => {
    mockedAuthFetch.mockResolvedValue(
      jsonResponse({
        enabled: true,
        endpoint: 'https://collector.example.test/v1/traces',
        sample_rate: 0.25,
        service_name_prefix: 'managed',
      }),
    );

    render(
      <ObservabilityConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    await waitFor(() =>
      expect(
        screen.getByText('Platform-managed observability'),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole('checkbox', { name: 'Enable OTLP telemetry' }),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText('OTLP Endpoint URL')).toBeDisabled();
    expect(screen.getByTestId('modal-save-button')).toBeEnabled();
  });

  it('fails closed when a successful response has an invalid policy shape', async () => {
    mockedAuthFetch.mockResolvedValue(jsonResponse({ endpoint: 'missing-enabled' }));

    render(
      <ObservabilityConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    await waitFor(() =>
      expect(
        screen.getByText('Platform observability policy unavailable'),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByText(
        'The platform returned an invalid observability policy response.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByTestId('modal-save-button')).toBeDisabled();
  });
});
