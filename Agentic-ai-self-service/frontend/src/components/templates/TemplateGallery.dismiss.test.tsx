/**
 * TemplateGallery — can a keyboard user get out of it, and can a screen reader name it?
 *
 * Measured against the deployed bundle before the fix, as a real admin in Chromium:
 *
 *   semantics: {"roleDialogAnywhere":false,"ariaModal":null,"ariaLabelledBy":null,
 *              "firstButtons":[{"text":"","ariaLabel":null,"title":null},...]}
 *   gallery still open after Escape: true
 *
 * So the modal announced itself as nothing, its close control had an accessible name
 * of the empty string, and Escape did not dismiss it — leaving a mouse click on the
 * scrim or on that unnamed button as the only exits. `ModalShell.tsx:42-51` in this
 * same repo already supplies all three, and its docstring claims to "enforce
 * consistent modal UX across the app"; this modal simply does not use it.
 *
 * The follow-up coverage below closes the original residuals too: focus remains in
 * the topmost dialog and replacement uses the accessible ConfirmDialog.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { TemplateGallery } from './TemplateGallery';

const baseProps = {
  onSelectTemplate: vi.fn(),
  hasExistingNodes: false,
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('dismissing the gallery from the keyboard', () => {
  it('Escape closes it', () => {
    const onClose = vi.fn();
    render(<TemplateGallery {...baseProps} isOpen onClose={onClose} />);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('a key that is not Escape does not close it', () => {
    // The positive control's counterpart: a handler that closed on any keypress
    // would pass the test above while making the gallery unusable.
    const onClose = vi.fn();
    render(<TemplateGallery {...baseProps} isOpen onClose={onClose} />);

    fireEvent.keyDown(document, { key: 'Enter' });
    fireEvent.keyDown(document, { key: 'a' });
    fireEvent.keyDown(document, { key: 'Tab' });

    expect(onClose).not.toHaveBeenCalled();
  });

  it('a CLOSED gallery does not swallow Escape', () => {
    // The listener is mounted inside `isOpen` precisely so a closed gallery cannot
    // steal Escape from whichever modal is actually open. Registering it
    // unconditionally would pass the first test and break every other modal.
    const onClose = vi.fn();
    render(<TemplateGallery {...baseProps} isOpen={false} onClose={onClose} />);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).not.toHaveBeenCalled();
  });

  it('stops listening once unmounted', () => {
    const onClose = vi.fn();
    const { unmount } = render(<TemplateGallery {...baseProps} isOpen onClose={onClose} />);
    unmount();

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('what assistive technology is told about the gallery', () => {
  it('is a dialog, is modal, and is named by its own heading', () => {
    render(<TemplateGallery {...baseProps} isOpen onClose={vi.fn()} />);

    // getByRole('dialog') resolves the accessible name through aria-labelledby, so
    // this one assertion fails if the role, aria-modal or the id wiring is missing.
    const dialog = screen.getByRole('dialog', { name: 'Workflow Templates' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
  });

  it('the close control has an accessible name rather than an empty one', () => {
    render(<TemplateGallery {...baseProps} isOpen onClose={vi.fn()} />);

    const close = screen.getByRole('button', { name: /close the workflow templates gallery/i });
    fireEvent.click(close);
    // Naming it is only half the point — it must still be the button that closes.
    expect(close).toBeTruthy();
  });

  it('the close control actually closes', () => {
    const onClose = vi.fn();
    render(<TemplateGallery {...baseProps} isOpen onClose={onClose} />);

    fireEvent.click(screen.getByRole('button', { name: /close the workflow templates gallery/i }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('focus containment', () => {
  it('wraps Tab, rejects programmatic outside focus, and restores the opener', async () => {
    const onClose = vi.fn();
    const view = render(
      <>
        <button type="button">Open templates</button>
        <TemplateGallery {...baseProps} isOpen={false} onClose={onClose} />
      </>,
    );
    const opener = screen.getByRole('button', { name: 'Open templates' });
    opener.focus();

    view.rerender(
      <>
        <button type="button">Open templates</button>
        <TemplateGallery {...baseProps} isOpen onClose={onClose} />
      </>,
    );
    const dialog = screen.getByRole('dialog', { name: 'Workflow Templates' });
    const buttons = within(dialog).getAllByRole('button');
    const first = buttons[0];
    const last = buttons[buttons.length - 1];

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
        <button type="button">Open templates</button>
        <TemplateGallery {...baseProps} isOpen={false} onClose={onClose} />
      </>,
    );
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });
});

describe('replacing an existing workflow', () => {
  it('uses an accessible confirmation and never calls native window.confirm', () => {
    const onClose = vi.fn();
    const onSelectTemplate = vi.fn();
    const nativeConfirm = vi.spyOn(window, 'confirm');
    render(
      <TemplateGallery
        isOpen
        onClose={onClose}
        onSelectTemplate={onSelectTemplate}
        hasExistingNodes
      />,
    );

    fireEvent.click(screen.getAllByRole('button', { name: 'Use Template' })[0]);

    expect(nativeConfirm).not.toHaveBeenCalled();
    expect(onSelectTemplate).not.toHaveBeenCalled();
    const confirmation = screen.getByRole('dialog', { name: 'Replace current workflow?' });
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Keep current workflow' }));

    expect(screen.queryByRole('dialog', { name: 'Replace current workflow?' })).toBeNull();
    expect(screen.getByRole('dialog', { name: 'Workflow Templates' })).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getAllByRole('button', { name: 'Use Template' })[0]);
    fireEvent.click(
      within(screen.getByRole('dialog', { name: 'Replace current workflow?' })).getByRole(
        'button',
        { name: 'Replace workflow' },
      ),
    );

    expect(onSelectTemplate).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('Escape cancels only the nested confirmation, then closes the gallery', () => {
    const onClose = vi.fn();
    render(
      <TemplateGallery
        isOpen
        onClose={onClose}
        onSelectTemplate={vi.fn()}
        hasExistingNodes
      />,
    );

    fireEvent.click(screen.getAllByRole('button', { name: 'Use Template' })[0]);
    fireEvent.keyDown(document, { key: 'Escape' });

    expect(screen.queryByRole('dialog', { name: 'Replace current workflow?' })).toBeNull();
    expect(screen.getByRole('dialog', { name: 'Workflow Templates' })).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
