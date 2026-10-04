/**
 * A standalone Python export creates no AWS resources, so governance tags cannot be
 * applied to it. The UI must refuse visibly -- never strip the fields and succeed --
 * and must show the backend's own 400 detail when the route refuses something.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DeployPanel } from './DeployPanel';
import type { RuntimeConfiguration } from '../../types/components';
import { useWorkflowStore } from '../../store/workflowStore';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

const config: RuntimeConfiguration = {
  name: 'test-runtime',
  entrypoint: 'agent.py',
  framework: 'strands_agents',
  model: {
    provider: 'bedrock',
    modelId: 'us.anthropic.claude-sonnet-5',
    temperature: 0.7,
    topP: 0.9,
  },
  systemPrompt: 'hi',
  deploymentType: 'direct_code_deploy',
  pythonRuntime: 'PYTHON_3_12',
  protocol: 'HTTP',
  idleTimeout: 900,
  maxLifetime: 28800,
  enableOtel: false,
  modelProvider: 'bedrock',
  multiAgentPattern: 'none',
};

const GOVERNANCE_MESSAGE =
  'Standalone Python export contains no AWS resources, so tags and tag profiles cannot be applied. ' +
  'Clear the governance settings, or use the CloudFormation export or platform deploy instead.';

type ExportReply = { ok: boolean; status: number; body: unknown };

function mockApi(exportReply: ExportReply, cfnReplies: ExportReply[] = []) {
  mockAuthFetch.mockImplementation(async (url: string) => {
    if (url === '/api/settings/tags') {
      return {
        ok: true,
        status: 200,
        json: async () => [{
          key: 'Environment',
          default_value: null,
          required: false,
          show_on_card: true,
          created_at: '2026-09-20T10:00:00Z',
          updated_at: '2026-09-20T10:00:00Z',
        }],
      };
    }
    if (url === '/api/settings/tag-profiles') {
      return {
        ok: true,
        status: 200,
        json: async () => [{
          name: 'regulated',
          values: { Environment: 'production' },
          created_at: '2026-09-20T11:00:00Z',
          updated_at: '2026-09-20T11:00:00Z',
        }],
      };
    }
    if (url === '/api/export-python') {
      return { ok: exportReply.ok, status: exportReply.status, json: async () => exportReply.body };
    }
    if (url === '/api/generate-cfn-template') {
      const reply = cfnReplies.shift() ?? { ok: true, status: 200, body: { download_url: 'https://d.example.invalid/c.zip' } };
      return { ok: reply.ok, status: reply.status, json: async () => reply.body };
    }
    return { ok: true, json: async () => ({}) };
  });
}

const exportCalls = () => mockAuthFetch.mock.calls.filter(([url]) => url === '/api/export-python');

function renderPanel(restored = false) {
  render(
    <DeployPanel
      config={config}
      nodeId="node-1"
      isVisible
      onClose={() => {}}
      restoredDeployment={restored ? { runtimeId: 'rt-live-1', endpoint: 'https://runtime.example.invalid' } : null}
    />,
  );
}

async function waitForGovernanceReady() {
  await screen.findByLabelText('Tag profile');
  await waitFor(() => {
    expect(screen.getByRole('button', { name: 'Export as Python' })).toBeEnabled();
    expect(
      screen.getByRole('button', { name: 'Download CloudFormation Template' }),
    ).toBeEnabled();
  });
}

/** The controls a user needs to fix an export and retry, and none that would redeploy. */
function expectIdleControlsIntact() {
  expect(screen.getByLabelText('Tag profile')).toBeInTheDocument();
  expect(screen.getByLabelText('Naming prefix')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Download CloudFormation Template' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Export as Python' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Retry Deployment/ })).toBeNull();
}

/** A live deployment is still reachable: its runtime, Chat, and Delete. */
function expectDeploymentIntact() {
  expect(screen.getByText('rt-live-1')).toBeInTheDocument();
  expect(screen.getByRole('tab', { name: 'Chat' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Delete from AWS/ })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Retry Deployment/ })).toBeNull();
}

