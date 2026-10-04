import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PolicyConfigurationModal } from './PolicyConfigurationModal';

describe('PolicyConfigurationModal accessibility', () => {
  it('names and describes policy selectors and resource fields', () => {
    render(
      <PolicyConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    expect(
      screen.getByRole('combobox', { name: 'Default Effect' }),
    ).toHaveAccessibleDescription('The default effect when no policy matches');

    fireEvent.click(screen.getByRole('tab', { name: 'Policy Rules' }));

    expect(screen.getByRole('combobox', { name: 'Effect' })).toBeInTheDocument();
    expect(
      screen.getByRole('textbox', { name: 'Resource (optional)' }),
    ).toHaveAccessibleDescription(
      'Leave empty to auto-fill with the deployed gateway ARN',
    );
  });
});
