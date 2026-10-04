import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PromptLibraryModal } from './PromptLibraryModal';
import { RegistryModal } from './RegistryModal';
import { HitlInboxModal } from './HitlInboxModal';
import { ConnectorPickerModal } from './ConnectorPickerModal';
import { DeleteConfirmDialog } from '../flow-sidebar/DeleteConfirmDialog';
import { AgentGeneratorPanel } from '../ai/AgentGeneratorPanel';

const api = vi.hoisted(() => ({
  listPromptsApi: vi.fn(),
  createPromptApi: vi.fn(),
  addPromptVersionApi: vi.fn(),
  promotePromptVersionApi: vi.fn(),
  resolvePromptApi: vi.fn(),
  deletePromptApi: vi.fn(),
  searchRegistryApi: vi.fn(),
  cloneFromRegistryApi: vi.fn(),
  approveRegistryApi: vi.fn(),
  rejectRegistryApi: vi.fn(),
  getRegistryEntryApi: vi.fn(),
  listConnectorsApi: vi.fn(),
  getConnectorApi: vi.fn(),
  listHitlPending: vi.fn(),
  decideHitl: vi.fn(),
  generateCanvasApi: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  ...api,
  getApiClient: () => ({
    listHitlPending: api.listHitlPending,
    decideHitl: api.decideHitl,
  }),
  getErrorMessage: (error: unknown) =>
    error instanceof Error ? error.message : String(error),
}));

vi.mock('../../auth/useIsRegistryAdmin', () => ({
  useIsRegistryAdmin: () => true,
}));

vi.mock('../../auth/scopes', () => ({
  useScopes: () => ({ hasScope: () => true }),
}));

vi.mock('../../store/workflowStore', () => ({
  useWorkflowStore: (selector: (state: { validationState: null }) => unknown) =>
    selector({ validationState: null }),
}));

vi.mock('./AwsRegistryPanel', () => ({ AwsRegistryPanel: () => null }));
vi.mock('./LiteLLMRegistryPanel', () => ({ LiteLLMRegistryPanel: () => null }));
vi.mock('./DeployTargetsPanel', () => ({ DeployTargetsPanel: () => null }));
vi.mock('./registry/McpServersPanel', () => ({ McpServersPanel: () => <p>MCP catalog</p> }));
vi.mock('./registry/TokenInfoCard', () => ({ TokenInfoCard: () => <p>Identity</p> }));

beforeEach(() => {
  vi.clearAllMocks();
  api.listPromptsApi.mockResolvedValue([]);
  api.searchRegistryApi.mockResolvedValue([]);
  api.listConnectorsApi.mockResolvedValue([]);
  api.listHitlPending.mockResolvedValue([]);
  api.decideHitl.mockResolvedValue(undefined);
});

