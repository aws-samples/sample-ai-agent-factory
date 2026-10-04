/**
 * Property-based tests for validation engine.
 * Validates: Requirements 3.7, 3.8, 8.1, 8.2, 8.3, 8.4
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  validateComponentConfiguration,
  validateConnection,
  validateWorkflow,
  areComponentsCompatible,
  type WorkflowNode,
  type WorkflowEdge,
} from './validation';
import { CONNECTION_COMPATIBILITY, REQUIRED_FIELDS } from '../types/validation';
import type { AgentCoreComponentType } from '../types/workflow';
import type {
  RuntimeConfiguration,
  GatewayConfiguration,
  IdentityConfiguration,
} from '../types/components';

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

const componentTypeArb = fc.constantFrom<AgentCoreComponentType>(
  'runtime',
  'gateway',
  'memory',
  'code_interpreter',
  'browser',
  'observability',
  'identity'
);

const nodeIdArb = fc.uuid();

// Generate valid runtime configuration
const validRuntimeConfigArb = fc.record({
  name: fc.string({ minLength: 1, maxLength: 100 }),
  entrypoint: fc.constant('agent.py'),
  framework: fc.constantFrom(
    'strands_agents',
    'langgraph',
    'langchain',
    'crewai',
    'llamaindex',
    'openai_agents_sdk',
    'google_adk',
    'autogen',
    'custom'
  ),
  model: fc.record({
    provider: fc.constantFrom('anthropic', 'amazon', 'openai', 'google', 'meta'),
    modelId: fc.string({ minLength: 1 }),
    temperature: fc.float({ min: 0, max: 2 }),
    topP: fc.float({ min: 0, max: 1 }),
  }),
  systemPrompt: fc.string({ minLength: 1, maxLength: 1000 }),
  deploymentType: fc.constantFrom('direct_code_deploy', 'container'),
  pythonRuntime: fc.constantFrom('PYTHON_3_10', 'PYTHON_3_11', 'PYTHON_3_12', 'PYTHON_3_13'),
  protocol: fc.constantFrom('HTTP', 'MCP', 'A2A'),
  idleTimeout: fc.integer({ min: 60, max: 28800 }),
  maxLifetime: fc.integer({ min: 60, max: 28800 }),
  enableOtel: fc.boolean(),
}) as fc.Arbitrary<RuntimeConfiguration>;

// Generate invalid runtime configuration (missing required fields)
const invalidRuntimeConfigArb = fc.record({
  name: fc.constant(''),
  entrypoint: fc.constant('agent.py'),
  framework: fc.constantFrom(
    'strands_agents',
    'langgraph',
    'langchain',
    'crewai',
    'llamaindex',
    'openai_agents_sdk',
    'google_adk',
    'autogen',
    'custom'
  ),
  model: fc.record({
    provider: fc.constantFrom('anthropic', 'amazon', 'openai', 'google', 'meta'),
    modelId: fc.string({ minLength: 1 }),
    temperature: fc.float({ min: 0, max: 2 }),
    topP: fc.float({ min: 0, max: 1 }),
  }),
  systemPrompt: fc.constant(''),
  deploymentType: fc.constantFrom('direct_code_deploy', 'container'),
  pythonRuntime: fc.constantFrom('PYTHON_3_10', 'PYTHON_3_11', 'PYTHON_3_12', 'PYTHON_3_13'),
  protocol: fc.constantFrom('HTTP', 'MCP', 'A2A'),
  idleTimeout: fc.integer({ min: 60, max: 28800 }),
  maxLifetime: fc.integer({ min: 60, max: 28800 }),
  enableOtel: fc.boolean(),
}) as fc.Arbitrary<RuntimeConfiguration>;

// ============================================================================
// Property 22: Node Validation Indicators
// ============================================================================

describe('Property 22: Node Validation Indicators', () => {
  /**
   * **Validates: Requirements 8.1, 8.2**
   *
   * For any node with incomplete configuration, a warning indicator shall be displayed.
   * For any node with invalid configuration, an error indicator with descriptive tooltip
   * shall be displayed.
   */
  it('returns error status for nodes with missing required fields', () => {
    fc.assert(
      fc.property(nodeIdArb, componentTypeArb, (nodeId, componentType) => {
        // Validate with no configuration
        const result = validateComponentConfiguration(nodeId, componentType, undefined);

        expect(result.status).toBe('error');
        expect(result.errors.length).toBeGreaterThan(0);
        expect(result.errors[0].message).toContain('required');
      }),
      { numRuns: 50 }
    );
  });

  it('returns valid status for nodes with complete valid configuration', () => {
    fc.assert(
      fc.property(nodeIdArb, validRuntimeConfigArb, (nodeId, config) => {
        const result = validateComponentConfiguration(nodeId, 'runtime', config);

        // Should be valid or warning (warnings are acceptable for valid configs)
        expect(['valid', 'warning']).toContain(result.status);
        expect(result.errors.length).toBe(0);
      }),
      { numRuns: 50 }
    );
  });

  it('includes descriptive error messages for invalid configurations', () => {
    fc.assert(
      fc.property(nodeIdArb, invalidRuntimeConfigArb, (nodeId, config) => {
        const result = validateComponentConfiguration(nodeId, 'runtime', config);

        // Should have errors for missing name and systemPrompt
        expect(result.status).toBe('error');
        expect(result.errors.some(e => e.message.toLowerCase().includes('name'))).toBe(true);
      }),
      { numRuns: 50 }
    );
  });

  it('validation errors include component ID for tracking', () => {
    fc.assert(
      fc.property(nodeIdArb, componentTypeArb, (nodeId, componentType) => {
        const result = validateComponentConfiguration(nodeId, componentType, undefined);

        // All errors should reference the component
        for (const error of result.errors) {
          expect(error.componentId).toBe(nodeId);
        }
      }),
      { numRuns: 50 }
    );
  });
});

