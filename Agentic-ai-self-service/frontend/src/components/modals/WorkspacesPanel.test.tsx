import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { WorkspacesPanel } from './WorkspacesPanel';
import { applyPreset, emptyPolicy } from './workspacePresets';
import type { FeatureFlagsState } from '../../hooks/useFeatureFlags';

const { api, flags } = vi.hoisted(() => ({
  api: {
    listWorkspacePolicies: vi.fn(),
    putWorkspacePolicy: vi.fn(),
    deleteWorkspacePolicy: vi.fn(),
  },
  flags: { state: { status: 'loading' } as { status: string } },
}));

vi.mock('../../services/api', () => ({
  getApiClient: () => api,
  getErrorMessage: (error: unknown) => {
    if (error instanceof Error) return error.message;
    if (typeof error === 'object' && error !== null && 'message' in error) {
      return String((error as { message: unknown }).message);
    }
    return String(error);
  },
}));

vi.mock('../../hooks/useFeatureFlags', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../hooks/useFeatureFlags')>()),
  useFeatureFlags: () => ({ state: flags.state as FeatureFlagsState, enabled: () => false, refresh: () => {} }),
}));

function ready(workspacePolicy: boolean): FeatureFlagsState {
  return {
    status: 'ready',
    flags: {
      features: { platformFlagsCanary: false, specV2Read: false, workspacePolicy },
      source: 'env',
      parameter: '',
      ignored: [],
      ttlSeconds: 0,
      refreshedAt: '',
      descriptions: {},
    },
  };
}

const finance = applyPreset('standard', {
  workspaceId: 'finance',
  name: 'Finance',
  memberGroups: ['g-finance-builders'],
  adminGroups: [],
  approverGroups: ['g-approvers'],
  securityGroups: ['g-security'],
});
const defaultPolicy = emptyPolicy('default', 'Default');

