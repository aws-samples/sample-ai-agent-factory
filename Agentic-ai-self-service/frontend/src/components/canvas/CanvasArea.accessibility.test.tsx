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

describe('CanvasArea accessibility', () => {
  it('disables the empty-state actions while no flow is open, and says why', () => {
    const onOpenTemplateGallery = vi.fn();
    const onOpenAgentGenerator = vi.fn();
    render(
      <CanvasArea
        nodes={[]}
        selectedNode={null}
        lastSaveError={null}
        onNodeCreate={vi.fn()}
        onNodeDoubleClick={vi.fn()}
        onRestoreDeployment={vi.fn()}
        onClearSaveError={vi.fn()}
        onOpenTemplateGallery={onOpenTemplateGallery}
        onOpenAgentGenerator={onOpenAgentGenerator}
        onOpenConfig={vi.fn()}
        authoringDisabledReason="Opening your flow…"
      />,
    );

    expect(screen.getByRole('status')).toHaveTextContent('Opening your flow…');
    const browse = screen.getByRole('button', { name: 'Browse Templates' });
    const generate = screen.getByRole('button', { name: 'Generate with AI' });
    expect(browse).toBeDisabled();
    expect(generate).toBeDisabled();
    fireEvent.click(browse);
    fireEvent.click(generate);
    expect(onOpenTemplateGallery).not.toHaveBeenCalled();
    expect(onOpenAgentGenerator).not.toHaveBeenCalled();
  });

  it('enables the empty-state actions once a flow is open', () => {
    render(
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
        authoringDisabledReason={null}
      />,
    );

    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole('button', { name: 'Browse Templates' })).toBeEnabled();
  });

  it('uses a theme-safe foreground and background for the primary empty-state action', () => {
    render(
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
      />,
    );

    expect(
      screen.getByRole('button', { name: 'Browse Templates' }),
    ).toHaveStyle({
      background: 'var(--accent)',
      color: 'var(--accent-foreground)',
    });
    expect(
      screen.getByRole('button', { name: 'Generate with AI' }),
    ).toHaveStyle({ color: 'var(--accent)' });
  });
});
