/**
 * GatewayConfigurationModal — multi-target editor tests.
 *
 * Proves the modal can render and edit MULTIPLE targets of different families on
 * ONE gateway (the new `targets[]` array), while staying backward-compatible with
 * a single-target initial config.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { GatewayConfigurationModal } from './GatewayConfigurationModal';
import type { GatewayConfiguration } from '../../types/components';

// The MCP catalog fetch is lazy + async; a resolved empty list keeps the
// mcp_server row rendering its "Custom endpoint…" option without network.
vi.mock('../../services/api', () => ({
  listMcpServers: vi.fn().mockResolvedValue([]),
}));

describe('GatewayConfigurationModal — multiple targets', () => {
  it('seeds a single target row from a legacy single-target config', () => {
    const initial: Partial<GatewayConfiguration> = {
      name: 'legacy-gw',
      targetType: 'lambda',
      targetConfig: { type: 'lambda', functionArn: '' },
      enableSemanticSearch: true,
    };
    render(<GatewayConfigurationModal isOpen onClose={() => {}} onSave={() => {}} initialConfig={initial} />);
    fireEvent.click(screen.getByTestId('tab-target'));
    expect(screen.getByTestId('target-row-0')).toBeTruthy();
    expect(screen.queryByTestId('target-row-1')).toBeNull();
  });

  it('adds targets and saves them as a mixed targets[] array', () => {
    const onSave = vi.fn();
    const initial: Partial<GatewayConfiguration> = {
      name: 'multi-gw',
      targetType: 'lambda',
      targetConfig: { type: 'lambda', functionArn: 'arn:aws:lambda:us-west-2:123456789012:function:a' },
      enableSemanticSearch: true,
    };
    render(<GatewayConfigurationModal isOpen onClose={() => {}} onSave={onSave} initialConfig={initial} />);

    fireEvent.click(screen.getByTestId('tab-target'));

    // Row 0 is a Lambda from the initial config — fill its ARN.
    fireEvent.change(screen.getByTestId('field-functionArn_0'), {
      target: { value: 'arn:aws:lambda:us-west-2:123456789012:function:a' },
    });

    // Add a 2nd target and make it OpenAPI.
    fireEvent.click(screen.getByTestId('add-target'));
    expect(screen.getByTestId('target-row-1')).toBeTruthy();
    fireEvent.change(screen.getByTestId('field-targetType_1'), { target: { value: 'openapi' } });
    fireEvent.change(screen.getByTestId('field-specUrl_1'), {
      target: { value: 'https://api.example.com/openapi.json' },
    });

    // Add a 3rd target and make it Smithy.
    fireEvent.click(screen.getByTestId('add-target'));
    fireEvent.change(screen.getByTestId('field-targetType_2'), { target: { value: 'smithy' } });

    fireEvent.click(screen.getByTestId('modal-save-button'));

    expect(onSave).toHaveBeenCalledTimes(1);
    const saved = onSave.mock.calls[0][0] as GatewayConfiguration;
    expect(saved.targets).toHaveLength(3);
    expect(saved.targets?.map((t) => t.type)).toEqual(['lambda', 'openapi', 'smithy']);
    // Legacy single-target fields mirror targets[0] for backward compat.
    expect(saved.targetType).toBe('lambda');
    expect(saved.targetConfig.type).toBe('lambda');
  });

  it('removes a target row (and cannot remove the last one)', () => {
    const onSave = vi.fn();
    const initial: Partial<GatewayConfiguration> = {
      name: 'gw',
      targets: [
        { type: 'lambda', functionArn: 'arn:aws:lambda:us-west-2:123456789012:function:a' },
        { type: 'smithy', modelName: 'dynamodb' },
      ],
      targetType: 'lambda',
      targetConfig: { type: 'lambda', functionArn: 'arn:aws:lambda:us-west-2:123456789012:function:a' },
      enableSemanticSearch: true,
    };
    render(<GatewayConfigurationModal isOpen onClose={() => {}} onSave={onSave} initialConfig={initial} />);
    fireEvent.click(screen.getByTestId('tab-target'));

    // Two rows seeded from targets[].
    expect(screen.getByTestId('target-row-1')).toBeTruthy();

    // Remove the second one.
    fireEvent.click(screen.getByTestId('remove-target-1'));
    expect(screen.queryByTestId('target-row-1')).toBeNull();

    // The remaining single row's remove button is disabled.
    expect((screen.getByTestId('remove-target-0') as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByTestId('modal-save-button'));
    const saved = onSave.mock.calls[0][0] as GatewayConfiguration;
    expect(saved.targets).toHaveLength(1);
    expect(saved.targets?.[0].type).toBe('lambda');
  });
});