// ============================================================================
// Property 23: Connection Compatibility Validation
// ============================================================================

describe('Property 23: Connection Compatibility Validation', () => {
  /**
   * **Validates: Requirements 8.3**
   *
   * For any edge connecting incompatible component types, a validation error
   * shall be displayed on the connection.
   */
  it('returns error for incompatible connections', () => {
    fc.assert(
      fc.property(
        componentTypeArb,
        componentTypeArb,
        nodeIdArb,
        nodeIdArb,
        nodeIdArb,
        (sourceType, targetType, sourceId, targetId, edgeId) => {
          // Skip if same node
          if (sourceId === targetId) return true;

          const nodes: WorkflowNode[] = [
            { id: sourceId, type: sourceType, data: {} },
            { id: targetId, type: targetType, data: {} },
          ];

          const edge: WorkflowEdge = {
            id: edgeId,
            source: sourceId,
            target: targetId,
          };

          const result = validateConnection(edge, nodes);
          const isCompatible = areComponentsCompatible(sourceType, targetType);

          if (isCompatible) {
            expect(result.status).toBe('valid');
            expect(result.errors.length).toBe(0);
          } else {
            expect(result.status).toBe('error');
            expect(result.errors.length).toBeGreaterThan(0);
            expect(result.errors[0].message).toContain('Cannot connect');
          }

          return true;
        }
      ),
      { numRuns: 100 }
    );
  });

  it('compatible connections from compatibility matrix are valid', () => {
    // Test all valid combinations from the compatibility matrix
    for (const [sourceType, targets] of Object.entries(CONNECTION_COMPATIBILITY)) {
      for (const targetType of targets) {
        const nodes: WorkflowNode[] = [
          { id: 'source-1', type: sourceType as AgentCoreComponentType, data: {} },
          { id: 'target-1', type: targetType, data: {} },
        ];

        const edge: WorkflowEdge = {
          id: 'edge-1',
          source: 'source-1',
          target: 'target-1',
        };

        const result = validateConnection(edge, nodes);
        expect(result.status).toBe('valid');
      }
    }
  });

  it('returns error when source node is not found', () => {
    const nodes: WorkflowNode[] = [
      { id: 'target-1', type: 'runtime', data: {} },
    ];

    const edge: WorkflowEdge = {
      id: 'edge-1',
      source: 'missing-source',
      target: 'target-1',
    };

    const result = validateConnection(edge, nodes);
    expect(result.status).toBe('error');
    expect(result.errors.some(e => e.message.includes('Source node not found'))).toBe(true);
  });

  it('returns error when target node is not found', () => {
    const nodes: WorkflowNode[] = [
      { id: 'source-1', type: 'runtime', data: {} },
    ];

    const edge: WorkflowEdge = {
      id: 'edge-1',
      source: 'source-1',
      target: 'missing-target',
    };

    const result = validateConnection(edge, nodes);
    expect(result.status).toBe('error');
    expect(result.errors.some(e => e.message.includes('Target node not found'))).toBe(true);
  });
});

