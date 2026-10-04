import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DeploymentTargetFields } from './DeploymentTargetFields';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

const catalog = {
  enabled: true,
  home_region: 'us-east-1',
  regions: ['eu-west-1'],
  accounts: [
    {
      account_id: '123456789012',
      region: 'eu-west-1',
    },
  ],
};

describe('DeploymentTargetFields', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockAuthFetch.mockResolvedValue({
      ok: true,
      json: async () => catalog,
    });
  });

  it('loads only the sanitized deployer catalog and selects an account target', async () => {
    const onChange = vi.fn();
    render(<DeploymentTargetFields value={{}} onChange={onChange} />);

    const select = await screen.findByLabelText('Live deployment target');
    expect(mockAuthFetch).toHaveBeenCalledWith('/api/deploy-targets');
    expect(screen.queryByText(/AgentCoreFlowsDeploymentRole/)).not.toBeInTheDocument();

    fireEvent.change(select, {
      target: { value: 'account:123456789012:eu-west-1' },
    });

    expect(onChange).toHaveBeenLastCalledWith({
      targetAccountId: '123456789012',
      targetRegion: 'eu-west-1',
    });
  });

  it('supports a region-only deployment in the platform account', async () => {
    const onChange = vi.fn();
    render(<DeploymentTargetFields value={{}} onChange={onChange} />);

    fireEvent.change(await screen.findByLabelText('Live deployment target'), {
      target: { value: 'region:eu-west-1' },
    });

    expect(onChange).toHaveBeenLastCalledWith({
      targetRegion: 'eu-west-1',
    });
  });

  it('resets a stale target that is no longer in the allowlist', async () => {
    const onChange = vi.fn();
    render(
      <DeploymentTargetFields
        value={{
          targetAccountId: '999999999999',
          targetRegion: 'us-west-2',
        }}
        onChange={onChange}
      />,
    );

    await waitFor(() => {
      expect(onChange).toHaveBeenCalledWith({});
    });
  });

  it('keeps platform-default deployment available when the catalog is disabled', async () => {
    mockAuthFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        enabled: false,
        home_region: 'us-east-1',
        regions: [],
        accounts: [],
      }),
    });

    const onChange = vi.fn();
    const { container } = render(
      <DeploymentTargetFields value={{}} onChange={onChange} />,
    );

    await waitFor(() => expect(mockAuthFetch).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    expect(onChange).not.toHaveBeenCalled();
  });
});
