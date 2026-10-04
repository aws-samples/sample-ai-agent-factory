import { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AppHeader } from './AppHeader';

vi.mock('aws-amplify/auth', () => ({ signOut: vi.fn() }));

function HeaderHarness({
  onChange,
}: {
  onChange: (mode: 'visual' | 'harness') => void;
}) {
  const [mode, setMode] = useState<'visual' | 'harness'>('visual');
  return (
    <AppHeader
      activeFlowName="Demo"
      nodesCount={1}
      deployableConfig={undefined}
        isReadyToDeploy={true}
        validationErrorCount={0}
      authoringMode={mode}
      onAuthoringModeChange={(nextMode) => {
        setMode(nextMode);
        onChange(nextMode);
      }}
      onDeploy={vi.fn()}
      onOpenRegistry={vi.fn()}
      onPreviewAsEndUser={vi.fn()}
      onOpenHitlInbox={vi.fn()}
      canDeploy={false}
      canOpenRegistry
      showAdminControls
      canOpenHitlInbox
    />
  );
}

describe('AppHeader authoring-mode tabs', () => {
  it('exposes the application name as the page heading', () => {
    render(<HeaderHarness onChange={vi.fn()} />);

    expect(
      screen.getByRole('heading', { level: 1, name: 'AgentCore Flows' }),
    ).toHaveStyle({ color: 'var(--header-fg)' });
    expect(screen.getByRole('banner')).toBeInTheDocument();
  });

  it('uses manual activation so arrowing does not replace the authoring view', async () => {
    const onAuthoringModeChange = vi.fn();
    render(<HeaderHarness onChange={onAuthoringModeChange} />);

    const visual = screen.getByRole('tab', { name: 'Visual Canvas' });
    const harness = screen.getByRole('tab', { name: 'Harness' });
    expect(visual).toHaveAttribute('tabindex', '0');
    expect(harness).toHaveAttribute('tabindex', '-1');
    expect(visual).toHaveAttribute('aria-controls', 'authoring-panel-visual');
    expect(harness).toHaveAttribute('aria-controls', 'authoring-panel-harness');

    fireEvent.keyDown(visual, { key: 'ArrowRight' });
    await waitFor(() => expect(document.activeElement).toBe(harness));
    expect(onAuthoringModeChange).not.toHaveBeenCalled();
    expect(harness).toHaveAttribute('aria-selected', 'false');
    expect(harness).toHaveAttribute('tabindex', '0');

    fireEvent.keyDown(harness, { key: 'Enter' });
    expect(onAuthoringModeChange).toHaveBeenCalledWith('harness');
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Harness' })).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    );
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole('tab', { name: 'Harness' }),
      ),
    );

    fireEvent.keyDown(screen.getByRole('tab', { name: 'Harness' }), { key: 'Home' });
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole('tab', { name: 'Visual Canvas' }),
      ),
    );
    expect(onAuthoringModeChange).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(screen.getByRole('tab', { name: 'Visual Canvas' }), { key: ' ' });
    expect(onAuthoringModeChange).toHaveBeenLastCalledWith('visual');
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole('tab', { name: 'Visual Canvas' }),
      ),
    );
  });

  it('does not render registry or type-admin controls without their capabilities', () => {
    render(
      <AppHeader
        activeFlowName="Demo"
        nodesCount={0}
        deployableConfig={undefined}
        isReadyToDeploy={true}
        validationErrorCount={0}
        authoringMode="visual"
        onAuthoringModeChange={vi.fn()}
        onDeploy={vi.fn()}
        onOpenRegistry={vi.fn()}
        onPreviewAsEndUser={vi.fn()}
        onOpenHitlInbox={vi.fn()}
        canDeploy={false}
        canOpenRegistry={false}
        showAdminControls={false}
        canOpenHitlInbox={false}
      />,
    );

    expect(screen.queryByRole('button', { name: 'Browse agent registry' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'View as end-user' })).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Human-in-the-loop approvals inbox' }),
    ).not.toBeInTheDocument();
  });
});

describe('AppHeader readiness badge follows the validator', () => {
  const base = {
    activeFlowName: 'Flow',
    nodesCount: 2,
    deployableConfig: { name: 'rt' } as never,
    authoringMode: 'visual' as const,
    onAuthoringModeChange: vi.fn(),
    onDeploy: vi.fn(),
    onOpenRegistry: vi.fn(),
    onPreviewAsEndUser: vi.fn(),
    onOpenHitlInbox: vi.fn(),
    canDeploy: true,
    canOpenRegistry: false,
    showAdminControls: false,
    canOpenHitlInbox: false,
  };

  it('never says "Ready to deploy" over a red canvas, and counts the errors instead', () => {
    // Live, 2026-09-28: the badge read "Ready to deploy" while the validator held a gateway error.
    render(<AppHeader {...base} isReadyToDeploy={false} validationErrorCount={1} />);
    expect(screen.queryByText('Ready to deploy')).toBeNull();
    expect(screen.getByRole('status')).toHaveTextContent('1 validation error');
  });

  it('says "Ready to deploy" only when the validator agrees', () => {
    render(<AppHeader {...base} isReadyToDeploy={true} validationErrorCount={0} />);
    expect(screen.getByText('Ready to deploy')).toBeInTheDocument();
    expect(screen.queryByTestId('canvas-validation-badge')).toBeNull();
  });
});