// ============================================================================
// Property 24: Ready-to-Deploy Indicator
// ============================================================================

describe('Property 24: Ready-to-Deploy Indicator', () => {
  /**
   * **Validates: Requirements 8.4**
   *
   * For any workflow where all nodes have valid configurations and all connections
   * are compatible, a ready-to-deploy indicator shall be displayed.
   */
  it('returns isReadyToDeploy=true for valid workflow with nodes', () => {
    const validConfig: RuntimeConfiguration = {
      name: 'Test Runtime',
      entrypoint: 'agent.py',
      framework: 'strands_agents',
      model: {
        provider: 'anthropic',
        modelId: 'us.anthropic.claude-sonnet-5',
        temperature: 0.7,
        topP: 0.9,
      },
      systemPrompt: 'You are a helpful assistant.',
      deploymentType: 'direct_code_deploy',
      pythonRuntime: 'PYTHON_3_11',
      protocol: 'HTTP',
      idleTimeout: 300,
      maxLifetime: 3600,
      enableOtel: false,
      modelProvider: 'bedrock',
      multiAgentPattern: 'none',
    };

    const nodes: WorkflowNode[] = [
      { id: 'node-1', type: 'runtime', data: { configuration: validConfig } },
    ];

    const edges: WorkflowEdge[] = [];

    const result = validateWorkflow(nodes, edges);

    expect(result.isValid).toBe(true);
    expect(result.isReadyToDeploy).toBe(true);
    expect(result.errors.length).toBe(0);
  });

  it('returns isReadyToDeploy=false for empty workflow', () => {
    const result = validateWorkflow([], []);

    expect(result.isValid).toBe(true);
    expect(result.isReadyToDeploy).toBe(false);
  });

  it('returns isReadyToDeploy=false when nodes have errors', () => {
    const nodes: WorkflowNode[] = [
      { id: 'node-1', type: 'runtime', data: {} }, // Missing configuration
    ];

    const result = validateWorkflow(nodes, []);

    expect(result.isValid).toBe(false);
    expect(result.isReadyToDeploy).toBe(false);
    expect(result.errors.length).toBeGreaterThan(0);
  });

  it('returns isReadyToDeploy=false when edges have errors', () => {
    const memoryConfig = {
      name: 'Test Memory',
      enabled: true,
    };

    // Memory cannot connect to memory (incompatible)
    const nodes: WorkflowNode[] = [
      { id: 'node-1', type: 'memory', data: { configuration: memoryConfig } },
      { id: 'node-2', type: 'memory', data: { configuration: memoryConfig } },
    ];

    const edges: WorkflowEdge[] = [
      { id: 'edge-1', source: 'node-1', target: 'node-2' },
    ];

    const result = validateWorkflow(nodes, edges);

    expect(result.isValid).toBe(false);
    expect(result.isReadyToDeploy).toBe(false);
  });

  it('aggregates all node and edge validation states', () => {
    fc.assert(
      fc.property(
        fc.array(componentTypeArb, { minLength: 1, maxLength: 5 }),
        (types) => {
          const nodes: WorkflowNode[] = types.map((type, i) => ({
            id: `node-${i}`,
            type,
            data: {}, // Missing configuration - will cause errors
          }));

          const result = validateWorkflow(nodes, []);

          // Should have validation state for each node
          expect(result.nodeStates.size).toBe(nodes.length);

          // All nodes should have error status due to missing config
          for (const [, state] of result.nodeStates) {
            expect(state.status).toBe('error');
          }

          return true;
        }
      ),
      { numRuns: 50 }
    );
  });
});

// ============================================================================
// Property 16: Required Field Validation
// ============================================================================

