import { describe, expect, it } from 'vitest';
import { WORKFLOW_TEMPLATES } from '../data/templates';
import type { RuntimeConfiguration } from '../types/components';
import {
  MODEL_ONLY_RUNTIME_FIELDS,
  runtimeConfigForRequest,
} from './runtimeRequestConfig';
import { validateComponentConfiguration } from './validation';

const modelConfig: RuntimeConfiguration = {
  name: 'standalone-mcp',
  entrypoint: '',
  framework: 'strands_agents',
  model: {
    provider: 'bedrock',
    modelId: 'us.anthropic.claude-sonnet-5',
    temperature: 0.7,
    topP: 0.9,
  },
  systemPrompt: 'This field must not cross the MCP request boundary.',
  deploymentType: 'direct_code_deploy',
  pythonRuntime: 'PYTHON_3_13',
  protocol: 'MCP',
  idleTimeout: 300,
  maxLifetime: 3600,
  enableOtel: false,
  modelProvider: 'bedrock',
  providerApiKeyRef:
    'arn:aws:secretsmanager:us-east-1:111122223333:secret:model-key',
  providerBaseUrl: 'https://models.example.com/v1',
  multiAgentPattern: 'graph',
  multiAgentConfig: { agents: [] },
};

describe('runtimeConfigForRequest', () => {
  it('removes every model-only field for the standalone MCP template', () => {
    const result = runtimeConfigForRequest(modelConfig, 'mcp-server-runtime');

    expect(result).toMatchObject({
      name: 'standalone-mcp',
      entrypoint: 'agent.py',
      protocol: 'MCP',
      idleTimeout: 300,
      maxLifetime: 3600,
      enableOtel: false,
    });
    for (const field of MODEL_ONLY_RUNTIME_FIELDS) {
      expect(result).not.toHaveProperty(field);
    }
  });

  it('does not sanitize a generic template that merely claims MCP', () => {
    const result = runtimeConfigForRequest(modelConfig, 'web-search-agent');

    expect(result.model?.modelId).toBe('us.anthropic.claude-sonnet-5');
    expect(result.systemPrompt).toContain('must not cross');
    expect(result.modelProvider).toBe('bedrock');
    expect(result.multiAgentPattern).toBe('graph');
  });

  it('keeps the gallery template itself model-free', () => {
    const template = WORKFLOW_TEMPLATES.find(
      ({ id }) => id === 'mcp-server-runtime',
    );
    expect(template).toBeDefined();
    const config = template?.nodes[0].configuration as unknown as Record<
      string,
      unknown
    >;

    expect(config.protocol).toBe('MCP');
    for (const field of MODEL_ONLY_RUNTIME_FIELDS) {
      expect(config).not.toHaveProperty(field);
    }

    const validation = validateComponentConfiguration(
      'standalone-mcp',
      'runtime',
      config as unknown as RuntimeConfiguration,
    );
    expect(validation.errors).toEqual([]);
    expect(validation.warnings).toEqual([]);
    expect(validation.status).toBe('valid');
  });
});
