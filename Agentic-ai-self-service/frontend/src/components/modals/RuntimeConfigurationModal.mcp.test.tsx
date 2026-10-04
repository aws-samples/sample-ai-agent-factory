import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { authFetch } from '../../auth/authFetch';
import { RuntimeConfigurationModal } from './RuntimeConfigurationModal';

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

describe('RuntimeConfigurationModal MCP tool-server contract', () => {
  beforeEach(() => {
    mockedAuthFetch.mockReset();
    mockedAuthFetch.mockResolvedValue(jsonResponse({ enabled: false }));
  });

  it('shows only runtime settings and does not require model-only fields', async () => {
    const onSave = vi.fn();

    render(
      <RuntimeConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={onSave}
        initialConfig={{
          name: 'standalone_mcp',
          protocol: 'MCP',
          systemPrompt: '',
        }}
      />,
    );

    expect(screen.getByTestId('modal-save-button')).toBeDisabled();
    await waitFor(() =>
      expect(screen.getByTestId('modal-save-button')).toBeEnabled(),
    );
    expect(
      screen.getByText('Model-free MCP tool server'),
    ).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'General' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Advanced' })).toBeInTheDocument();
    expect(
      screen.queryByRole('tab', { name: 'Provider' }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('tab', { name: 'System Prompt' }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('tab', { name: 'Model' }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('tab', { name: 'Multi-Agent' }),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText('Server Protocol')).toBeDisabled();
    fireEvent.click(screen.getByTestId('modal-save-button'));

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        protocol: 'MCP',
        enableOtel: false,
      }),
    );
    for (const field of [
      'framework',
      'model',
      'modelProvider',
      'providerApiKeyRef',
      'providerBaseUrl',
      'systemPrompt',
      'multiAgentPattern',
      'multiAgentConfig',
      'observability',
    ]) {
      expect(onSave.mock.calls[0][0]).not.toHaveProperty(field);
    }
  });

  it('fails closed when the admin OTEL policy is unreadable and recovers only after retry', async () => {
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
      <RuntimeConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={onSave}
        initialConfig={{
          name: 'standalone_mcp',
          protocol: 'MCP',
          systemPrompt: '',
        }}
      />,
    );

    await waitFor(() =>
      expect(
        screen.getByText('Platform observability policy unavailable'),
      ).toBeInTheDocument(),
    );
    expect(screen.getByTestId('modal-save-button')).toBeDisabled();
    expect(
      screen.queryByText('Please fix the following errors:'),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'Advanced' }));
    expect(
      screen.getByText('Platform observability policy unavailable'),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry policy check' }));

    await waitFor(() =>
      expect(screen.getByTestId('modal-save-button')).toBeEnabled(),
    );
    expect(
      screen.queryByText('Platform observability policy unavailable'),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('modal-save-button'));
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(mockedAuthFetch).toHaveBeenCalledTimes(2);
  });

  it('does not offer an unsafe MCP protocol for an HTTP agent template', () => {
    render(
      <RuntimeConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
        initialConfig={{
          name: 'http_agent',
          protocol: 'HTTP',
          systemPrompt: 'Use the model.',
        }}
      />,
    );

    fireEvent.click(screen.getByRole('tab', { name: 'General' }));
    const protocol = screen.getByLabelText('Server Protocol');

    expect(protocol).toBeEnabled();
    expect(
      screen.queryByRole('option', { name: 'MCP - Model Context Protocol' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('option', { name: 'HTTP - Standard REST API' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('option', { name: 'A2A - Agent-to-Agent' }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'MCP is available through the dedicated MCP runtime templates, which generate a real MCP server.',
      ),
    ).toBeInTheDocument();
  });
});