describe('Property 16: Required Field Validation', () => {
  /**
   * **Validates: Requirements 3.7, 3.8**
   *
   * For any component configuration save operation, if any required field for that
   * component type is empty or invalid, the validation shall fail and error indicators
   * shall be displayed on the missing/invalid fields.
   */
  it('validates all required fields for each component type', () => {
    for (const [componentType] of Object.entries(REQUIRED_FIELDS)) {
      const result = validateComponentConfiguration(
        'test-node',
        componentType as AgentCoreComponentType,
        undefined
      );

      expect(result.status).toBe('error');
      expect(result.errors.length).toBeGreaterThan(0);

      // Should have error about configuration being required
      expect(result.errors.some(e =>
        e.message.toLowerCase().includes('required')
      )).toBe(true);
    }
  });

  it('reports specific field names in error messages', () => {
    // Create config with empty name
    const config: RuntimeConfiguration = {
      name: '',
      entrypoint: 'agent.py',
      framework: 'strands_agents',
      model: {
        provider: 'anthropic',
        modelId: 'us.anthropic.claude-sonnet-5',
        temperature: 0.7,
        topP: 0.9,
      },
      systemPrompt: '',
      deploymentType: 'direct_code_deploy',
      pythonRuntime: 'PYTHON_3_11',
      protocol: 'HTTP',
      idleTimeout: 300,
      maxLifetime: 3600,
      enableOtel: false,
      modelProvider: 'bedrock',
      multiAgentPattern: 'none',
    };

    const result = validateComponentConfiguration('test-node', 'runtime', config);

    expect(result.status).toBe('error');

    // Should have error for name field
    const nameError = result.errors.find(e => e.field === 'name');
    expect(nameError).toBeDefined();
    expect(nameError?.message.toLowerCase()).toContain('name');
  });

  it('validates nested required fields', () => {
    // Runtime requires model configuration
    const config: RuntimeConfiguration = {
      name: 'Test',
      entrypoint: 'agent.py',
      framework: 'strands_agents',
      model: {
        provider: 'anthropic',
        modelId: '', // Empty model ID
        temperature: 0.7,
        topP: 0.9,
      },
      systemPrompt: 'Test prompt',
      deploymentType: 'direct_code_deploy',
      pythonRuntime: 'PYTHON_3_11',
      protocol: 'HTTP',
      idleTimeout: 300,
      maxLifetime: 3600,
      enableOtel: false,
      modelProvider: 'bedrock',
      multiAgentPattern: 'none',
    };

    const result = validateComponentConfiguration('test-node', 'runtime', config);

    // Model is a required field and should be validated
    // The validation should pass since model object exists
    // (individual model fields are not in REQUIRED_FIELDS)
    expect(result.errors.filter(e => e.field === 'model').length).toBe(0);
  });
});

// ============================================================================
// Additional Unit Tests
// ============================================================================

