/**
 * F-15: a save conflict is shown as a decision, not as a generic "auto-save
 * failed" toast. Both sides are still intact; the user picks which one wins.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { CanvasArea } from './CanvasArea';

vi.mock('./WorkflowCanvas', () => ({
  default: () => <div data-testid="workflow-canvas" />,
}));

vi.mock('../deploy/ActiveDeploymentBanner', () => ({
  ActiveDeploymentBanner: () => null,
}));

vi.mock('../modals/modalRegistry', () => ({
  configurationTargetForExistingNode: () => null,
}));

const conflict = {
  flowId: 'flow-shared',
  serverVersion: 7,
  serverUpdatedAt: '2026-09-28T10:00:00+00:00',
  message: 'This flow was changed elsewhere since you loaded it.',
};

function renderArea(props: Partial<Parameters<typeof CanvasArea>[0]>) {
  return render(
    <CanvasArea
      nodes={[]}
      selectedNode={null}
      lastSaveError={null}
      onNodeCreate={vi.fn()}
      onNodeDoubleClick={vi.fn()}
      onRestoreDeployment={vi.fn()}
      onClearSaveError={vi.fn()}
      onOpenTemplateGallery={vi.fn()}
      onOpenAgentGenerator={vi.fn()}
      onOpenConfig={vi.fn()}
      {...props}
    />,
  );
}

describe('CanvasArea save conflict', () => {
  it('offers reload and keep-mine, and routes each choice to the resolver', () => {
    const onResolveSaveConflict = vi.fn();
    renderArea({ saveConflict: conflict, onResolveSaveConflict });

    const dialog = screen.getByRole('alertdialog', { name: 'This flow was changed elsewhere' });
    expect(dialog).toHaveAccessibleDescription(/newer version/);

    fireEvent.click(screen.getByRole('button', { name: 'Reload their version' }));
    expect(onResolveSaveConflict).toHaveBeenLastCalledWith('reload');

    fireEvent.click(screen.getByRole('button', { name: 'Keep mine and overwrite' }));
    expect(onResolveSaveConflict).toHaveBeenLastCalledWith('keep_mine');
  });

  it('shows the conflict instead of the generic auto-save toast when both are set', () => {
    renderArea({ saveConflict: conflict, lastSaveError: conflict.message });

    expect(screen.getByTestId('autosave-conflict-toast')).toBeInTheDocument();
    expect(screen.queryByTestId('autosave-error-toast')).toBeNull();
  });

  it('keeps the generic toast for an ordinary save failure', () => {
    renderArea({ lastSaveError: 'autosave endpoint unavailable' });

    expect(screen.getByTestId('autosave-error-toast')).toBeInTheDocument();
    expect(screen.queryByTestId('autosave-conflict-toast')).toBeNull();
  });
});
