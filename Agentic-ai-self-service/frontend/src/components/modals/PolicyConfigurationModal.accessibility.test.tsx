import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PolicyConfigurationModal } from './PolicyConfigurationModal';
import * as awsRegion from '../../utils/awsRegion';

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

describe('PolicyConfigurationModal Gateway guardrails profile', () => {
  it('defaults to off, saves a profile object only for standard/strict, and clears it for off', () => {
    const onSave = vi.fn();
    render(<PolicyConfigurationModal isOpen onClose={vi.fn()} onSave={onSave} />);

    const select = screen.getByRole('combobox', { name: 'Gateway guardrails profile' });
    expect(select).toHaveValue('off');
    expect(
      screen.getByRole('option', { name: /Standard - deny prompt injection/ }),
    ).not.toBeDisabled();

    fireEvent.change(select, { target: { value: 'strict' } });
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    expect(onSave).toHaveBeenLastCalledWith(
      expect.objectContaining({ guardrails: { profile: 'strict' } }),
    );

    fireEvent.change(select, { target: { value: 'off' } });
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    expect(onSave.mock.calls.at(-1)?.[0].guardrails).toBeUndefined();
  });

  it('shows a saved profile back and disables both profiles with the Region reason where AWS lacks them', () => {
    const spy = vi
      .spyOn(awsRegion, 'gatewayGuardrailsUnavailableReason')
      .mockReturnValue(awsRegion.gatewayGuardrailsUnavailableReason('eu-west-1'));
    try {
      render(
        <PolicyConfigurationModal
          isOpen
          onClose={vi.fn()}
          onSave={vi.fn()}
          initialConfig={{ guardrails: { profile: 'standard' } }}
        />,
      );
      expect(screen.getByRole('combobox', { name: 'Gateway guardrails profile' })).toHaveValue('standard');
      expect(screen.getByRole('option', { name: /Standard - deny prompt injection/ })).toBeDisabled();
      expect(screen.getByRole('option', { name: /Strict - Standard/ })).toBeDisabled();
      expect(screen.getByRole('option', { name: /Off - Cedar/ })).not.toBeDisabled();
      // The saved-but-unavailable profile is a validation error with the backend's exact text.
      expect(screen.getByTestId('error-policy-gateway-guardrails')).toHaveTextContent(
        /Amazon Bedrock Guardrails in AgentCore Policy are not available in Europe \(Ireland\) \(eu-west-1\)/,
      );
    } finally {
      spy.mockRestore();
    }
  });
});