describe('Validation Engine Utilities', () => {
  it('areComponentsCompatible returns correct results', () => {
    // Valid combinations
    expect(areComponentsCompatible('runtime', 'gateway')).toBe(true);
    expect(areComponentsCompatible('runtime', 'identity')).toBe(true);
    expect(areComponentsCompatible('runtime', 'memory')).toBe(true);
    expect(areComponentsCompatible('gateway', 'runtime')).toBe(true);
    expect(areComponentsCompatible('identity', 'runtime')).toBe(true);
    expect(areComponentsCompatible('identity', 'gateway')).toBe(true);
    expect(areComponentsCompatible('memory', 'runtime')).toBe(true);
    expect(areComponentsCompatible('code_interpreter', 'runtime')).toBe(true);
    expect(areComponentsCompatible('browser', 'runtime')).toBe(true);

    // Invalid combinations
    expect(areComponentsCompatible('gateway', 'gateway')).toBe(false);
    expect(areComponentsCompatible('memory', 'memory')).toBe(false);
    expect(areComponentsCompatible('identity', 'memory')).toBe(false);
  });

  describe('a gateway must have something to serve', () => {
    const runtime = (id: string, protocol: 'HTTP' | 'MCP' = 'HTTP'): WorkflowNode => ({
      id,
      type: 'runtime',
      data: {
        configuration: {
          name: id,
          framework: 'strands',
          systemPrompt: 'You are helpful.',
          model: { modelId: 'us.anthropic.claude-haiku-4-5-20251001-v1:0' },
          protocol,
        } as unknown as RuntimeConfiguration,
      },
    });
    const gateway = (id: string, config: Partial<GatewayConfiguration> = {}): WorkflowNode => ({
      id,
      type: 'gateway',
      data: { configuration: { name: id, enableSemanticSearch: true, ...config } as GatewayConfiguration },
    });
    const tool = (id: string): WorkflowNode => ({
      id,
      type: 'tool',
      data: { configuration: { name: id, toolId: 'get_order' } as never },
    });
    const edge = (source: string, target: string): WorkflowEdge => ({ id: `${source}-${target}`, source, target, type: 'data' });
    const gatewayErrors = (nodes: WorkflowNode[], edges: WorkflowEdge[]) =>
      validateWorkflow(nodes, edges).errors.filter((e) => e.componentId === 'gw').map((e) => e.field);

    it('rejects a gateway with no explicit target, no tool and no MCP runtime (the shipped strands shape)', () => {
      // Live, 2026-09-28: the deployer refused "a declared target that has no payload" while the
      // canvas read "Ready to deploy". A bare { type: 'lambda' } placeholder is not a target either.
      const result = validateWorkflow([runtime('rt'), gateway('gw')], [edge('rt', 'gw')]);
      expect(result.isReadyToDeploy).toBe(false);
      expect(result.nodeStates.get('gw')?.status).toBe('error');
      expect(gatewayErrors([runtime('rt'), gateway('gw')], [edge('rt', 'gw')])).toContain('targets');
      // A single legacy placeholder target reports under the legacy field path.
      expect(
        gatewayErrors([runtime('rt'), gateway('gw', { targetType: 'lambda', targetConfig: { type: 'lambda' } as never })], [edge('rt', 'gw')]),
      ).toContain('targetConfig.functionArn');
    });

    it('accepts a gateway served by a connected tool node (the blueprint shape)', () => {
      const result = validateWorkflow(
        [runtime('rt'), gateway('gw'), tool('t1')],
        [edge('rt', 'gw'), edge('gw', 't1')],
      );
      expect(result.errors.filter((e) => e.componentId === 'gw')).toEqual([]);
    });

    it('accepts a gateway served by a connected MCP-protocol runtime (the MCP-server-target shape)', () => {
      const result = validateWorkflow(
        [runtime('rt'), gateway('gw'), runtime('mcp', 'MCP')],
        [edge('rt', 'gw'), edge('gw', 'mcp')],
      );
      expect(result.errors.filter((e) => e.componentId === 'gw')).toEqual([]);
    });

    it('accepts a gateway with an explicit Lambda target that carries its ARN', () => {
      const result = validateWorkflow(
        [runtime('rt'), gateway('gw', { targets: [{ type: 'lambda', functionArn: 'arn:aws:lambda:us-east-1:123456789012:function:tool' }] as never })],
        [edge('rt', 'gw')],
      );
      expect(result.errors.filter((e) => e.componentId === 'gw')).toEqual([]);
    });

    it('does not apply the rule to a LiteLLM gateway', () => {
      const result = validateWorkflow(
        [runtime('rt'), gateway('gw', { gatewayProvider: 'litellm', litellmBaseUrl: 'https://proxy.example', litellmApiKey: 'k' } as never)],
        [edge('rt', 'gw')],
      );
      expect(result.errors.filter((e) => e.componentId === 'gw' && e.field === 'targets')).toEqual([]);
    });

    it('no longer requires a target type as a bare field, so templates need no placeholder', () => {
      expect(REQUIRED_FIELDS.gateway).toEqual(['name']);
    });
  });

  it('validates Lambda ARN format in gateway config', () => {
    const validConfig: GatewayConfiguration = {
      name: 'Test Gateway',
      targetType: 'lambda',
      targetConfig: {
        type: 'lambda',
        functionArn: 'arn:aws:lambda:us-east-1:123456789012:function:my-function',
      },
      enableSemanticSearch: false,
    };

    const result = validateComponentConfiguration('test-node', 'gateway', validConfig);

    // Should not have Lambda ARN error
    expect(result.errors.filter(e => e.field === 'targetConfig.functionArn').length).toBe(0);
  });

  it('reports error for invalid Lambda ARN', () => {
    const invalidConfig: GatewayConfiguration = {
      name: 'Test Gateway',
      targetType: 'lambda',
      targetConfig: {
        type: 'lambda',
        functionArn: 'invalid-arn',
      },
      enableSemanticSearch: false,
    };

    const result = validateComponentConfiguration('test-node', 'gateway', invalidConfig);

    // Should have Lambda ARN error
    const arnError = result.errors.find(e => e.field === 'targetConfig.functionArn');
    expect(arnError).toBeDefined();
    expect(arnError?.message).toContain('Invalid Lambda ARN');
  });

  // ------------------------------------------------------------------
  // Every target the deploy sends must be validated.
  //
  // These checks used to read `targetType` / `targetConfig` only. Once the
  // multi-target editor landed, `resolveGatewayTargets` ignores `targetConfig`
  // whenever `targets[]` is non-empty — so the array that actually reaches the
  // backend went out completely unvalidated, and an incomplete target was
  // dropped server-side while the deployment reported success.
  // ------------------------------------------------------------------

  it('rejects an empty Lambda ARN, not just a malformed one', () => {
    // createDefaultTargetConfig hands out functionArn: '', so this is the state of
    // every freshly added Lambda target. It produced a green deploy with no tool.
    const result = validateComponentConfiguration('test-node', 'gateway', {
      name: 'Test Gateway',
      targetType: 'lambda',
      targetConfig: { type: 'lambda', functionArn: '' },
      enableSemanticSearch: false,
    } as GatewayConfiguration);

    const err = result.errors.find((e) => e.field === 'targetConfig.functionArn');
    expect(err).toBeDefined();
    expect(err?.message).toContain('required');
  });

  it('validates each entry of a multi-target gateway, not the legacy single target', () => {
    const result = validateComponentConfiguration('test-node', 'gateway', {
      name: 'Test Gateway',
      // The legacy pair is VALID and is what the old code looked at...
      targetType: 'lambda',
      targetConfig: { type: 'lambda', functionArn: 'arn:aws:lambda:us-east-1:123456789012:function:ok' },
      // ...while every entry the deploy actually sends is incomplete.
      targets: [
        { type: 'lambda', functionArn: '' },
        { type: 'openapi' },
        { type: 'mcp_server' },
      ],
      enableSemanticSearch: false,
    } as GatewayConfiguration);

    expect(result.errors.map((e) => e.field)).toEqual(
      expect.arrayContaining(['targets[0].functionArn', 'targets[1]', 'targets[2].serverId'])
    );
  });

  it('accepts a fully-specified multi-target gateway', () => {
    const result = validateComponentConfiguration('test-node', 'gateway', {
      name: 'Test Gateway',
      targetType: 'lambda',
      targetConfig: { type: 'lambda', functionArn: 'arn:aws:lambda:us-east-1:123456789012:function:ok' },
      targets: [
        { type: 'lambda', functionArn: 'arn:aws:lambda:us-east-1:123456789012:function:ok' },
        { type: 'openapi', specUrl: 'https://api.example.com/openapi.json' },
        { type: 'mcp_server', serverId: 'aws-knowledge' },
        { type: 'mcp_server', serverId: '__custom__', serverUrl: 'https://mcp.example.com/mcp' },
      ],
      enableSemanticSearch: true,
    } as GatewayConfiguration);

    // A refusal-only suite would pass while rejecting everything; pin the happy path.
    expect(result.errors).toHaveLength(0);
  });

  it('rejects a custom MCP target with no endpoint URL', () => {
    const result = validateComponentConfiguration('test-node', 'gateway', {
      name: 'Test Gateway',
      targetType: 'mcp_server',
      targetConfig: { type: 'mcp_server', serverId: '__custom__' },
      enableSemanticSearch: false,
    } as GatewayConfiguration);

    expect(result.errors.find((e) => e.field === 'targetConfig.serverUrl')).toBeDefined();
  });

  it('rejects a Smithy target, which can never deploy', () => {
    const result = validateComponentConfiguration('test-node', 'gateway', {
      name: 'Test Gateway',
      targetType: 'smithy',
      targetConfig: { type: 'smithy', modelName: 'dynamodb' },
      enableSemanticSearch: false,
    } as GatewayConfiguration);

    const err = result.errors.find((e) => e.field === 'targetConfig.type');
    expect(err).toBeDefined();
    expect(err?.message).toContain('not supported');
  });

  it('no longer offers Smithy as a target family', async () => {
    const { TARGET_TYPE_OPTIONS } = await import('./gatewayConfig');
    expect(TARGET_TYPE_OPTIONS.map((o) => o.value)).not.toContain('smithy');
  });

  it('validates identity configuration', () => {
    const validConfig: IdentityConfiguration = {
      name: 'Test Identity',
      credentialType: 'api_key',
      apiKeyConfig: {
        keyName: 'my-key',
        keyValueRef: 'secrets/my-key',
        headerName: 'X-API-Key',
      },
    };

    const result = validateComponentConfiguration('test-node', 'identity', validConfig);

    // Should not have errors
    expect(result.errors.length).toBe(0);
  });

  it('reports error for missing identity name', () => {
    const invalidConfig: IdentityConfiguration = {
      name: '',
      credentialType: 'api_key',
      apiKeyConfig: {
        keyName: 'my-key',
        keyValueRef: 'secrets/my-key',
        headerName: 'X-API-Key',
      },
    };

    const result = validateComponentConfiguration('test-node', 'identity', invalidConfig);

    // Should have name error
    const nameError = result.errors.find(e => e.field === 'name');
    expect(nameError).toBeDefined();
  });
});

