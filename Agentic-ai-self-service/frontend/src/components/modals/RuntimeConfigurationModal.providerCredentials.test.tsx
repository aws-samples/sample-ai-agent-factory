import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { authFetch } from '../../auth/authFetch';
import { RuntimeConfigurationModal } from './RuntimeConfigurationModal';

vi.mock('../../auth/authFetch', () => ({
  authFetch: vi.fn(),
}));

const mockedAuthFetch = vi.mocked(authFetch);
const STORED_ARN =
  'arn:aws:secretsmanager:us-east-1:111122223333:secret:agentcore-provider/openai/owner-deadbeef-AbCdEf';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function renderOpenAI(onSave = vi.fn()) {
  render(
    <RuntimeConfigurationModal
      isOpen
      onClose={vi.fn()}
      onSave={onSave}
      initialConfig={{
        name: 'provider-agent',
        systemPrompt: 'Help the user.',
        modelProvider: 'openai',
        model: {
          provider: 'openai',
          modelId: 'gpt-4o-mini',
          temperature: 0.7,
          topP: 0.9,
        },
      }}
    />,
  );
  return onSave;
}

describe('RuntimeConfigurationModal provider credentials', () => {
  beforeEach(() => {
    mockedAuthFetch.mockReset();
    mockedAuthFetch.mockImplementation(async (url) => {
      if (String(url).endsWith('/api/observability/platform-defaults')) {
        return jsonResponse({ enabled: false });
      }
      if (String(url).endsWith('/api/provider-credentials')) {
        return jsonResponse({ secret_arn: STORED_ARN });
      }
      throw new Error(`unexpected request: ${String(url)}`);
    });
  });

  it('stores a password field through the authenticated endpoint and saves only the ARN', async () => {
    const onSave = renderOpenAI();
    const keyInput = screen.getByLabelText('OpenAI API key') as HTMLInputElement;

    expect(keyInput.type).toBe('password');
    expect(screen.getByTestId('modal-save-button')).toBeDisabled();

    fireEvent.change(keyInput, { target: { value: 'sk-fake-browser-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'Store API key' }));

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Credential stored'));
    expect(keyInput).toHaveValue('');
    expect(screen.getByTestId('field-providerApiKeyRef')).toHaveValue(STORED_ARN);
    expect(screen.getByTestId('modal-save-button')).toBeEnabled();

    const storeCall = mockedAuthFetch.mock.calls.find(([url]) =>
      String(url).endsWith('/api/provider-credentials'),
    );
    expect(storeCall).toBeDefined();
    expect(storeCall?.[1]?.method).toBe('POST');
    expect(JSON.parse(String(storeCall?.[1]?.body))).toEqual({
      provider: 'openai',
      api_key: 'sk-fake-browser-key',
    });

    fireEvent.click(screen.getByTestId('modal-save-button'));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        providerApiKeyRef: STORED_ARN,
      }),
    );
    expect(JSON.stringify(onSave.mock.calls[0][0])).not.toContain('sk-fake-browser-key');
  });

  it('shows a normalized API error and does not invent an ARN', async () => {
    mockedAuthFetch.mockImplementation(async (url) => {
      if (String(url).endsWith('/api/observability/platform-defaults')) {
        return jsonResponse({ enabled: false });
      }
      return jsonResponse({ detail: 'Missing required scope(s): agent:write' }, 403);
    });
    renderOpenAI();

    fireEvent.change(screen.getByLabelText('OpenAI API key'), {
      target: { value: 'sk-fake-browser-key' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Store API key' }));

    await waitFor(() =>
      expect(
        screen.getByText('Missing required scope(s): agent:write'),
      ).toBeInTheDocument(),
    );
    expect(screen.getByTestId('field-providerApiKeyRef')).toHaveValue('');
    expect(screen.getByTestId('modal-save-button')).toBeDisabled();
  });
});
