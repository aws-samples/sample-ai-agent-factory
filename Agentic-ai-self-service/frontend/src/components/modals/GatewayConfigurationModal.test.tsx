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
  listMcpServersApi: vi.fn().mockResolvedValue([]),
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

describe('GatewayConfigurationModal — custom MCP endpoint outbound auth', () => {
  /** An AgentCore gateway whose single target is a raw MCP endpoint — the shape
   *  used to wire a LiteLLM proxy as a TARGET rather than as the gateway. */
  const openCustomMcpRow = () => {
    render(
      <GatewayConfigurationModal
        isOpen
        onClose={() => {}}
        onSave={() => {}}
        initialConfig={{
          name: 'gw',
          targetType: 'mcp_server',
          targetConfig: { type: 'mcp_server', serverId: '__custom__', serverUrl: 'https://litellm.example.com/mcp/' },
          enableSemanticSearch: true,
        }}
      />
    );
    fireEvent.click(screen.getByTestId('tab-target'));
  };

  it('hides the key-shape controls until a custom endpoint uses an API key', () => {
    openCustomMcpRow();
    expect(screen.queryByTestId('field-apiKeyHeader_0')).toBeNull();
    fireEvent.change(screen.getByTestId('field-customAuthType_0'), { target: { value: 'api_key' } });
    expect(screen.getByTestId('field-apiKeyHeader_0')).toBeTruthy();
    expect((screen.getByTestId('field-apiKeyFormat_0') as HTMLSelectElement).value).toBe('bearer');
  });

  it('saves the header name and format so the deploy can build a descriptor', () => {
    const onSave = vi.fn();
    render(
      <GatewayConfigurationModal
        isOpen
        onClose={() => {}}
        onSave={onSave}
        initialConfig={{
          name: 'gw',
          targetType: 'mcp_server',
          targetConfig: { type: 'mcp_server', serverId: '__custom__', serverUrl: 'https://litellm.example.com/mcp/' },
          enableSemanticSearch: true,
        }}
      />
    );
    fireEvent.click(screen.getByTestId('tab-target'));
    fireEvent.change(screen.getByTestId('field-customAuthType_0'), { target: { value: 'api_key' } });
    fireEvent.change(screen.getByTestId('field-apiKeyHeader_0'), { target: { value: 'x-litellm-api-key' } });
    fireEvent.change(screen.getByTestId('field-apiKeyFormat_0'), { target: { value: 'raw' } });
    fireEvent.click(screen.getByTestId('modal-save-button'));

    const saved = onSave.mock.calls[0][0] as GatewayConfiguration;
    const target = saved.targets?.[0] as { apiKeyHeader?: string; apiKeyFormat?: string };
    expect(target.apiKeyHeader).toBe('x-litellm-api-key');
    expect(target.apiKeyFormat).toBe('raw');
  });
});

describe('GatewayConfigurationModal — gateway provider (Workstream A)', () => {
  const litellmInitial: Partial<GatewayConfiguration> = {
    name: 'litellm-gw',
    gatewayProvider: 'litellm',
    litellmBaseUrl: 'https://litellm.example.com',
    litellmApiKey: 'sk-test',
    enableSemanticSearch: true,
  };

  it('defaults to AgentCore and shows the target editor', () => {
    render(
      <GatewayConfigurationModal
        isOpen
        onClose={() => {}}
        onSave={() => {}}
        initialConfig={{ name: 'gw', targetType: 'lambda', targetConfig: { type: 'lambda', functionArn: '' }, enableSemanticSearch: true }}
      />
    );
    expect((screen.getByTestId('field-gatewayProvider') as HTMLSelectElement).value).toBe('agentcore');
    expect(screen.getByTestId('tab-target')).toBeTruthy();
    expect(screen.queryByTestId('tab-litellm')).toBeNull();
  });

  it('swaps the target tabs for a LiteLLM tab when the provider changes', () => {
    render(
      <GatewayConfigurationModal isOpen onClose={() => {}} onSave={() => {}} initialConfig={{ name: 'gw', targetType: 'lambda', targetConfig: { type: 'lambda', functionArn: '' }, enableSemanticSearch: true }} />
    );
    fireEvent.change(screen.getByTestId('field-gatewayProvider'), { target: { value: 'litellm' } });

    // Targets and semantic search are AgentCore-only; leaving them visible would
    // imply they still do something.
    expect(screen.queryByTestId('tab-target')).toBeNull();
    expect(screen.queryByTestId('tab-advanced')).toBeNull();
    fireEvent.click(screen.getByTestId('tab-litellm'));
    expect(screen.getByTestId('gateway-litellm')).toBeTruthy();
  });

  it('renders the LiteLLM tab for a stored LiteLLM node', () => {
    render(
      <GatewayConfigurationModal isOpen onClose={() => {}} onSave={() => {}} initialConfig={litellmInitial} />
    );
    fireEvent.click(screen.getByTestId('tab-litellm'));
    expect((screen.getByTestId('field-litellmBaseUrl') as HTMLInputElement).value).toBe(
      'https://litellm.example.com'
    );
  });

  it('masks the virtual key input', () => {
    // It is a live credential typed into a browser; `type=password` is the
    // minimum, and the field is write-only server-side.
    render(
      <GatewayConfigurationModal isOpen onClose={() => {}} onSave={() => {}} initialConfig={litellmInitial} />
    );
    fireEvent.click(screen.getByTestId('tab-litellm'));
    expect((screen.getByTestId('field-litellmApiKey') as HTMLInputElement).type).toBe('password');
  });

  it('saves base URL, key and comma-split server aliases', () => {
    const onSave = vi.fn();
    render(
      <GatewayConfigurationModal isOpen onClose={() => {}} onSave={onSave} initialConfig={litellmInitial} />
    );
    fireEvent.click(screen.getByTestId('tab-litellm'));
    fireEvent.change(screen.getByTestId('field-litellmServers'), {
      target: { value: 'github, jira ,, ' },
    });
    fireEvent.click(screen.getByTestId('modal-save-button'));

    expect(onSave).toHaveBeenCalledTimes(1);
    const saved = onSave.mock.calls[0][0] as GatewayConfiguration;
    expect(saved.gatewayProvider).toBe('litellm');
    expect(saved.litellmBaseUrl).toBe('https://litellm.example.com');
    // Blanks dropped and each alias trimmed — a stray comma would otherwise
    // become an empty alias the proxy cannot resolve.
    expect(saved.litellmServers).toEqual(['github', 'jira']);
  });

  it('blocks save until a base URL is supplied', () => {
    const onSave = vi.fn();
    render(
      <GatewayConfigurationModal
        isOpen
        onClose={() => {}}
        onSave={onSave}
        initialConfig={{ ...litellmInitial, litellmBaseUrl: '' }}
      />
    );
    fireEvent.click(screen.getByTestId('modal-save-button'));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('names the provider in the modal title', () => {
    render(
      <GatewayConfigurationModal isOpen onClose={() => {}} onSave={() => {}} initialConfig={litellmInitial} />
    );
    expect(screen.getByText('Configure LiteLLM MCP Gateway')).toBeTruthy();
  });
});