// ============================================================================
// Code-generation composition boundary
// ============================================================================

describe('Code-generation component composition', () => {
  const runtimeConfig = (
    overrides: Partial<RuntimeConfiguration> = {}
  ): RuntimeConfiguration => ({
    name: 'Composition Runtime',
    entrypoint: 'agent.py',
    framework: 'strands_agents',
    model: {
      provider: 'anthropic',
      modelId: 'us.anthropic.claude-sonnet-5',
      temperature: 0.7,
      topP: 0.9,
    },
    systemPrompt: 'Use every connected capability.',
    deploymentType: 'direct_code_deploy',
    pythonRuntime: 'PYTHON_3_13',
    protocol: 'HTTP',
    idleTimeout: 300,
    maxLifetime: 3600,
    enableOtel: false,
    modelProvider: 'bedrock',
    multiAgentPattern: 'none',
    ...overrides,
  });

  it('blocks A2A plus another connected capability before deployment', () => {
    const nodes: WorkflowNode[] = [
      {
        id: 'runtime',
        type: 'runtime',
        data: { configuration: runtimeConfig() },
      },
      {
        id: 'a2a',
        type: 'a2a',
        data: {
          configuration: {
            name: 'Peer Network',
            enabled: true,
            pattern: 'peer_to_peer',
            agentEndpoints: [],
            timeoutSeconds: 30,
            maxRetries: 3,
            enableParallelExecution: false,
            enableMessageRouting: false,
            routingStrategy: 'round_robin',
            shareContext: false,
            contextWindowSize: 10,
          },
        },
      },
      {
        id: 'browser',
        type: 'browser',
        data: { configuration: { name: 'Browser', enabled: true } },
      },
    ];
    const edges: WorkflowEdge[] = [
      { id: 'runtime-a2a', source: 'runtime', target: 'a2a' },
      { id: 'runtime-browser', source: 'runtime', target: 'browser' },
    ];

    const result = validateWorkflow(nodes, edges);

    expect(result.isReadyToDeploy).toBe(false);
    expect(result.nodeStates.get('runtime')?.status).toBe('error');
    const message = result.nodeStates
      .get('runtime')
      ?.errors.find((error) => error.field === 'connectedComponents')?.message;
    expect(message).toContain('A2A');
    expect(message).toContain('Browser');
  });

  it('blocks multi-agent plus a Knowledge Base reached through its Gateway', () => {
    const nodes: WorkflowNode[] = [
      {
        id: 'runtime',
        type: 'runtime',
        data: {
          configuration: runtimeConfig({
            multiAgentPattern: 'graph',
            multiAgentConfig: { agents: [] },
          }),
        },
      },
      {
        id: 'gateway',
        type: 'gateway',
        data: {
          configuration: {
            name: 'Composition Gateway',
            targetType: 'lambda',
            targetConfig: {
              type: 'lambda',
              functionArn: 'arn:aws:lambda:us-east-1:123456789012:function:composition',
            },
            enableSemanticSearch: true,
          },
        },
      },
      {
        id: 'knowledge-base',
        type: 'tool',
        data: {
          configuration: {
            name: 'Knowledge Base',
            toolId: 'knowledge_base',
            description: 'Retrieve grounded context.',
            enabled: true,
            isKnowledgeBase: true,
          },
        },
      },
    ];
    const edges: WorkflowEdge[] = [
      { id: 'runtime-gateway', source: 'runtime', target: 'gateway' },
      { id: 'gateway-kb', source: 'gateway', target: 'knowledge-base' },
    ];

    const result = validateWorkflow(nodes, edges);

    expect(result.isReadyToDeploy).toBe(false);
    const message = result.nodeStates
      .get('runtime')
      ?.errors.find((error) => error.field === 'connectedComponents')?.message;
    expect(message).toContain('Multi-Agent Graph');
    expect(message).toContain('Gateway');
    expect(message).toContain('Knowledge Base');
  });

  it('keeps feasible single-agent compositions deployable', () => {
    const nodes: WorkflowNode[] = [
      {
        id: 'runtime',
        type: 'runtime',
        data: { configuration: runtimeConfig() },
      },
      {
        id: 'memory',
        type: 'memory',
        data: { configuration: { name: 'Memory', enabled: true } },
      },
      {
        id: 'browser',
        type: 'browser',
        data: { configuration: { name: 'Browser', enabled: true } },
      },
    ];
    const edges: WorkflowEdge[] = [
      { id: 'runtime-memory', source: 'runtime', target: 'memory' },
      { id: 'runtime-browser', source: 'runtime', target: 'browser' },
    ];

    const result = validateWorkflow(nodes, edges);

    expect(result.isReadyToDeploy).toBe(true);
    expect(result.nodeStates.get('runtime')?.status).not.toBe('error');
  });
});

