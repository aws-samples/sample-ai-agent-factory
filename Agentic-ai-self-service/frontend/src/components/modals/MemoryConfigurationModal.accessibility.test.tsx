import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MemoryConfigurationModal } from './MemoryConfigurationModal';

describe('MemoryConfigurationModal accessibility', () => {
  it('names and describes the event expiry selector', () => {
    render(
      <MemoryConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    expect(
      screen.getByRole('combobox', { name: 'Event Expiry Duration' }),
    ).toHaveAccessibleDescription(
      'How long raw conversation events are retained (3–365 days)',
    );
  });
});
