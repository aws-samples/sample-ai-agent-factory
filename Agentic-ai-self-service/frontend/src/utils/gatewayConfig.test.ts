/**
 * Property-based tests for gateway configuration utilities.
 * Validates: Requirements 4.7
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  isValidLambdaArn,
  resolveGatewayTargets,
  mapGatewayDeployTargets,
  isLiteLLMGateway,
  createDefaultGatewayConfig,
} from './gatewayConfig';
import type { GatewayConfiguration } from '../types/components';

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

// Valid AWS regions
const validRegions = [
  'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
  'eu-west-1', 'eu-west-2', 'eu-central-1',
  'ap-northeast-1', 'ap-southeast-1', 'ap-southeast-2',
  'sa-east-1', 'ca-central-1',
  'us-gov-west-1', 'us-gov-east-1',
];

const regionArb = fc.constantFrom(...validRegions);

// Valid 12-digit AWS account IDs
const accountIdArb = fc.stringMatching(/^\d{12}$/);

// Valid Lambda function names (1-64 chars, alphanumeric, hyphens, underscores)
const functionNameArb = fc.stringMatching(/^[a-zA-Z0-9_-]{1,64}$/);

// Optional qualifier (version or alias)
const qualifierArb = fc.oneof(
  fc.constant(''),
  fc.constant(':$LATEST'),
  fc.stringMatching(/^:[a-zA-Z0-9_-]+$/).filter((q) => q.length <= 65)
);

// Generator for valid Lambda ARNs
const validLambdaArnArb = fc.tuple(regionArb, accountIdArb, functionNameArb, qualifierArb)
  .map(([region, accountId, functionName, qualifier]) =>
    `arn:aws:lambda:${region}:${accountId}:function:${functionName}${qualifier}`
  );

// Generator for invalid ARNs (various malformed patterns)
const invalidArnArb = fc.oneof(
  // Empty or whitespace
  fc.constant(''),
  fc.constant('   '),
  // Missing parts
  fc.constant('arn:aws:lambda'),
  fc.constant('arn:aws:lambda:us-east-1'),
  fc.constant('arn:aws:lambda:us-east-1:123456789012'),
  fc.constant('arn:aws:lambda:us-east-1:123456789012:function'),
  // Wrong service
  fc.constant('arn:aws:s3:us-east-1:123456789012:function:my-function'),
  fc.constant('arn:aws:ec2:us-east-1:123456789012:function:my-function'),
  // Invalid account ID (not 12 digits)
  fc.constant('arn:aws:lambda:us-east-1:12345:function:my-function'),
  fc.constant('arn:aws:lambda:us-east-1:1234567890123:function:my-function'),
  fc.constant('arn:aws:lambda:us-east-1:abcdefghijkl:function:my-function'),
  // Invalid region format
  fc.constant('arn:aws:lambda:invalid:123456789012:function:my-function'),
  fc.constant('arn:aws:lambda:US-EAST-1:123456789012:function:my-function'),
  // Invalid function name
  fc.constant('arn:aws:lambda:us-east-1:123456789012:function:'),
  fc.constant('arn:aws:lambda:us-east-1:123456789012:function:my function'),
  fc.constant('arn:aws:lambda:us-east-1:123456789012:function:my.function'),
  // Random strings
  fc.string({ minLength: 1, maxLength: 100 }).filter((s) => !s.startsWith('arn:aws:lambda:')),
);

// ============================================================================
// Property 17: Lambda ARN Format Validation
// ============================================================================

describe('Property 17: Lambda ARN Format Validation', () => {
  /**
   * **Validates: Requirements 4.7**
   *
   * For any Lambda ARN input, the validation shall verify the ARN matches
   * the pattern `arn:aws:lambda:<region>:<account>:function:<name>` and
   * display an error for invalid formats.
   */
  it('accepts valid Lambda ARNs', () => {
    fc.assert(
      fc.property(validLambdaArnArb, (arn) => {
        expect(isValidLambdaArn(arn)).toBe(true);
      }),
      { numRuns: 100 }
    );
  });

  it('rejects invalid ARNs', () => {
    fc.assert(
      fc.property(invalidArnArb, (arn) => {
        expect(isValidLambdaArn(arn)).toBe(false);
      }),
      { numRuns: 100 }
    );
  });

  it('validates region format correctly', () => {
    // Valid regions should pass
    for (const region of validRegions) {
      const arn = `arn:aws:lambda:${region}:123456789012:function:my-function`;
      expect(isValidLambdaArn(arn)).toBe(true);
    }

    // Invalid regions should fail
    const invalidRegions = ['invalid', 'US-EAST-1', 'us_east_1', '123', 'us-east'];
    for (const region of invalidRegions) {
      const arn = `arn:aws:lambda:${region}:123456789012:function:my-function`;
      expect(isValidLambdaArn(arn)).toBe(false);
    }
  });

  it('validates account ID format correctly', () => {
    fc.assert(
      fc.property(accountIdArb, (accountId) => {
        const arn = `arn:aws:lambda:us-east-1:${accountId}:function:my-function`;
        expect(isValidLambdaArn(arn)).toBe(true);
      }),
      { numRuns: 100 }
    );

    // Invalid account IDs
    const invalidAccountIds = ['12345', '1234567890123', 'abcdefghijkl', '12345678901a'];
    for (const accountId of invalidAccountIds) {
      const arn = `arn:aws:lambda:us-east-1:${accountId}:function:my-function`;
      expect(isValidLambdaArn(arn)).toBe(false);
    }
  });

  it('validates function name format correctly', () => {
    fc.assert(
      fc.property(functionNameArb, (functionName) => {
        const arn = `arn:aws:lambda:us-east-1:123456789012:function:${functionName}`;
        expect(isValidLambdaArn(arn)).toBe(true);
      }),
      { numRuns: 100 }
    );

    // Invalid function names
    const invalidNames = ['', 'my function', 'my.function', 'my@function'];
    for (const name of invalidNames) {
      const arn = `arn:aws:lambda:us-east-1:123456789012:function:${name}`;
      expect(isValidLambdaArn(arn)).toBe(false);
    }
  });

  it('handles null and undefined inputs', () => {
    expect(isValidLambdaArn(null as unknown as string)).toBe(false);
    expect(isValidLambdaArn(undefined as unknown as string)).toBe(false);
  });

  it('handles non-string inputs', () => {
    expect(isValidLambdaArn(123 as unknown as string)).toBe(false);
    expect(isValidLambdaArn({} as unknown as string)).toBe(false);
    expect(isValidLambdaArn([] as unknown as string)).toBe(false);
  });
});

