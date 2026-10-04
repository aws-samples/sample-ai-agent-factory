import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { PromptSelection } from './PromptLibraryModal';
import { RuntimeConfigurationModal } from './RuntimeConfigurationModal';

vi.mock('../../auth/authFetch', () => ({
  authFetch: vi.fn(() => new Promise(() => {})),
}));

describe('RuntimeConfigurationModal prompt library', () => {
  it('applies the selected library prompt to the system prompt field', () => {
    let selectPrompt: ((selection: PromptSelection) => void) | undefined;
    const onOpenPromptLibrary = vi.fn((onSelect) => {
      selectPrompt = onSelect;
    });

    render(
      <RuntimeConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
        onOpenPromptLibrary={onOpenPromptLibrary}
      />,
    );

    fireEvent.click(screen.getByRole('tab', { name: /System Prompt/ }));
    fireEvent.click(
      screen.getByRole('button', { name: 'Use from prompt library' }),
    );

    expect(onOpenPromptLibrary).toHaveBeenCalledTimes(1);

    act(() => {
      selectPrompt?.({
        promptName: 'support-agent',
        versionId: 'v3',
        body: 'Answer support questions with concise, cited steps.',
      });
    });

    expect(
      screen.getByRole('textbox', { name: /System Prompt/ }),
    ).toHaveValue('Answer support questions with concise, cited steps.');
  });
});
