import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ConfirmDialog } from './ConfirmDialog';

describe('ConfirmDialog focus and Escape ownership', () => {
  it('owns focus as the topmost dialog and keeps Escape from closing its parent', async () => {
    const onCancel = vi.fn();
    const parentEscape = vi.fn();
    const parentKeydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        parentEscape();
      }
    };
    document.addEventListener('keydown', parentKeydown);

    try {
      render(
        <>
          <button type="button">Outside action</button>
          <ConfirmDialog
            isOpen
            title="Replace workflow?"
            message="The current workflow will be replaced."
            confirmLabel="Replace"
            cancelLabel="Keep"
            variant="danger"
            onConfirm={vi.fn()}
            onCancel={onCancel}
          />
        </>,
      );

      const dialog = screen.getByRole('dialog', { name: 'Replace workflow?' });
      const keep = within(dialog).getByRole('button', { name: 'Keep' });
      const replace = within(dialog).getByRole('button', { name: 'Replace' });
      const outside = screen.getByRole('button', { name: 'Outside action' });

      // Destructive confirmations start on the safe action.
      await waitFor(() => expect(document.activeElement).toBe(keep));

      fireEvent.keyDown(keep, { key: 'Tab', shiftKey: true });
      expect(document.activeElement).toBe(replace);

      fireEvent.keyDown(replace, { key: 'Tab' });
      expect(document.activeElement).toBe(keep);

      outside.focus();
      expect(dialog.contains(document.activeElement)).toBe(true);

      const laterCaptureListener = vi.fn();
      document.addEventListener('keydown', laterCaptureListener, true);
      try {
        fireEvent.keyDown(document, { key: 'Escape' });
        expect(onCancel).toHaveBeenCalledTimes(1);
        expect(parentEscape).not.toHaveBeenCalled();
        expect(laterCaptureListener).not.toHaveBeenCalled();
      } finally {
        document.removeEventListener('keydown', laterCaptureListener, true);
      }
    } finally {
      document.removeEventListener('keydown', parentKeydown);
    }
  });

  it('cancels only when the pointer press originates on the dialog surround', () => {
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        isOpen
        title="Delete runtime?"
        message="This action cannot be undone."
        variant="danger"
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: 'Delete runtime?' });
    const message = screen.getByText('This action cannot be undone.');
    fireEvent.pointerDown(message);
    expect(onCancel).not.toHaveBeenCalled();

    // Releasing a drag over the surround must not turn a panel-originated press
    // into a cancellation.
    fireEvent.pointerUp(dialog);
    expect(onCancel).not.toHaveBeenCalled();

    fireEvent.pointerDown(dialog);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
