import { describe, expect, it } from 'vitest';
import { stripWriteOnlyCredentials, isWriteOnlyCredentialKey } from './credentialScrub';
import { createRegistryCanvasSnapshot } from '../services/api/registry';
import { WorkflowSerializer } from './serialization';
import { normalizeDeploymentGovernance } from '../types/workflow';

// The P0: a LiteLLM virtual key typed into the gateway modal was autosaved and stored in DynamoDB in
// plaintext. Values never persist; references do.
const SENTINEL = 'sk-matrix-sentinel-NEVER-PERSIST-0123456789';
const CASES: Array<[string, Record<string, unknown>]> = [
  ['litellm key', { litellmApiKey: SENTINEL, litellmApiKeyRef: 'arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf' }],
  ['mcp target apiKey', { targets: [{ targetType: 'mcp', targetConfig: { apiKey: SENTINEL, serverId: 'github' } }] }],
  ['oauth clientSecret', { oauth2Config: { clientId: 'abc', clientSecret: SENTINEL, tokenUrl: 'https://idp.example/token' } }],
  ['connector secretValue', { connectors: [{ name: 'jira', secretValue: SENTINEL, secretRef: 'arn:aws:secretsmanager:us-east-1:123456789012:secret:jira-XyZ' }] }],
  ['snake_case spelling', { litellm_api_key: SENTINEL, client_secret: SENTINEL, api_key: SENTINEL }],
];

describe('stripWriteOnlyCredentials', () => {
  it.each(CASES)('%s: drops the value at any depth and keeps references', (_label, config) => {
    const out = stripWriteOnlyCredentials(config);
    const text = JSON.stringify(out);
    expect(text).not.toContain(SENTINEL);
    if (JSON.stringify(config).includes('secretsmanager')) expect(text).toContain('secretsmanager');
  });

  it('is a deep copy that leaves non-credential keys and the input untouched', () => {
    const src = { a: { b: [{ apiKey: 'x', keep: 1, apiKeyRef: 'r' }] }, password: 'p', name: 'n' };
    expect(stripWriteOnlyCredentials(src)).toEqual({ a: { b: [{ keep: 1, apiKeyRef: 'r' }] }, name: 'n' });
    expect(src.password).toBe('p');
  });

  it('covers both spellings of every documented key', () => {
    for (const camel of ['litellmApiKey', 'apiKey', 'clientSecret', 'secretValue', 'accessToken', 'refreshToken', 'privateKey', 'bearerToken']) {
      const snake = camel.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`);
      expect(isWriteOnlyCredentialKey(camel)).toBe(true);
      expect(isWriteOnlyCredentialKey(snake)).toBe(true);
    }
    expect(isWriteOnlyCredentialKey('litellmApiKeyRef')).toBe(false);
  });
});

describe('createRegistryCanvasSnapshot', () => {
  it.each(CASES)('%s: a published snapshot never carries the value', (_label, config) => {
    const snap = createRegistryCanvasSnapshot('n', [{ id: 'gw-1', type: 'gateway', data: { componentType: 'gateway', configuration: config } }], [], { x: 0, y: 0, zoom: 1 }, normalizeDeploymentGovernance({}));
    expect(JSON.stringify(snap)).not.toContain(SENTINEL);
  });
});

describe('WorkflowSerializer', () => {
  it.each(CASES)('%s: a serialized canvas never carries the value', (_label, config) => {
    const node = { id: 'gw-1', type: 'gateway', position: { x: 0, y: 0 }, data: { label: 'Gateway', componentType: 'gateway', configuration: config, validationStatus: 'pending' } } as never;
    const json = WorkflowSerializer.serialize([node], [], { name: 'n', description: '', version: '1.0.0' } as never);
    expect(json).not.toContain(SENTINEL);
  });
});
