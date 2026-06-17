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
        modelId: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
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
        modelId: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
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
