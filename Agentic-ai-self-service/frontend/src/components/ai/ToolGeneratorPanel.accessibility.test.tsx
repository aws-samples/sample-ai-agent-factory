import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ToolGeneratorPanel } from './ToolGeneratorPanel';

describe('ToolGeneratorPanel accessibility', () => {
  it('makes the parked off-screen drawer inert and absent from the dialog tree', () => {
    render(
      <ToolGeneratorPanel
        isVisible={false}
        onClose={vi.fn()}
        onAddToolToCanvas={vi.fn()}
      />,
    );

    const panel = screen.getByTestId('tool-generator-panel');
    expect(panel).toHaveAttribute('inert');
    expect(panel).toHaveAttribute('aria-hidden', 'true');
    expect(screen.queryByRole('dialog', { name: 'AI Tool Generator' })).toBeNull();
  });

  it('is a named modal drawer, owns focus, wraps Tab, and closes on Escape', async () => {
    const onClose = vi.fn();
    const view = render(
      <>
        <button type="button">Open tool generator</button>
        <ToolGeneratorPanel
          isVisible={false}
          onClose={onClose}
          onAddToolToCanvas={vi.fn()}
        />
      </>,
    );
    const opener = screen.getByRole('button', { name: 'Open tool generator' });
    opener.focus();

    view.rerender(
      <>
        <button type="button">Open tool generator</button>
        <ToolGeneratorPanel isVisible onClose={onClose} onAddToolToCanvas={vi.fn()} />
      </>,
    );

    const dialog = screen.getByRole('dialog', { name: 'AI Tool Generator' });
    const panel = screen.getByTestId('tool-generator-panel');
    const input = screen.getByPlaceholderText(/describe a tool/i);
    expect(
      within(dialog).getByRole('heading', { name: 'AI Tool Generator', level: 2 }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole('heading', { name: 'Create Tools with AI', level: 3 }),
    ).toBeInTheDocument();
    expect(panel).not.toHaveAttribute('inert');
    expect(panel).toHaveAttribute('aria-hidden', 'false');
    expect(panel).toHaveAttribute('aria-modal', 'true');
    await waitFor(() => expect(document.activeElement).toBe(input));

    const focusable = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]):not([tabindex="-1"]), textarea:not([disabled]), [tabindex="0"]',
      ),
    );
    const first = focusable[0];
    const last = focusable[focusable.length - 1];

    last.focus();
    fireEvent.keyDown(last, { key: 'Tab' });
    expect(document.activeElement).toBe(first);

    opener.focus();
    expect(dialog.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