describe('hand-built modal accessibility contract', () => {
  it('gives the prompt library a named modal, trapped focus, Escape, and focus restoration', async () => {
    const onClose = vi.fn();
    const view = render(
      <>
        <button type="button">Open prompt library</button>
        <PromptLibraryModal isOpen={false} onClose={onClose} />
      </>,
    );
    const opener = screen.getByRole('button', { name: 'Open prompt library' });
    opener.focus();

    view.rerender(
      <>
        <button type="button">Open prompt library</button>
        <PromptLibraryModal isOpen onClose={onClose} />
      </>,
    );
    const dialog = screen.getByRole('dialog', { name: 'Prompt Library' });
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    opener.focus();
    expect(dialog.contains(document.activeElement)).toBe(true);

    view.rerender(
      <>
        <button type="button">Open prompt library</button>
        <PromptLibraryModal isOpen={false} onClose={onClose} />
      </>,
    );
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });

  it('gives the HITL inbox and connector picker modal semantics and Escape dismissal', async () => {
    const closeInbox = vi.fn();
    const inboxView = render(<HitlInboxModal isOpen onClose={closeInbox} />);
    const inbox = screen.getByRole('dialog', { name: 'Human-in-the-loop Approvals' });
    expect(inbox).toHaveAttribute('aria-modal', 'true');
    await waitFor(() => expect(inbox.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(closeInbox).toHaveBeenCalledTimes(1);
    inboxView.unmount();

    const closeConnector = vi.fn();
    render(<ConnectorPickerModal isOpen onClose={closeConnector} />);
    const connector = screen.getByRole('dialog', { name: 'Add a connector' });
    expect(connector).toHaveAttribute('aria-modal', 'true');
    await waitFor(() => expect(connector.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(closeConnector).toHaveBeenCalledTimes(1);
  });

  it('focuses the safe action in the flow deletion dialog and owns Escape', async () => {
    const onCancel = vi.fn();
    render(
      <DeleteConfirmDialog
        isOpen
        flowName="Production flow"
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: 'Delete Flow' });
    const cancel = within(dialog).getByRole('button', { name: 'Cancel' });
    await waitFor(() => expect(document.activeElement).toBe(cancel));

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});

describe('native-dialog replacements', () => {
  it('uses an accessible nested confirmation before replacing an existing canvas', async () => {
    api.generateCanvasApi.mockResolvedValue({
      success: true,
      responseType: 'spec',
      spec: {
        name: 'Generated support agent',
        description: 'Answers questions',
        rationale: 'Requested by the user',
        nodes: [{
          idSuffix: 'runtime-1',
          type: 'runtime',
          label: 'Runtime',
          position: { x: 0, y: 0 },
          configuration: {},
        }],
        edges: [],
      },
    });
    const nativeConfirm = vi.spyOn(window, 'confirm');
    const onApplySpec = vi.fn();
    const onClose = vi.fn();

    render(
      <AgentGeneratorPanel
        isVisible
        onClose={onClose}
        onApplySpec={onApplySpec}
        hasExistingNodes
      />,
    );

    const panel = screen.getByRole('dialog', { name: 'Generate Agent' });
    expect(panel).toHaveAttribute('aria-modal', 'true');
    expect(
      within(panel).getByRole('heading', { name: 'Generate Agent', level: 2 }),
    ).toBeInTheDocument();
    const agentDescription = screen.getByRole('textbox', { name: 'Agent description' });
    fireEvent.change(agentDescription, {
      target: { value: 'Build a support agent' },
    });
    const send = screen.getByRole('button', { name: 'Send' });
    send.focus();
    fireEvent.click(send);
    const apply = await screen.findByRole('button', { name: /Apply to Canvas/ });
    await waitFor(() => expect(document.activeElement).toBe(agentDescription));
    expect(within(panel).getByText('Answers questions')).toBeInTheDocument();
    expect(within(panel).getByRole('listitem')).toHaveTextContent('runtime (Runtime)');
    expect(within(panel).queryByText(/\*\*Generated support agent\*\*/)).toBeNull();
    fireEvent.click(within(panel).getByText('View raw JSON'));
    expect(
      within(panel).getByRole('region', { name: 'Generated agent specification JSON' }),
    ).toHaveAttribute('tabindex', '0');
    fireEvent.click(apply);

    const confirmation = screen.getByRole('dialog', { name: 'Replace current workflow?' });
    expect(nativeConfirm).not.toHaveBeenCalled();
    expect(confirmation).toHaveAttribute('aria-modal', 'true');

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Replace current workflow?' })).toBeNull(),
    );
    expect(screen.getByRole('dialog', { name: 'Generate Agent' })).toBeVisible();
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /Apply to Canvas/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Replace workflow' }));
    expect(onApplySpec).toHaveBeenCalledTimes(1);
    nativeConfirm.mockRestore();
  });

  it('uses an accessible rejection form and submits the entered reason directly', async () => {
    const entry = {
      agent_slug: 'pending-agent',
      display_name: 'Pending Agent',
      description: 'Awaiting review',
      tags: [],
      status: 'pending',
      visibility: 'org',
      is_owner: false,
      usage_count: 0,
      updated_at: '2026-09-21T00:00:00.000Z',
    };
    api.searchRegistryApi.mockResolvedValue([entry]);
    api.rejectRegistryApi.mockResolvedValue(undefined);
    const nativePrompt = vi.spyOn(window, 'prompt');

    render(<RegistryModal isOpen onClose={vi.fn()} />);

    const registry = screen.getByRole('dialog', { name: /Agent Registry/ });
    expect(registry).toHaveAttribute('aria-modal', 'true');

    const agentsTab = screen.getByRole('tab', { name: 'Agent blueprints' });
    fireEvent.keyDown(agentsTab, { key: 'ArrowRight' });
    expect(screen.getByRole('tab', { name: 'MCP servers' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    fireEvent.keyDown(screen.getByRole('tab', { name: 'MCP servers' }), { key: 'Home' });

    const reject = await screen.findByRole('button', { name: 'Reject Pending Agent' });
    fireEvent.keyDown(reject, { key: 'Enter' });
    expect(screen.queryByRole('tab', { name: 'Overview' })).toBeNull();

    fireEvent.click(reject);
    expect(nativePrompt).not.toHaveBeenCalled();
    const rejectionDialog = screen.getByRole('dialog', { name: 'Reject Pending Agent?' });
    const reason = within(rejectionDialog).getByLabelText(/Rejection reason/);
    fireEvent.change(reason, { target: { value: 'Missing security review' } });
    fireEvent.click(within(rejectionDialog).getByRole('button', { name: 'Reject agent' }));

    await waitFor(() =>
      expect(api.rejectRegistryApi).toHaveBeenCalledWith(
        'pending-agent',
        'Missing security review',
      ),
    );
    nativePrompt.mockRestore();
  });

  it('makes registry detail tabs keyboard navigable and exposes status filters as toggles', async () => {
    const entry = {
      agent_slug: 'approved-agent',
      display_name: 'Approved Agent',
      description: 'Ready to clone',
      tags: [],
      status: 'approved',
      visibility: 'org',
      is_owner: true,
      usage_count: 2,
      updated_at: '2026-09-21T00:00:00.000Z',
      canvas_snapshot: { nodes: [], edges: [] },
    };
    api.searchRegistryApi.mockResolvedValue([entry]);
    api.getRegistryEntryApi.mockResolvedValue(entry);

    render(<RegistryModal isOpen onClose={vi.fn()} onClone={vi.fn()} />);

    const allFilter = await screen.findByRole('button', { name: 'All (1)' });
    expect(allFilter).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(
      screen.getByRole('button', { name: 'View details for Approved Agent' }),
    );

    const overview = await screen.findByRole('tab', { name: 'Overview' });
    const components = screen.getByRole('tab', { name: 'Components' });
    expect(overview).toHaveAttribute('tabindex', '0');
    expect(components).toHaveAttribute('tabindex', '-1');

    fireEvent.keyDown(overview, { key: 'ArrowRight' });
    expect(components).toHaveAttribute('aria-selected', 'true');
    expect(document.activeElement).toBe(components);
    expect(screen.getByRole('tabpanel', { name: 'Components' })).toBeVisible();
  });
});