// ============================================================================
// Multi-target resolution + deploy mapping
// ============================================================================

describe('resolveGatewayTargets', () => {
  it('falls back to the single legacy target when targets[] is absent', () => {
    const config: GatewayConfiguration = {
      name: 'gw',
      targetType: 'lambda',
      targetConfig: { type: 'lambda', functionArn: 'arn' },
      enableSemanticSearch: true,
    };
    const resolved = resolveGatewayTargets(config);
    expect(resolved).toHaveLength(1);
    expect(resolved[0].type).toBe('lambda');
  });

  it('prefers targets[] when present and non-empty', () => {
    const config: GatewayConfiguration = {
      name: 'gw',
      targetType: 'lambda',
      targetConfig: { type: 'lambda', functionArn: 'arn' },
      targets: [
        { type: 'openapi', specUrl: 'https://x/openapi.json' },
        { type: 'smithy', modelName: 'dynamodb' },
      ],
      enableSemanticSearch: true,
    };
    const resolved = resolveGatewayTargets(config);
    expect(resolved.map((t) => t.type)).toEqual(['openapi', 'smithy']);
  });
});

describe('mapGatewayDeployTargets', () => {
  it('splits a mixed targets[] into externalMcpServers + gatewayTargets', () => {
    const config: GatewayConfiguration = {
      name: 'gw',
      targetType: 'lambda',
      targetConfig: { type: 'lambda', functionArn: 'arn' },
      targets: [
        { type: 'mcp_server', serverId: 'aws-knowledge' },
        { type: 'mcp_server', serverId: '__custom__', serverUrl: 'https://mcp.example.com/mcp', authType: 'none', customName: 'mine' },
        { type: 'lambda', functionArn: 'arn:aws:lambda:us-west-2:123456789012:function:a' },
        { type: 'openapi', specUrl: 'https://api.example.com/openapi.json' },
        { type: 'smithy', modelName: 'dynamodb' },
      ],
      enableSemanticSearch: true,
    };
    const { externalMcpServers, gatewayTargets } = mapGatewayDeployTargets(config);

    // Both MCP entries collected (catalog + custom).
    expect(externalMcpServers).toHaveLength(2);
    expect(externalMcpServers[0]).toEqual({ server_id: 'aws-knowledge' });
    expect(externalMcpServers[1]).toMatchObject({
      endpoint: 'https://mcp.example.com/mcp',
      auth_type: 'none',
      name: 'mine',
    });

    // Non-MCP families threaded through as gatewayTargets.
    expect(gatewayTargets.map((t) => t.type)).toEqual(['lambda', 'openapi', 'smithy']);
  });

  it('maps a legacy single mcp_server target with no targets[]', () => {
    const config: GatewayConfiguration = {
      name: 'gw',
      targetType: 'mcp_server',
      targetConfig: { type: 'mcp_server', serverId: 'aws-knowledge' },
      enableSemanticSearch: true,
    };
    const { externalMcpServers, gatewayTargets } = mapGatewayDeployTargets(config);
    expect(externalMcpServers).toEqual([{ server_id: 'aws-knowledge' }]);
    expect(gatewayTargets).toHaveLength(0);
  });

  it('drops a custom mcp target with no URL and returns empty for null config', () => {
    const config: GatewayConfiguration = {
      name: 'gw',
      targetType: 'mcp_server',
      targetConfig: { type: 'mcp_server', serverId: '__custom__' },
      enableSemanticSearch: true,
    };
    expect(mapGatewayDeployTargets(config).externalMcpServers).toHaveLength(0);
    expect(mapGatewayDeployTargets(null).externalMcpServers).toHaveLength(0);
  });

  // The third supported shape: a LiteLLM proxy as an mcp_server TARGET inside an
  // AgentCore Gateway (rather than replacing the gateway). The descriptor is what
  // decides how the gateway authenticates outbound; with none sent, the backend
  // used to fall back to a bare `Authorization: <key>`, which LiteLLM rejects.
  const customApiKeyTarget = (extra: Record<string, unknown> = {}) => ({
    name: 'gw',
    targetType: 'mcp_server' as const,
    targetConfig: {
      type: 'mcp_server' as const,
      serverId: '__custom__',
      serverUrl: 'https://litellm.example.com/mcp/',
      authType: 'api_key' as const,
      apiKey: 'sk-secret',
      ...extra,
    },
    enableSemanticSearch: true,
  });

  it('sends an Authorization: Bearer descriptor for a custom api_key endpoint', () => {
    const [entry] = mapGatewayDeployTargets(customApiKeyTarget()).externalMcpServers;
    expect(entry.secret_value).toBe('sk-secret');
    expect(entry.api_key_descriptor).toEqual({
      location: 'HEADER',
      parameter_name: 'Authorization',
      prefix: 'Bearer',
    });
  });

  it('leaves NO trailing space on the prefix', () => {
    // AgentCore joins credentialPrefix to the key with its own single space, so a
    // trailing space here is sent as `Bearer  <key>` and the target fails its
    // initialize handshake with HTTP 400. Verified against real AWS.
    const [entry] = mapGatewayDeployTargets(customApiKeyTarget()).externalMcpServers;
    expect(entry.api_key_descriptor?.prefix).not.toMatch(/\s$/);
  });

  it("honours LiteLLM's own header when the user names it", () => {
    const cfg = customApiKeyTarget({ apiKeyHeader: 'x-litellm-api-key' });
    const [entry] = mapGatewayDeployTargets(cfg).externalMcpServers;
    expect(entry.api_key_descriptor?.parameter_name).toBe('x-litellm-api-key');
    expect(entry.api_key_descriptor?.prefix).toBe('Bearer');
  });

  it('sends an empty prefix when the raw format is chosen', () => {
    const cfg = customApiKeyTarget({ apiKeyHeader: 'x-api-key', apiKeyFormat: 'raw' as const });
    const [entry] = mapGatewayDeployTargets(cfg).externalMcpServers;
    expect(entry.api_key_descriptor).toEqual({
      location: 'HEADER',
      parameter_name: 'x-api-key',
      prefix: '',
    });
  });

  it('sends no descriptor for a catalog entry or a non-api_key custom endpoint', () => {
    // Catalog entries carry their own descriptor server-side — sending one from
    // the canvas would override a verified value with a guess.
    const catalog = mapGatewayDeployTargets({
      name: 'gw',
      targetType: 'mcp_server',
      targetConfig: { type: 'mcp_server', serverId: 'aws-knowledge', apiKeyHeader: 'nope' },
      enableSemanticSearch: true,
    } as GatewayConfiguration);
    expect(catalog.externalMcpServers[0].api_key_descriptor).toBeUndefined();

    const noAuth = mapGatewayDeployTargets(
      customApiKeyTarget({ authType: 'none' as const }) as GatewayConfiguration,
    );
    expect(noAuth.externalMcpServers[0].api_key_descriptor).toBeUndefined();
  });
});

