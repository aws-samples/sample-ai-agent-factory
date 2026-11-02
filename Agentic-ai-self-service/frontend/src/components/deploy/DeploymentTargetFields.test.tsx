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

describe('DeploymentTargetFields release-Region envelope', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('never offers a target outside the seven selected Regions, even from an unfiltered catalog', async () => {
    mockAuthFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        enabled: true,
        home_region: 'us-east-1',
        regions: ['eu-west-1', 'ap-southeast-2', 'us-west-1'],
        accounts: [
          { account_id: '111111111111', region: 'us-west-2' },
          { account_id: '222222222222', region: 'ap-northeast-1' },
        ],
      }),
    });
    render(<DeploymentTargetFields value={{}} onChange={vi.fn()} />);
    const select = (await screen.findByLabelText('Live deployment target')) as HTMLSelectElement;
    const values = Array.from(select.options).map((option) => option.value);
    expect(values).toEqual(['default', 'region:eu-west-1', 'account:111111111111:us-west-2']);
    expect(values.join(' ')).not.toMatch(/ap-southeast-2|us-west-1|ap-northeast-1/);
  });

  it('resets a stale selection that names an unselected Region', async () => {
    mockAuthFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ enabled: true, home_region: 'us-east-1', regions: ['ap-southeast-2'], accounts: [] }),
    });
    const onChange = vi.fn();
    render(<DeploymentTargetFields value={{ targetRegion: 'ap-southeast-2' }} onChange={onChange} />);
    await waitFor(() => {
      expect(onChange).toHaveBeenCalledWith({});
    });
  });

  it('does not claim a refusal when the catalog names no home Region', async () => {
    mockAuthFetch.mockResolvedValue({ ok: true, json: async () => ({}) });
    render(<DeploymentTargetFields value={{}} onChange={vi.fn()} />);
    await waitFor(() => expect(mockAuthFetch).toHaveBeenCalledWith('/api/deploy-targets'));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows the refusal instead of offering an inadmissible platform home Region', async () => {
    mockAuthFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ enabled: false, home_region: 'us-west-1', regions: [], accounts: [] }),
    });
    render(<DeploymentTargetFields value={{}} onChange={vi.fn()} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Region us-west-1 is not a selected production release Region');
    expect(alert.textContent).toContain('US West (Oregon) (us-west-2)');
  });
});
