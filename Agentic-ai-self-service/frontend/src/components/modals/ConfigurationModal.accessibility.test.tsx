import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ConfigurationModal } from './ConfigurationModal';

describe('ConfigurationModal tab accessibility', () => {
  it('can block Save while an external policy is unresolved without inventing a validation error', () => {
    const onSave = vi.fn();

    render(
      <ConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={onSave}
        title="Policy-dependent settings"
        tabs={[{ id: 'only', label: 'Only', content: <p>Checking policy</p> }]}
        isSaveDisabled
      />,
    );

    const save = screen.getByTestId('modal-save-button');
    expect(save).toBeDisabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    fireEvent.click(save);
    expect(onSave).not.toHaveBeenCalled();
  });

  it('uses roving focus and arrow, Home, and End navigation', () => {
    render(
      <ConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
        title="Runtime settings"
        tabs={[
          { id: 'general', label: 'General', content: <p>General fields</p> },
          { id: 'network', label: 'Network', content: <p>Network fields</p> },
          {
            id: 'security',
            label: 'Security',
            content: <p>Security fields</p>,
            hasError: true,
          },
        ]}
      />,
    );

    const general = screen.getByRole('tab', { name: 'General' });
    const network = screen.getByRole('tab', { name: 'Network' });
    const security = screen.getByRole('tab', { name: /Security/ });

    expect(general).toHaveAttribute('aria-selected', 'true');
    expect(general).toHaveAttribute('tabindex', '0');
    expect(network).toHaveAttribute('tabindex', '-1');
    expect(screen.getByRole('tabpanel', { name: 'General' })).toHaveTextContent(
      'General fields',
    );

    fireEvent.keyDown(general, { key: 'ArrowRight' });
    expect(network).toHaveAttribute('aria-selected', 'true');
    expect(document.activeElement).toBe(network);
    expect(screen.getByRole('tabpanel', { name: 'Network' })).toHaveTextContent(
      'Network fields',
    );

    fireEvent.keyDown(network, { key: 'End' });
    expect(document.activeElement).toBe(security);
    expect(security).toHaveAttribute('aria-selected', 'true');

    fireEvent.keyDown(security, { key: 'Home' });
    expect(document.activeElement).toBe(general);
    expect(general).toHaveAttribute('aria-selected', 'true');
  });

  it('does not advertise a one-section modal as a tab interface', () => {
    render(
      <ConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
        title="Simple settings"
        tabs={[{ id: 'only', label: 'Only', content: <p>Only fields</p> }]}
      />,
    );

    expect(screen.queryByRole('tablist')).toBeNull();
    expect(screen.queryByRole('tabpanel')).toBeNull();
    expect(screen.getByText('Only fields')).toBeInTheDocument();
  });
});
