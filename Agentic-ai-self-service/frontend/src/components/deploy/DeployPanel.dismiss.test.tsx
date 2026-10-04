/**
 * DeployPanel — the same two questions as TemplateGallery, plus the one this drawer
 * has and the gallery does not: a nested confirmation that also listens for Escape.
 *
 * Measured against the deployed bundle before the fix, as a real admin in Chromium:
 *
 *   semantics: {"roleDialogAnywhere":false,"ariaModal":null,"ariaLabelledBy":null,
 *              "firstButtons":[{"text":"","ariaLabel":null,"title":null},...]}
 *   drawer still open after Escape: true
 *
 * `ConfirmDialog` registers its OWN document-level Escape listener, so an unguarded
 * handler here would let one keypress cancel the delete confirmation and close the
 * drawer underneath it in the same tick. That guard is the interesting test below; the
 * route to it is the `restoredDeployment` prop, which puts the drawer in the deployed
 * state where the Delete control exists.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
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
  model: { provider: 'bedrock', modelId: 'm', temperature: 0.7, topP: 0.9 },
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

beforeEach(() => {
  vi.clearAllMocks();
  useWorkflowStore.getState().resetWorkflowDocument(null);
  mockAuthFetch.mockImplementation(async (...args: unknown[]) => {
    const url = String(args[0]);
    const response =
      url === '/api/settings/tags' || url === '/api/settings/tag-profiles' ? [] : {};
    return { ok: true, json: async () => response };
  });
});

describe('dismissing the deploy drawer from the keyboard', () => {
  it('Escape closes it', () => {
    const onClose = vi.fn();
    render(<DeployPanel config={config} nodeId="n1" isVisible onClose={onClose} />);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('a key that is not Escape does not close it', () => {
    const onClose = vi.fn();
    render(<DeployPanel config={config} nodeId="n1" isVisible onClose={onClose} />);

    fireEvent.keyDown(document, { key: 'Enter' });
    fireEvent.keyDown(document, { key: 'Escape ' }); // trailing space: not the key
    expect(onClose).not.toHaveBeenCalled();
  });

  it('a hidden drawer does not swallow Escape', () => {
    const onClose = vi.fn();
    render(<DeployPanel config={config} nodeId="n1" isVisible={false} onClose={onClose} />);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).not.toHaveBeenCalled();
  });

  it('Escape cancels an open delete confirmation WITHOUT also closing the drawer', async () => {
    // Both components listen on `document`, so both handlers run for one keypress.
    // Without the `!showDeleteConfirm` guard the user loses the drawer as well as the
    // confirmation, and never sees which of the two their Escape applied to.
    const onClose = vi.fn();
    render(
      <DeployPanel
        config={config}
        nodeId="n1"
        isVisible
        onClose={onClose}
        restoredDeployment={{ runtimeId: 'r1', endpoint: 'https://e', gatewayUrl: undefined }}
      />,
    );

    // The restore effect switches to the Chat tab, where the Delete control lives.
    const del = await waitFor(() => screen.getByRole('button', { name: /^Delete$/i }));
    fireEvent.click(del);

    // The confirmation is up.
    await waitFor(() => expect(screen.getByText(/Delete Runtime/i)).toBeTruthy());

    fireEvent.keyDown(document, { key: 'Escape' });

    // The confirmation went away; the drawer stayed.
    await waitFor(() => expect(screen.queryByText(/Delete Runtime/i)).toBeNull());
    expect(onClose).not.toHaveBeenCalled();

    // And once the confirmation is gone, Escape closes the drawer as normal — so the
    // guard suppresses the keypress rather than disabling the handler for good.
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('what assistive technology is told about the deploy drawer', () => {
  it('is a dialog, is modal, and is named by its own heading', () => {
    render(<DeployPanel config={config} nodeId="n1" isVisible onClose={vi.fn()} />);

    const dialog = screen.getByRole('dialog', { name: 'Deploy & Test' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(
      screen.getByRole('heading', { name: 'Deploy & Test', level: 2 }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole('button', { name: 'Deploy to AgentCore' }),
    ).toHaveLength(1);
  });

  it('the close control has an accessible name, and closes', () => {
    const onClose = vi.fn();
    render(<DeployPanel config={config} nodeId="n1" isVisible onClose={onClose} />);

    fireEvent.click(screen.getByRole('button', { name: /close the deploy and test panel/i }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('runtime deletion feedback', () => {
  it('keeps the deployed state and surfaces a failed delete to the user', async () => {
    mockAuthFetch.mockImplementation(async (...args: unknown[]) => {
      const url = String(args[0]);
      if (url === '/api/runtime/r1') {
        return {
          ok: false,
          status: 500,
          json: async () => ({
            success: false,
            message: 'Deletion failed audit sentinel',
          }),
        };
      }
      const response =
        url === '/api/settings/tags' || url === '/api/settings/tag-profiles' ? [] : {};
      return { ok: true, status: 200, json: async () => response };
    });

    render(
      <DeployPanel
        config={config}
        nodeId="n1"
        isVisible
        onClose={vi.fn()}
        restoredDeployment={{
          runtimeId: 'r1',
          endpoint: 'https://example.test/runtime',
        }}
      />,
    );

    fireEvent.click(
      await screen.findByRole('button', { name: /^Delete$/i }),
    );
    const confirmation = screen.getByRole('dialog', { name: 'Delete Runtime' });
    fireEvent.click(
      within(confirmation).getByRole('button', { name: /^Delete$/i }),
    );

    expect(screen.getByRole('tab', { name: 'Chat' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(
      await screen.findByRole('alert'),
    ).toHaveTextContent(
      'Runtime deletion failed: Deletion failed audit sentinel',
    );
    expect(screen.getByLabelText('Message to test agent')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Delete$/i })).toBeInTheDocument();
  });
});

describe('deploy drawer focus containment', () => {
  it('wraps Tab, rejects outside focus, and restores the opener', async () => {
    const onClose = vi.fn();
    const view = render(
      <>
        <button type="button">Open deploy panel</button>
        <DeployPanel config={config} nodeId="n1" isVisible={false} onClose={onClose} />
      </>,
    );
    const opener = screen.getByRole('button', { name: 'Open deploy panel' });
    opener.focus();

    view.rerender(
      <>
        <button type="button">Open deploy panel</button>
        <DeployPanel config={config} nodeId="n1" isVisible onClose={onClose} />
      </>,
    );
    const dialog = screen.getByRole('dialog', { name: 'Deploy & Test' });
    await screen.findByText('No organisation tag policies or profiles are configured.');
    await waitFor(() => {
      expect(
        screen.getAllByRole('button', { name: 'Deploy to AgentCore' })[0],
      ).toBeEnabled();
    });
    const focusable = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'a[href], area[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), iframe, object, embed, [contenteditable="true"], [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((element) => element.tabIndex >= 0);
    const first = focusable[0];
    const last = focusable[focusable.length - 1];

    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    last.focus();
    fireEvent.keyDown(last, { key: 'Tab' });
    expect(document.activeElement).toBe(first);

    first.focus();
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(last);

    opener.focus();
    expect(dialog.contains(document.activeElement)).toBe(true);

    view.rerender(
      <>
        <button type="button">Open deploy panel</button>
        <DeployPanel config={config} nodeId="n1" isVisible={false} onClose={onClose} />
      </>,
    );
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });
});

describe('deploy drawer tab semantics and keyboard navigation', () => {
  it('exposes one selected tab and uses arrow keys while skipping disabled tabs', () => {
    render(<DeployPanel config={config} nodeId="n1" isVisible onClose={vi.fn()} />);

    const tabs = screen.getAllByRole('tab');
    expect(tabs).toHaveLength(7);

    const deploy = screen.getByRole('tab', { name: 'Deploy' });
    const chat = screen.getByRole('tab', { name: 'Chat' });
    const versions = screen.getByRole('tab', { name: 'Versions' });
    const triggers = screen.getByRole('tab', { name: 'Triggers' });

    expect(deploy).toHaveAttribute('aria-selected', 'true');
    expect(deploy).toHaveAttribute('tabindex', '0');
    expect(chat).toBeDisabled();
    expect(versions).toHaveAttribute('tabindex', '-1');
    expect(screen.getByRole('tabpanel', { name: 'Deploy' })).toBeTruthy();

    fireEvent.keyDown(deploy, { key: 'ArrowRight' });
    expect(versions).toHaveAttribute('aria-selected', 'true');
    expect(document.activeElement).toBe(versions);
    expect(screen.getByRole('tabpanel', { name: 'Versions' })).toBeTruthy();

    fireEvent.keyDown(versions, { key: 'End' });
    expect(triggers).toHaveAttribute('aria-selected', 'true');
    expect(document.activeElement).toBe(triggers);

    fireEvent.keyDown(triggers, { key: 'Home' });
    expect(deploy).toHaveAttribute('aria-selected', 'true');
    expect(document.activeElement).toBe(deploy);
  });
});
