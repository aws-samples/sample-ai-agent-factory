import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { A2AConfigurationModal } from './A2AConfigurationModal';
import { GuardrailsConfigurationModal } from './GuardrailsConfigurationModal';

describe('A2A configuration control names', () => {
  it('names the list-entry fields on both list tabs', () => {
    render(
      <A2AConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('tab', { name: 'Capabilities' }));
    expect(screen.getByRole('textbox', { name: 'New agent capability' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
    expect(screen.getByRole('textbox', { name: 'Peer base URL' })).toBeInTheDocument();
  });
});

describe('Guardrails configuration control semantics', () => {
  it('announces filter groups and their selected strength', () => {
    render(
      <GuardrailsConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('tab', { name: 'Content Filters' }));
    const hateSpeech = screen.getByRole('group', { name: 'Hate Speech' });
    const high = within(hateSpeech).getByRole('button', { name: 'HIGH' });
    const low = within(hateSpeech).getByRole('button', { name: 'LOW' });

    expect(high).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(low);
    expect(low).toHaveAttribute('aria-pressed', 'true');
    expect(high).toHaveAttribute('aria-pressed', 'false');
  });

  it('keeps a PII action independent from its checkbox and names dynamic fields', () => {
    render(
      <GuardrailsConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('tab', { name: 'PII & Words' }));

    const email = screen.getByRole('checkbox', { name: 'EMAIL' });
    fireEvent.click(email);
    const action = screen.getByRole('combobox', { name: 'EMAIL handling action' });
    fireEvent.change(action, { target: { value: 'BLOCK' } });

    expect(email).toBeChecked();
    expect(action).toHaveValue('BLOCK');
    expect(
      screen.getByRole('textbox', { name: 'Comma-separated list of words to block' }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '+ Add Denied Topic' }));
    expect(screen.getByRole('textbox', { name: 'Denied topic 1 name' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Denied topic 1 definition' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Remove denied topic 1' })).toBeInTheDocument();
  });
});
