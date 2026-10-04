import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ModalShell } from './ModalShell';

function Harness({ isOpen }: { isOpen: boolean }) {
  return (
    <>
      <button type="button">Open settings</button>
      <ModalShell isOpen={isOpen} onClose={vi.fn()} title="Runtime settings">
        <div>
          <button type="button">First action</button>
          <button type="button">Last action</button>
          <div inert>
            <button type="button">Parked action</button>
          </div>
        </div>
      </ModalShell>
    </>
  );
}

describe('ModalShell focus containment', () => {
  it('wraps Tab in both directions, blocks outside focus, and restores the opener', async () => {
    const view = render(<Harness isOpen={false} />);
    const opener = screen.getByRole('button', { name: 'Open settings' });
    opener.focus();

    view.rerender(<Harness isOpen />);
    const dialog = screen.getByRole('dialog', { name: 'Runtime settings' });
    const first = within(dialog).getByRole('button', { name: 'Close modal' });
    const last = within(dialog).getByRole('button', { name: 'Last action' });

    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    last.focus();
    fireEvent.keyDown(last, { key: 'Tab' });
    expect(document.activeElement).toBe(first);

    first.focus();
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(last);

    opener.focus();
    expect(dialog.contains(document.activeElement)).toBe(true);

    view.rerender(<Harness isOpen={false} />);
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });
});
