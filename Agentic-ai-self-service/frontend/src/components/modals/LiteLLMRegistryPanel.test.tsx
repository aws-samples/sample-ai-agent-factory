/**
 * LiteLLMRegistryPanel — Workstream B.
 *
 * The panel's whole job is keeping three states apart that a naive UI collapses:
 * not configured, configured-but-unverified (normal for a private LiteLLM), and
 * authoritative. Collapsing the middle one into an error sends an admin to
 * re-check a URL that was never wrong; collapsing it into success hides that the
 * control plane cannot see their proxy.
 *
 * It also must not activate as a side effect of testing a connection — flipping
 * the authoritative catalog changes what every developer sees and which catalog
 * gates deploys.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { LiteLLMRegistryPanel } from './LiteLLMRegistryPanel';
import * as api from '../../services/api';

const NOT_CONFIGURED = { provider: 'dynamodb', configured: false, verified: false };

const CONNECTED_UNVERIFIED = {
  provider: 'dynamodb',
  configured: true,
  base_url: 'https://litellm.internal.example.com',
  api_key_ref: 'arn:aws:secretsmanager:eu-central-1:1:secret:agentcore-registry/litellm/abc-1',
  verified: false,
};

const ACTIVE = {
  ...CONNECTED_UNVERIFIED,
  provider: 'litellm',
  verified: true,
  capabilities: {
    provider: 'litellm',
    authoritative_catalog: 'LiteLLM MCP catalog',
    supports_publish: true,
    supports_update: true,
    supports_delete: true,
    supports_review: true,
    supports_clone: true,
    read_only_sources: ['litellm'],
    notes: 'LiteLLM is the authoritative catalog and the approval source of truth.',
  },
};

const SERVERS = {
  configured: true,
  servers: [
    { name: 'GitHub MCP', slug: 'github-mcp', enabled: true },
    { name: 'retired', slug: 'retired', enabled: false },
  ],
};

function mockClient(overrides: Record<string, unknown> = {}) {
  const client = {
    getLiteLLMRegistryConfig: vi.fn().mockResolvedValue(NOT_CONFIGURED),
    listLiteLLMServers: vi.fn().mockResolvedValue({ configured: false, servers: [] }),
    enableLiteLLMRegistry: vi.fn().mockResolvedValue(CONNECTED_UNVERIFIED),
    disableLiteLLMRegistry: vi.fn().mockResolvedValue({ provider: 'dynamodb', configured: false, verified: false }),
    ...overrides,
  };
  vi.spyOn(api, 'getApiClient').mockReturnValue(client as never);
  return client;
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => vi.restoreAllMocks());

describe('LiteLLMRegistryPanel', () => {
  it('starts unconfigured and does not claim a connection', async () => {
    mockClient();
    render(<LiteLLMRegistryPanel />);
    expect(await screen.findByText('Not configured')).toBeTruthy();
  });

  it('an API that is unavailable leaves the panel disabled rather than erroring', async () => {
    mockClient({ getLiteLLMRegistryConfig: vi.fn().mockRejectedValue(new Error('404')) });
    render(<LiteLLMRegistryPanel />);
    expect(await screen.findByText('Not configured')).toBeTruthy();
    expect(screen.queryByText(/404/)).toBeNull();
  });

  it('"Connect & test" does NOT make LiteLLM authoritative', async () => {
    const client = mockClient();
    render(<LiteLLMRegistryPanel />);
    await screen.findByText('Not configured');

    fireEvent.change(screen.getByPlaceholderText('https://litellm.example.com'), {
      target: { value: 'https://litellm.example.com' },
    });
    fireEvent.change(screen.getByPlaceholderText(/virtual key/i), { target: { value: 'sk-abc' } });
    fireEvent.click(screen.getByText('Connect & test'));

    await waitFor(() => expect(client.enableLiteLLMRegistry).toHaveBeenCalled());
    expect(client.enableLiteLLMRegistry).toHaveBeenCalledWith(
      expect.objectContaining({ base_url: 'https://litellm.example.com', activate: false }),
    );
  });

  it('the separate activate button is the only thing that sets activate: true', async () => {
    const client = mockClient();
    render(<LiteLLMRegistryPanel />);
    await screen.findByText('Not configured');

    fireEvent.change(screen.getByPlaceholderText('https://litellm.example.com'), {
      target: { value: 'https://litellm.example.com' },
    });
    fireEvent.change(screen.getByPlaceholderText(/virtual key/i), { target: { value: 'sk-abc' } });
    fireEvent.click(screen.getByText('Connect & make authoritative'));

    await waitFor(() => expect(client.enableLiteLLMRegistry).toHaveBeenCalled());
    expect(client.enableLiteLLMRegistry).toHaveBeenCalledWith(expect.objectContaining({ activate: true }));
  });

  it('clears the virtual key from the DOM once it has been minted', async () => {
    mockClient();
    render(<LiteLLMRegistryPanel />);
    await screen.findByText('Not configured');

    fireEvent.change(screen.getByPlaceholderText('https://litellm.example.com'), {
      target: { value: 'https://litellm.example.com' },
    });
    const key = screen.getByPlaceholderText(/virtual key/i) as HTMLInputElement;
    fireEvent.change(key, { target: { value: 'sk-secret-value' } });
    expect(key.type).toBe('password');

    fireEvent.click(screen.getByText('Connect & test'));
    await waitFor(() => expect(document.body.innerHTML).not.toContain('sk-secret-value'));
  });

  it('an unverified connection reads as "reachable from here", not as broken', async () => {
    mockClient({
      getLiteLLMRegistryConfig: vi.fn().mockResolvedValue(CONNECTED_UNVERIFIED),
      listLiteLLMServers: vi.fn().mockResolvedValue(SERVERS),
    });
    render(<LiteLLMRegistryPanel />);

    expect(await screen.findByText(/platform catalog still authoritative/)).toBeTruthy();
    expect(screen.getByText(/only listens inside your VPC this is expected/)).toBeTruthy();
    // Crucially: it says the credential was ACCEPTED, so nobody goes hunting for a
    // bad key. (The banner does mention rejection — to say it did not happen here.)
    expect(screen.getByText(/The virtual key itself was accepted/)).toBeTruthy();
  });

  it('names disabled servers as the reason deploys get blocked', async () => {
    mockClient({
      getLiteLLMRegistryConfig: vi.fn().mockResolvedValue(ACTIVE),
      listLiteLLMServers: vi.fn().mockResolvedValue(SERVERS),
    });
    render(<LiteLLMRegistryPanel />);

    expect(await screen.findByText('Authoritative')).toBeTruthy();
    const counts = screen.getByTestId('litellm-server-counts').textContent ?? '';
    expect(counts).toContain('1 enabled MCP server');
    expect(counts).toContain('1 disabled (deploys wiring these are blocked)');
  });

  it('shows the backend’s own capability notes rather than a restated copy', async () => {
    mockClient({
      getLiteLLMRegistryConfig: vi.fn().mockResolvedValue(ACTIVE),
      listLiteLLMServers: vi.fn().mockResolvedValue(SERVERS),
    });
    render(<LiteLLMRegistryPanel />);
    expect(await screen.findByText(ACTIVE.capabilities.notes)).toBeTruthy();
  });

  it('never renders the api_key_ref as if it were the key itself', async () => {
    mockClient({
      getLiteLLMRegistryConfig: vi.fn().mockResolvedValue(ACTIVE),
      listLiteLLMServers: vi.fn().mockResolvedValue(SERVERS),
    });
    render(<LiteLLMRegistryPanel />);
    await screen.findByText('Authoritative');
    expect(screen.queryByPlaceholderText(/virtual key/i)).toBeNull();
  });

  it('reverting surfaces the key that was deliberately left behind', async () => {
    const client = mockClient({
      getLiteLLMRegistryConfig: vi.fn().mockResolvedValue(ACTIVE),
      listLiteLLMServers: vi.fn().mockResolvedValue(SERVERS),
      disableLiteLLMRegistry: vi.fn().mockResolvedValue({
        provider: 'dynamodb',
        configured: false,
        verified: false,
        orphaned_api_key_ref: 'arn:aws:secretsmanager:eu-central-1:1:secret:agentcore-registry/litellm/abc-1',
      }),
    });
    render(<LiteLLMRegistryPanel />);
    await screen.findByText('Authoritative');

    fireEvent.click(screen.getByText('Disconnect & use platform catalog'));
    await waitFor(() => expect(client.disableLiteLLMRegistry).toHaveBeenCalled());
    expect(await screen.findByText(/it was not deleted/)).toBeTruthy();
  });

  it('a failed save surfaces the reason instead of silently doing nothing', async () => {
    mockClient({
      enableLiteLLMRegistry: vi.fn().mockRejectedValue(new Error('LiteLLM registry base URL rejected — private address')),
    });
    render(<LiteLLMRegistryPanel />);
    await screen.findByText('Not configured');

    fireEvent.change(screen.getByPlaceholderText('https://litellm.example.com'), {
      target: { value: 'http://10.0.0.5' },
    });
    fireEvent.change(screen.getByPlaceholderText(/virtual key/i), { target: { value: 'sk-abc' } });
    fireEvent.click(screen.getByText('Connect & test'));

    expect(await screen.findByText(/base URL rejected/)).toBeTruthy();
  });

  it('a server-listing failure does not hide the connection itself', async () => {
    mockClient({
      getLiteLLMRegistryConfig: vi.fn().mockResolvedValue(CONNECTED_UNVERIFIED),
      listLiteLLMServers: vi.fn().mockRejectedValue(new Error('503')),
    });
    render(<LiteLLMRegistryPanel />);
    expect(await screen.findByText(/platform catalog still authoritative/)).toBeTruthy();
    expect(screen.queryByText(/503/)).toBeNull();
  });
});