describe('WorkspacesPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    flags.state = ready(true);
    api.listWorkspacePolicies.mockResolvedValue([defaultPolicy, finance]);
    api.putWorkspacePolicy.mockImplementation(async (document: unknown) => document);
    api.deleteWorkspacePolicy.mockResolvedValue(undefined);
  });

  it('renders nothing for a non-admin and asks the API for nothing', () => {
    const { container } = render(<WorkspacesPanel isAdmin={false} />);
    expect(container).toBeEmptyDOMElement();
    expect(api.listWorkspacePolicies).not.toHaveBeenCalled();
  });

  it('renders nothing while the workspacePolicy flag is off, because its routes would 404', () => {
    flags.state = ready(false);
    const { container } = render(<WorkspacesPanel isAdmin />);
    expect(container).toBeEmptyDOMElement();
    expect(api.listWorkspacePolicies).not.toHaveBeenCalled();
  });

  it('lists every policy with who it governs, what it allows and who approves', async () => {
    render(<WorkspacesPanel isAdmin />);
    const card = await screen.findByTestId('workspace-finance');
    expect(card).toHaveTextContent('Finance');
    expect(card).toHaveTextContent('Members: g-finance-builders');
    expect(card).toHaveTextContent('Allowed: providers bedrock');
    expect(card).toHaveTextContent('Approval: low auto · medium g-approvers · high g-security+g-approvers');
    expect(screen.getByTestId('workspace-default')).toHaveTextContent('everyone who is in no other workspace');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('warns when no default policy exists and offers to create it with the id fixed', async () => {
    api.listWorkspacePolicies.mockResolvedValue([finance]);
    render(<WorkspacesPanel isAdmin />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('every deploy is refused (503) until one exists');
    fireEvent.click(screen.getByText('Create the default policy'));
    expect(screen.getByLabelText(/Workspace id/)).toHaveValue('default');
    expect(screen.getByLabelText(/Member groups/)).toBeDisabled();
  });

  it('creates a workspace from the Standard preset and sends exactly the preset document', async () => {
    render(<WorkspacesPanel isAdmin />);
    await screen.findByTestId('workspace-finance');

    fireEvent.click(screen.getByText('New workspace'));
    fireEvent.change(screen.getByLabelText(/Workspace id/), { target: { value: 'sales' } });
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'Sales' } });
    fireEvent.change(screen.getByLabelText(/Member groups/), { target: { value: 'g-sales' } });
    fireEvent.change(screen.getByLabelText(/Approver groups/), { target: { value: 'g-approvers' } });
    fireEvent.change(screen.getByLabelText(/Security groups/), { target: { value: 'g-security' } });
    fireEvent.click(screen.getByRole('button', { name: 'Standard' }));

    expect(screen.getByRole('status')).toHaveTextContent('Applied the Standard preset');
    expect(screen.getByLabelText(/Allowed providers/)).toHaveValue('bedrock');
    expect(screen.getByLabelText(/Approvers: medium risk/)).toHaveValue('g-approvers');
    expect(screen.getByLabelText(/Approvers: high risk/)).toHaveValue('g-security, g-approvers');
    expect(screen.getByLabelText(/Workspace id/)).toHaveValue('sales');

    fireEvent.click(screen.getByText('Create workspace'));
    await waitFor(() => expect(api.putWorkspacePolicy).toHaveBeenCalledTimes(1));
    expect(api.putWorkspacePolicy).toHaveBeenCalledWith(
      applyPreset('standard', {
        workspaceId: 'sales',
        name: 'Sales',
        memberGroups: ['g-sales'],
        adminGroups: [],
        approverGroups: ['g-approvers'],
        securityGroups: ['g-security'],
      }),
    );
    expect(await screen.findByText('Saved the policy for workspace "sales".')).toBeInTheDocument();
    expect(api.listWorkspacePolicies).toHaveBeenCalledTimes(2);
  });

  it('shows the backend refusal verbatim and keeps the form open', async () => {
    api.putWorkspacePolicy.mockRejectedValue({
      message: "workspaceId in the body ('x') must equal the one in the path ('finance')",
      status: 400,
    });
    render(<WorkspacesPanel isAdmin />);
    const card = await screen.findByTestId('workspace-finance');
    fireEvent.click(within(card).getByText('Edit'));
    expect(screen.getByLabelText(/Workspace id/)).toBeDisabled();
    fireEvent.click(screen.getByText('Save changes'));
    expect(await screen.findByRole('alert')).toHaveTextContent('must equal the one in the path');
    expect(screen.getByLabelText(/^Name/)).toHaveValue('Finance');
  });

  it('refuses a malformed budget before any request is made', async () => {
    render(<WorkspacesPanel isAdmin />);
    await screen.findByTestId('workspace-finance');
    fireEvent.click(screen.getByText('New workspace'));
    fireEvent.change(screen.getByLabelText(/Workspace id/), { target: { value: 'sales' } });
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'Sales' } });
    fireEvent.change(screen.getByLabelText(/Per-agent cap/), { target: { value: '-5' } });
    fireEvent.click(screen.getByText('Create workspace'));
    expect(await screen.findByRole('alert')).toHaveTextContent('The per-agent cap must be a non-negative number');
    expect(api.putWorkspacePolicy).not.toHaveBeenCalled();
  });

  it('deletes a policy only after an explicit confirmation', async () => {
    render(<WorkspacesPanel isAdmin />);
    const card = await screen.findByTestId('workspace-finance');
    fireEvent.click(within(card).getByText('Delete'));
    expect(api.deleteWorkspacePolicy).not.toHaveBeenCalled();
    fireEvent.click(within(card).getByText('Cancel'));
    expect(within(card).queryByText('Confirm delete')).not.toBeInTheDocument();
    fireEvent.click(within(card).getByText('Delete'));
    fireEvent.click(within(card).getByText('Confirm delete'));
    await waitFor(() => expect(api.deleteWorkspacePolicy).toHaveBeenCalledWith('finance'));
    expect(await screen.findByText('Deleted the policy for workspace "finance".')).toBeInTheDocument();
  });

  it('shows a list failure instead of an empty list', async () => {
    api.listWorkspacePolicies.mockRejectedValue({ message: 'Insufficient scope: admin', status: 403 });
    render(<WorkspacesPanel isAdmin />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Insufficient scope: admin');
  });
});