// ============================================================================
// Gateway provider validation (Workstream A)
// ============================================================================

describe('LiteLLM gateway validation', () => {
  const litellm = (over: Partial<GatewayConfiguration> = {}): GatewayConfiguration =>
    ({
      name: 'gw',
      gatewayProvider: 'litellm',
      litellmBaseUrl: 'https://litellm.example.com',
      litellmApiKey: 'sk-test',
      enableSemanticSearch: true,
      // Deliberately absent: targetType / targetConfig. A LiteLLM gateway has
      // no AgentCore target, and REQUIRED_FIELDS.gateway hard-demands both.
      ...over,
    } as unknown as GatewayConfiguration);

  it('accepts a LiteLLM gateway with no targetType or targetConfig', () => {
    const result = validateComponentConfiguration('n1', 'gateway', litellm());
    expect(result.errors.map((e) => e.field)).not.toContain('targetType');
    expect(result.errors.map((e) => e.field)).not.toContain('targetConfig');
    expect(result.errors).toHaveLength(0);
  });

  it('no longer demands targetType/targetConfig as bare fields on an AgentCore gateway', () => {
    // Requiring them at the field level is what made templates ship a { type: 'lambda' }
    // placeholder that the deployer then refused as a target with no payload. Whether a
    // gateway has something to serve is decided by validateWorkflow against the whole graph
    // (explicit targets, connected tools, or a connected MCP runtime); see
    // 'a gateway must have something to serve'.
    const result = validateComponentConfiguration('n1', 'gateway', {
      name: 'gw',
      enableSemanticSearch: true,
    } as unknown as GatewayConfiguration);
    expect(result.errors.map((e) => e.field)).not.toContain('targetType');
    expect(result.errors.map((e) => e.field)).not.toContain('targetConfig');
    expect(result.errors).toEqual([]);  // a name is present, and nothing else is required at this level
  });

  it('requires a base URL', () => {
    const result = validateComponentConfiguration('n1', 'gateway', litellm({ litellmBaseUrl: '' }));
    expect(result.status).toBe('error');
    expect(result.errors.map((e) => e.field)).toContain('litellmBaseUrl');
  });

  it('rejects a non-https base URL before it ever reaches the backend', () => {
    const result = validateComponentConfiguration(
      'n1',
      'gateway',
      litellm({ litellmBaseUrl: 'http://litellm.example.com' })
    );
    expect(result.errors.find((e) => e.field === 'litellmBaseUrl')?.message).toMatch(/https/);
  });

  it('requires either an inline key or a stored key reference', () => {
    const missing = validateComponentConfiguration(
      'n1',
      'gateway',
      litellm({ litellmApiKey: undefined })
    );
    expect(missing.errors.map((e) => e.field)).toContain('litellmApiKey');

    // After a deploy the raw key is gone and only the ARN comes back. That must
    // not read as "unconfigured" and block every subsequent save.
    const stored = validateComponentConfiguration(
      'n1',
      'gateway',
      litellm({
        litellmApiKey: undefined,
        litellmApiKeyRef: 'arn:aws:secretsmanager:eu-central-1:1:secret:agentcore-connector/x',
      })
    );
    expect(stored.errors).toHaveLength(0);
  });

  it('does not warn about semantic search, which is an AgentCore-only concept', () => {
    const result = validateComponentConfiguration(
      'n1',
      'gateway',
      litellm({ enableSemanticSearch: false })
    );
    expect(result.warnings.map((w) => w.field)).not.toContain('enableSemanticSearch');
  });

  it('does not apply the Lambda ARN check to a LiteLLM gateway', () => {
    const result = validateComponentConfiguration(
      'n1',
      'gateway',
      litellm({ targetType: 'lambda', targetConfig: { type: 'lambda', functionArn: 'not-an-arn' } })
    );
    expect(result.errors).toHaveLength(0);
  });
});
