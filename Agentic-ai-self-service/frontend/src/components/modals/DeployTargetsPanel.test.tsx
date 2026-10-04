import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DeployTargetsPanel } from './DeployTargetsPanel';

const { api } = vi.hoisted(() => ({
  api: {
    getDeployTargets: vi.fn(),
    enableDeployTargets: vi.fn(),
    addDeployRegion: vi.fn(),
    addDeployAccount: vi.fn(),
  },
}));

vi.mock('../../services/api', () => ({
  getApiClient: () => api,
  getErrorMessage: (error: unknown) =>
    error instanceof Error ? error.message : String(error),
}));

const account = {
  account_id: '123456789012',
  role_arn:
    'arn:aws:iam::123456789012:role/AgentCoreFlowsDeploymentRole',
  runtime_role_arn:
    'arn:aws:iam::123456789012:role/AgentCoreFlowsRuntimeRole',
  mcp_runtime_role_arn:
    'arn:aws:iam::123456789012:role/AgentCoreFlowsMCPRuntimeRole',
  harness_role_arn:
    'arn:aws:iam::123456789012:role/AgentCoreFlowsHarnessRole',
  artifact_bucket: 'ecb-agent-runtime-artifacts',
  region: 'eu-west-1',
};

const regionTarget = {
  account_id: '123456789012',
  artifact_bucket: 'agentcore-flows-artifacts-123456789012-eu-west-1',
  region: 'eu-west-1',
};

describe('DeployTargetsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getDeployTargets.mockResolvedValue({
      enabled: true,
      regions: ['eu-west-1'],
      region_targets: [regionTarget],
      accounts: [account],
    });
    api.addDeployRegion.mockResolvedValue({
      ...regionTarget,
      validated: true,
      regions: ['eu-west-1'],
    });
    api.addDeployAccount.mockResolvedValue({
      account_id: account.account_id,
      runtime_role_arn: account.runtime_role_arn,
      mcp_runtime_role_arn: account.mcp_runtime_role_arn,
      harness_role_arn: account.harness_role_arn,
      artifact_bucket: account.artifact_bucket,
      validated: true,
    });
  });

  it('shows the exact validated artifact bucket and fixed deployment-role contract', async () => {
    render(<DeployTargetsPanel />);

    expect(
      await screen.findByText(`Artifacts: s3://${account.artifact_bucket}`),
    ).toBeInTheDocument();
    expect(screen.getByText('AgentCoreFlowsDeploymentRole')).toBeInTheDocument();
    expect(
      screen.getByText(
        (_text, element) =>
          element?.textContent === `Runtime: ${account.runtime_role_arn}`,
        ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        (_text, element) =>
          element?.textContent ===
          `MCP runtime: ${account.mcp_runtime_role_arn}`,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        (_text, element) =>
          element?.textContent === `Harness: ${account.harness_role_arn}`,
      ),
    ).toBeInTheDocument();
  });

  it('submits all four target prerequisites and clears the form after validation', async () => {
    render(<DeployTargetsPanel />);
    await screen.findByText(`Artifacts: s3://${account.artifact_bucket}`);

    fireEvent.change(screen.getByLabelText('Target AWS account ID'), {
      target: { value: account.account_id },
    });
    fireEvent.change(screen.getByLabelText('Target deployment role ARN'), {
      target: { value: account.role_arn },
    });
    fireEvent.change(screen.getByLabelText('Target account region'), {
      target: { value: account.region },
    });
    fireEvent.change(
      screen.getByLabelText('Target AgentCore runtime execution role ARN'),
      { target: { value: account.runtime_role_arn } },
    );
    fireEvent.change(
      screen.getByLabelText(
        'Target AgentCore MCP runtime execution role ARN',
      ),
      { target: { value: account.mcp_runtime_role_arn } },
    );
    fireEvent.change(
      screen.getByLabelText('Target AgentCore harness execution role ARN'),
      { target: { value: account.harness_role_arn } },
    );
    fireEvent.change(
      screen.getByLabelText('Target runtime artifact bucket name'),
      { target: { value: account.artifact_bucket } },
    );

    fireEvent.click(
      screen.getByRole('button', { name: 'Register & validate account' }),
    );

    await waitFor(() => {
      expect(api.addDeployAccount).toHaveBeenCalledWith(
        account.account_id,
        account.role_arn,
        account.region,
        account.runtime_role_arn,
        account.mcp_runtime_role_arn,
        account.harness_role_arn,
        account.artifact_bucket,
      );
    });
    await waitFor(() => {
      expect(
        (screen.getByLabelText('Target AWS account ID') as HTMLInputElement)
          .value,
      ).toBe('');
      expect(
        (
          screen.getByLabelText(
            'Target runtime artifact bucket name',
          ) as HTMLInputElement
        ).value,
      ).toBe('');
    });
  });

  it('validates and displays the regional runtime artifact bucket', async () => {
    render(<DeployTargetsPanel />);

    expect(
      await screen.findByText(
        `Regional artifacts: s3://${regionTarget.artifact_bucket}`,
      ),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Region to allow'), {
      target: { value: 'us-west-2' },
    });
    fireEvent.change(
      screen.getByLabelText('Regional runtime artifact bucket name'),
      {
        target: {
          value: 'agentcore-flows-artifacts-123456789012-us-west-2',
        },
      },
    );
    fireEvent.click(
      screen.getByRole('button', { name: 'Register & validate region' }),
    );

    await waitFor(() => {
      expect(api.addDeployRegion).toHaveBeenCalledWith(
        'us-west-2',
        'agentcore-flows-artifacts-123456789012-us-west-2',
      );
    });
    await waitFor(() => {
      expect(
        (screen.getByLabelText('Region to allow') as HTMLInputElement).value,
      ).toBe('');
      expect(
        (
          screen.getByLabelText(
            'Regional runtime artifact bucket name',
          ) as HTMLInputElement
        ).value,
      ).toBe('');
    });
  });

  it('blocks a regional bucket outside the platform namespace', async () => {
    render(<DeployTargetsPanel />);
    await screen.findByText(
      `Regional artifacts: s3://${regionTarget.artifact_bucket}`,
    );

    fireEvent.change(screen.getByLabelText('Region to allow'), {
      target: { value: 'us-west-2' },
    });
    fireEvent.change(
      screen.getByLabelText('Regional runtime artifact bucket name'),
      { target: { value: 'platform-artifacts-us-west-2' } },
    );

    expect(
      screen.getByRole('button', { name: 'Register & validate region' }),
    ).toBeDisabled();
    expect(
      screen.getByRole('alert'),
    ).toHaveTextContent(
      'agentcore-flows-artifacts-<12-digit platform account>-<suffix>',
    );
    expect(api.addDeployRegion).not.toHaveBeenCalled();
  });

  it('uses the recommended conventional bucket when the override is blank', async () => {
    render(<DeployTargetsPanel />);
    await screen.findByText(
      `Regional artifacts: s3://${regionTarget.artifact_bucket}`,
    );

    fireEvent.change(screen.getByLabelText('Region to allow'), {
      target: { value: 'us-west-2' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Register & validate region' }),
    );

    await waitFor(() => {
      expect(api.addDeployRegion).toHaveBeenCalledWith('us-west-2', undefined);
    });
  });
});
