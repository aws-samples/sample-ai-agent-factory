import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ComponentPalette } from './ComponentPalette';

describe('ComponentPalette prompt library access', () => {
  it('offers a named prompt-library control and invokes its callback', () => {
    const onOpenPromptLibrary = vi.fn();
    render(
      <ComponentPalette onOpenPromptLibrary={onOpenPromptLibrary} />,
    );

    fireEvent.click(
      screen.getByRole('button', { name: 'Open prompt library' }),
    );

    expect(onOpenPromptLibrary).toHaveBeenCalledTimes(1);
  });

  it('offers a keyboard-operable add control for every expanded component', () => {
    const onAddComponent = vi.fn();
    render(<ComponentPalette onAddComponent={onAddComponent} />);

    fireEvent.click(
      screen.getByRole('button', { name: 'Add AgentCore Runtime to canvas' }),
    );
    fireEvent.click(
      screen.getByRole('button', { name: 'Add Jira to canvas' }),
    );

    expect(onAddComponent).toHaveBeenNthCalledWith(1, 'runtime', undefined);
    expect(onAddComponent).toHaveBeenNthCalledWith(2, 'tool', 'connector:jira');
  });

  it('keeps the add controls available when the palette is collapsed', () => {
    const onAddComponent = vi.fn();
    render(
      <ComponentPalette
        collapsed
        onAddComponent={onAddComponent}
      />,
    );

    const addRuntime = screen.getByRole('button', {
      name: 'Add AgentCore Runtime to canvas',
    });
    addRuntime.focus();
    fireEvent.keyDown(addRuntime, { key: 'Enter' });
    fireEvent.click(addRuntime);

    expect(addRuntime).toHaveFocus();
    expect(onAddComponent).toHaveBeenCalledWith('runtime', undefined);
  });

  it('disables every canvas-mutating control while no flow is open, and says why', () => {
    const onAddComponent = vi.fn();
    const onOpenTemplates = vi.fn();
    render(
      <ComponentPalette
        onAddComponent={onAddComponent}
        onOpenTemplates={onOpenTemplates}
        onOpenRegistry={vi.fn()}
        onOpenAgentGenerator={vi.fn()}
        onOpenToolGenerator={vi.fn()}
        onOpenPromptLibrary={vi.fn()}
        authoringDisabledReason="Opening your flow…"
      />,
    );

    expect(screen.getByRole('status')).toHaveTextContent('Opening your flow…');

    const addRuntime = screen.getByRole('button', { name: 'Add AgentCore Runtime to canvas' });
    expect(addRuntime).toBeDisabled();
    fireEvent.click(addRuntime);
    expect(onAddComponent).not.toHaveBeenCalled();
    // The drag source is the add button's parent card; it must not start a drag.
    expect(addRuntime.parentElement).toHaveAttribute('draggable', 'false');

    const templates = screen.getByRole('button', { name: 'Browse templates' });
    expect(templates).toBeDisabled();
    fireEvent.click(templates);
    expect(onOpenTemplates).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Browse agent registry' })).toBeDisabled();
    expect(screen.getByRole('button', { name: /generate agent/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /ai tool generator/i })).toBeDisabled();

    // Read-only browsing stays available.
    expect(screen.getByRole('button', { name: 'Open prompt library' })).toBeEnabled();
  });

  it('keeps the collapsed add controls disabled while no flow is open', () => {
    const onAddComponent = vi.fn();
    render(
      <ComponentPalette
        collapsed
        onAddComponent={onAddComponent}
        authoringDisabledReason="Opening your flow…"
      />,
    );

    const addRuntime = screen.getByRole('button', { name: 'Add AgentCore Runtime to canvas' });
    expect(addRuntime).toBeDisabled();
    expect(addRuntime.parentElement).toHaveAttribute('draggable', 'false');
    fireEvent.click(addRuntime);
    expect(onAddComponent).not.toHaveBeenCalled();
  });

  it('enables the same controls once a flow is open', () => {
    render(
      <ComponentPalette
        onAddComponent={vi.fn()}
        onOpenTemplates={vi.fn()}
        authoringDisabledReason={null}
      />,
    );

    expect(screen.queryByRole('status')).toBeNull();
    const addRuntime = screen.getByRole('button', { name: 'Add AgentCore Runtime to canvas' });
    expect(addRuntime).toBeEnabled();
    expect(addRuntime.parentElement).toHaveAttribute('draggable', 'true');
    expect(screen.getByRole('button', { name: 'Browse templates' })).toBeEnabled();
  });

  it('uses theme-safe foreground and background tokens for category counts', () => {
    render(<ComponentPalette />);

    const computeCategory = screen.getByTestId('category-compute');
    const count = within(computeCategory).getByText('4');
    expect(count).toHaveStyle({
      color: 'var(--color-text-primary)',
      backgroundColor: 'var(--color-surface-hover)',
    });
  });

  it('keeps the agent-generator call to action on a high-contrast solid surface', () => {
    render(<ComponentPalette onOpenAgentGenerator={() => {}} />);

    expect(
      screen.getByRole('button', { name: 'Generate Agent (AI)' }),
    ).toHaveClass('bg-[#5b21b6]', 'text-white');
  });
});
