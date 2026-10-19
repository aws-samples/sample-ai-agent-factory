import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as api from '../../services/api';
import { AwsRegistryPanel } from './AwsRegistryPanel';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe('AwsRegistryPanel regional availability', () => {
  it('makes no API call and names the DynamoDB fallback in Frankfurt', () => {
    vi.stubEnv('VITE_AWS_REGION', 'eu-central-1');
    const getClient = vi.spyOn(api, 'getApiClient');

    render(<AwsRegistryPanel />);

    expect(screen.getByText('Unavailable in this Region')).toBeTruthy();
    expect(screen.getByRole('status')).toHaveTextContent('Europe (Frankfurt)');
    expect(screen.getByRole('status')).toHaveTextContent(
      'built-in DynamoDB agent registry remains available',
    );
    expect(screen.getByLabelText('AWS Agent Registry ID')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Enable' })).toBeDisabled();
    expect(getClient).not.toHaveBeenCalled();
  });

  it('loads the opt-in configuration normally in Ireland', async () => {
    vi.stubEnv('VITE_AWS_REGION', 'eu-west-1');
    const getAwsRegistryConfig = vi.fn().mockResolvedValue({
      enabled: false,
      registry_id: null,
      available: false,
      sdk_supported: true,
      status: null,
    });
    vi.spyOn(api, 'getApiClient').mockReturnValue({ getAwsRegistryConfig } as never);

    render(<AwsRegistryPanel />);

    await waitFor(() => expect(getAwsRegistryConfig).toHaveBeenCalledTimes(1));
    expect(screen.getByText('Not configured')).toBeTruthy();
    expect(screen.getByLabelText('AWS Agent Registry ID')).toBeEnabled();
    expect(screen.queryByRole('status')).toBeNull();
  });
});