// ============================================================================
// Gateway provider (Workstream A) — LiteLLM as an ADDITIONAL provider
// ============================================================================

describe('gateway provider selection', () => {
  it('defaults a freshly dropped Gateway node to AgentCore', () => {
    expect(createDefaultGatewayConfig().gatewayProvider).toBe('agentcore');
  });

  it('treats a canvas saved before this feature as AgentCore', () => {
    // Every stored flow predates `gatewayProvider`. Reading `undefined` as
    // LiteLLM would silently repoint existing agents at a proxy that isn't there.
    const legacy: GatewayConfiguration = {
      name: 'gw',
      targetType: 'lambda',
      targetConfig: { type: 'lambda', functionArn: 'arn:aws:lambda:us-west-2:123456789012:function:a' },
      enableSemanticSearch: true,
    };
    expect(isLiteLLMGateway(legacy)).toBe(false);
    expect(isLiteLLMGateway(null)).toBe(false);
    expect(isLiteLLMGateway(undefined)).toBe(false);
  });

  it('recognizes an explicit LiteLLM gateway', () => {
    const config: GatewayConfiguration = {
      ...createDefaultGatewayConfig(),
      gatewayProvider: 'litellm',
      litellmBaseUrl: 'https://litellm.example.com',
    };
    expect(isLiteLLMGateway(config)).toBe(true);
  });

  it('leaves the AgentCore target mapping untouched for a LiteLLM gateway', () => {
    // The deploy payload keeps carrying whatever targets the node happens to
    // have; the backend ignores them for a LiteLLM provider. Asserted so this
    // mapper is never quietly taught to branch on the provider.
    const config: GatewayConfiguration = {
      name: 'gw',
      gatewayProvider: 'litellm',
      litellmBaseUrl: 'https://litellm.example.com',
      targetType: 'mcp_server',
      targetConfig: { type: 'mcp_server', serverId: 'aws-knowledge' },
      enableSemanticSearch: true,
    };
    expect(mapGatewayDeployTargets(config).externalMcpServers).toEqual([{ server_id: 'aws-knowledge' }]);
  });
});