describe('DeployPanel standalone Python export and governance', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useWorkflowStore.getState().resetWorkflowDocument(null);
    sessionStorage.clear();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  });

  it('refuses visibly and sends nothing when a tag profile is selected', async () => {
    mockApi({ ok: true, status: 200, body: { download_url: 'https://downloads.example.invalid/a.zip' } });
    renderPanel();

    await waitForGovernanceReady();
    // The option must exist before the change, or the change is a silent no-op (certification #31).
    await screen.findByRole('option', { name: 'regulated' }, { timeout: 10_000 });
    fireEvent.change(screen.getByLabelText('Tag profile'), { target: { value: 'regulated' } });
    await screen.findByDisplayValue('production');

    const status = await screen.findByRole('status', { name: '' });
    expect(status).toHaveTextContent(GOVERNANCE_MESSAGE);
    const button = screen.getByRole('button', { name: 'Export as Python' });
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription(GOVERNANCE_MESSAGE);

    fireEvent.click(button);
    await new Promise((r) => setTimeout(r, 0));
    expect(exportCalls()).toHaveLength(0);
  });

  it('refuses visibly and sends nothing when only an explicit tag is set', async () => {
    mockApi({ ok: true, status: 200, body: { download_url: 'https://downloads.example.invalid/a.zip' } });
    renderPanel();

    await waitForGovernanceReady();
    fireEvent.change(screen.getByLabelText(/Environment/), { target: { value: 'staging' } });

    expect(await screen.findByText(GOVERNANCE_MESSAGE)).toHaveAttribute('role', 'status');
    fireEvent.click(screen.getByRole('button', { name: 'Export as Python' }));
    await new Promise((r) => setTimeout(r, 0));
    expect(exportCalls()).toHaveLength(0);
  });

  it('exports normally, with no governance fields, when none is selected', async () => {
    mockApi({ ok: true, status: 200, body: { download_url: 'https://downloads.example.invalid/a.zip' } });
    renderPanel();
    await waitForGovernanceReady();

    expect(screen.queryByText(GOVERNANCE_MESSAGE)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Export as Python' }));

    await waitFor(() => expect(exportCalls()).toHaveLength(1));
    const body = JSON.parse((exportCalls()[0][1] as { body: string }).body);
    expect(body).not.toHaveProperty('resourceTags');
    expect(body).not.toHaveProperty('tagProfile');
    await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled());
  });

  it('shows the backend 400 detail string, not a bare status code', async () => {
    const detail = 'namingProfile applies only to POST /api/generate-cfn-template. Remove namingProfile.';
    mockApi({ ok: false, status: 400, body: { detail } });
    renderPanel();
    await waitForGovernanceReady();

    fireEvent.click(screen.getByRole('button', { name: 'Export as Python' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(detail);
    expect(screen.queryByText('Python export failed (400)')).toBeNull();
    expectIdleControlsIntact();
  });

  it('keeps the settings and both exports after a CFN 400, and a corrected retry succeeds', async () => {
    const detail = 'Tag CostCenter is required by policy. Add CostCenter and export again.';
    mockApi({ ok: true, status: 200, body: {} }, [{ ok: false, status: 400, body: { detail } }]);
    renderPanel();
    await waitForGovernanceReady();

    fireEvent.click(screen.getByRole('button', { name: 'Download CloudFormation Template' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(detail);
    expectIdleControlsIntact();

    // Editing a setting clears the stale error; the retry then succeeds.
    fireEvent.change(screen.getByLabelText('Naming prefix'), { target: { value: 'ecb' } });
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'Download CloudFormation Template' }));
    await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(mockAuthFetch.mock.calls.filter(([url]) => url === '/api/deploy')).toHaveLength(0);
  });

  it.each([
    ['CFN', 'Download CloudFormation Template', 400],
    ['CFN', 'Download CloudFormation Template', 500],
    ['Python', 'Export as Python', 400],
    ['Python', 'Export as Python', 500],
  ])('a failed %s export (%s → %s) leaves a live deployment reachable', async (_kind, button, status) => {
    const failure = { ok: false, status, body: { detail: 'refused' } };
    mockApi(failure, [failure]);
    renderPanel(true);
    fireEvent.click(await screen.findByRole('tab', { name: 'Deploy' }));
    await screen.findByText('rt-live-1');
    await waitForGovernanceReady();

    fireEvent.click(screen.getByRole('button', { name: button }));

    await screen.findByRole('alert');
    expectDeploymentIntact();
  });

  // Both export routes read an error through one helper; table-driven so neither can drift.
  it.each([
    ['Python', 'Export as Python', 400, { detail: 'Remove namingProfile.' }, 'Remove namingProfile.'],
    ['CFN', 'Download CloudFormation Template', 400, { detail: 'Remove namingProfile.' }, 'Remove namingProfile.'],
    [
      'Python',
      'Export as Python',
      422,
      { detail: [{ loc: ['body', 'config'], msg: 'Field required', type: 'missing' }] },
      'Field required',
    ],
    [
      'CFN',
      'Download CloudFormation Template',
      422,
      { detail: [{ loc: ['body', 'config'], msg: 'Field required', type: 'missing' }] },
      'Field required',
    ],
    ['Python', 'Export as Python', 500, { detail: 'Internal server error' }, 'Python export failed (500)'],
    [
      'CFN',
      'Download CloudFormation Template',
      500,
      { detail: 'Internal server error' },
      'Template generation failed (500)',
    ],
  ])('a %s export (%s → %s) shows the actionable message', async (_kind, button, status, body, shown) => {
    const failure = { ok: false, status, body };
    mockApi(failure, [failure]);
    renderPanel();
    await waitForGovernanceReady();

    fireEvent.click(screen.getByRole('button', { name: button }));

    expect(await screen.findByRole('alert')).toHaveTextContent(shown);
    expect(screen.queryByText('Internal server error')).toBeNull();
    expectIdleControlsIntact();
  });
});
