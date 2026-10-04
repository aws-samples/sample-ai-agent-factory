/**
 * ValidationEngine service for workflow validation.
 * Implements component configuration validation and connection compatibility validation.
 * Requirements: 8.1, 8.2, 8.3
 */

import type { AgentCoreComponentType, ValidationStatus, ConnectionType } from '../types/workflow';
import type {
  ComponentConfiguration,
  RuntimeConfiguration,
  GatewayConfiguration,
  IdentityConfiguration,
  LambdaTargetConfig,
} from '../types/components';
import type { ValidationError } from '../types/validation';
import { CONNECTION_COMPATIBILITY, REQUIRED_FIELDS } from '../types/validation';
import { isLiteLLMGateway, isValidLambdaArn, resolveGatewayTargets } from './gatewayConfig';
import { validateCredentialFormat } from './identityConfig';

// ============================================================================
// Types
// ============================================================================

export interface NodeValidationState {
  nodeId: string;
  status: ValidationStatus;
  errors: ValidationError[];
  warnings: ValidationError[];
}

export interface EdgeValidationState {
  edgeId: string;
  status: ValidationStatus;
  errors: ValidationError[];
}

export interface WorkflowValidationState {
  isValid: boolean;
  isReadyToDeploy: boolean;
  nodeStates: Map<string, NodeValidationState>;
  edgeStates: Map<string, EdgeValidationState>;
  errors: ValidationError[];
  warnings: ValidationError[];
}

export interface WorkflowNode {
  id: string;
  type: AgentCoreComponentType;
  data: {
    configuration?: ComponentConfiguration;
    label?: string;
  };
}

export interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
  type?: ConnectionType;
}

// ============================================================================
// Component Configuration Validation
// ============================================================================

/**
 * Validate a component's configuration based on its type.
 * Property 16: Required Field Validation
 */
export function validateComponentConfiguration(
  nodeId: string,
  componentType: AgentCoreComponentType,
  configuration?: ComponentConfiguration
): NodeValidationState {
  const errors: ValidationError[] = [];
  const warnings: ValidationError[] = [];

  if (!configuration) {
    errors.push({
      componentId: nodeId,
      field: 'configuration',
      message: 'Component configuration is required',
      severity: 'error',
    });
    return { nodeId, status: 'error', errors, warnings };
  }

  // Validate required fields.
  // A LiteLLM gateway has no AgentCore target, so targetType/targetConfig are
  // swapped for litellmBaseUrl. Done here rather than in REQUIRED_FIELDS because
  // the required set depends on the node's own config, not just its type.
  let requiredFields: readonly string[] = REQUIRED_FIELDS[componentType];
  if (componentType === 'gateway' && isLiteLLMGateway(configuration as GatewayConfiguration)) {
    requiredFields = ['name', 'litellmBaseUrl'];
  } else if (
    componentType === 'runtime'
    && (configuration as RuntimeConfiguration).protocol === 'MCP'
  ) {
    requiredFields = ['name'];
  }
  for (const field of requiredFields) {
    const value = getNestedValue(configuration as unknown as Record<string, unknown>, field);
    if (value === undefined || value === null || value === '') {
      errors.push({
        componentId: nodeId,
        field,
        message: `${formatFieldName(field)} is required`,
        severity: 'error',
      });
    }
  }

  // Type-specific validation
  switch (componentType) {
    case 'runtime':
      validateRuntimeConfig(nodeId, configuration as RuntimeConfiguration, errors, warnings);
      break;
    case 'gateway':
      validateGatewayConfig(nodeId, configuration as GatewayConfiguration, errors, warnings);
      break;
    case 'identity':
      validateIdentityConfig(nodeId, configuration as IdentityConfiguration, errors, warnings);
      break;
    // Memory, CodeInterpreter, Browser, Observability, Evaluation, Policy, A2A have minimal validation
    case 'memory':
    case 'code_interpreter':
    case 'browser':
    case 'observability':
    case 'evaluation':
    case 'policy':
    case 'a2a':
      // These components only require a name, which is already validated above
      break;
  }

  const status: ValidationStatus = errors.length > 0 ? 'error' : warnings.length > 0 ? 'warning' : 'valid';
  return { nodeId, status, errors, warnings };
}

function validateRuntimeConfig(
  nodeId: string,
  config: RuntimeConfiguration,
  errors: ValidationError[],
  warnings: ValidationError[]
): void {
  // Validate system prompt length
  if (config.systemPrompt && config.systemPrompt.length > 100000) {
    errors.push({
      componentId: nodeId,
      field: 'systemPrompt',
      message: 'System prompt exceeds maximum length of 100,000 characters',
      severity: 'error',
    });
  }

  // Validate idle timeout
  if (config.idleTimeout !== undefined && (config.idleTimeout < 60 || config.idleTimeout > 28800)) {
    errors.push({
      componentId: nodeId,
      field: 'idleTimeout',
      message: 'Idle timeout must be between 60 and 28800 seconds',
      severity: 'error',
    });
  }

  // Validate max lifetime
  if (config.maxLifetime !== undefined && (config.maxLifetime < 60 || config.maxLifetime > 28800)) {
    errors.push({
      componentId: nodeId,
      field: 'maxLifetime',
      message: 'Max lifetime must be between 60 and 28800 seconds',
      severity: 'error',
    });
  }

  // Validate model configuration
  if (config.model) {
    if (config.model.temperature !== undefined && (config.model.temperature < 0 || config.model.temperature > 2)) {
      errors.push({
        componentId: nodeId,
        field: 'model.temperature',
        message: 'Temperature must be between 0 and 2',
        severity: 'error',
      });
    }
    if (config.model.topP !== undefined && (config.model.topP < 0 || config.model.topP > 1)) {
      errors.push({
        componentId: nodeId,
        field: 'model.topP',
        message: 'Top P must be between 0 and 1',
        severity: 'error',
      });
    }
  }

  // Warning for empty system prompt
  if (
    config.protocol !== 'MCP'
    && (!config.systemPrompt || config.systemPrompt.trim().length === 0)
  ) {
    warnings.push({
      componentId: nodeId,
      field: 'systemPrompt',
      message: 'System prompt is empty - consider adding instructions for the agent',
      severity: 'warning',
    });
  }
}

function validateGatewayConfig(
  nodeId: string,
  config: GatewayConfiguration,
  errors: ValidationError[],
  warnings: ValidationError[]
): void {
  // A LiteLLM gateway is validated on its own terms — the AgentCore target
  // checks below would all be false-negatives against fields it never sets.
  if (isLiteLLMGateway(config)) {
    const base = (config.litellmBaseUrl || '').trim();
    if (base && !/^https:\/\/[^\s/]+/i.test(base)) {
      errors.push({
        componentId: nodeId,
        field: 'litellmBaseUrl',
        message: 'LiteLLM base URL must be an https:// URL',
        severity: 'error',
      });
    }
    if (!config.litellmApiKey && !config.litellmApiKeyRef) {
      errors.push({
        componentId: nodeId,
        field: 'litellmApiKey',
        message: 'A LiteLLM virtual key is required to authenticate to the gateway',
        severity: 'error',
      });
    }
    return;
  }

  // Validate EVERY target the deploy will actually send.
  //
  // This used to read `config.targetType` / `config.targetConfig` — the legacy single
  // target. Once the multi-target editor landed, `resolveGatewayTargets` returns
  // `config.targets` whenever it is non-empty and ignores `targetConfig` entirely, so on
  // every multi-target gateway these checks were validating a field the deploy no longer
  // reads: the whole `targets[]` array went out unvalidated. Iterating the resolved list
  // is the only way the check and the payload cannot disagree.
  const resolvedTargets = resolveGatewayTargets(config);
  resolvedTargets.forEach((target, index) => {
    // Field paths stay `targetConfig.*` for a single legacy target so existing
    // field-level UI bindings keep working, and become `targets[i].*` otherwise.
    const fieldBase = config.targets && config.targets.length > 0 ? `targets[${index}]` : 'targetConfig';

    if (target.type === 'lambda') {
      const lambdaConfig = target as LambdaTargetConfig;
      // The ARN is REQUIRED, not merely format-checked when present.
      // createDefaultTargetConfig hands out `{ type: 'lambda', functionArn: '' }`, so
      // leaving the field blank passed validation, and the backend then skipped the
      // target with a warning — a deployment that reported success with the user's tool
      // silently absent (observed live: "Gateway lambda target #0 has no function_arn").
      if (!lambdaConfig.functionArn) {
        errors.push({
          componentId: nodeId,
          field: `${fieldBase}.functionArn`,
          message: 'A Lambda function ARN is required for a Lambda gateway target',
          severity: 'error',
        });
      } else if (!isValidLambdaArn(lambdaConfig.functionArn)) {
        errors.push({
          componentId: nodeId,
          field: `${fieldBase}.functionArn`,
          message: 'Invalid Lambda ARN format. Expected: arn:aws:lambda:<region>:<account>:function:<name>',
          severity: 'error',
        });
      }
    } else if (target.type === 'openapi') {
      const openApiConfig = target as { specUrl?: string; specContent?: string };
      if (!openApiConfig.specUrl && !openApiConfig.specContent) {
        errors.push({
          componentId: nodeId,
          field: fieldBase,
          message: 'OpenAPI specification URL or content is required',
          severity: 'error',
        });
      }
    } else if (target.type === 'smithy') {
      // No longer offered in the canvas (see TARGET_TYPE_OPTIONS), but a canvas saved
      // before it was withdrawn still carries one, and it can never deploy.
      errors.push({
        componentId: nodeId,
        field: `${fieldBase}.type`,
        message:
          'Smithy model targets are not supported: AgentCore needs an inline Smithy schema, ' +
          'not a model name. Use an OpenAPI, Lambda or MCP Server target instead.',
        severity: 'error',
      });
    } else if (target.type === 'mcp_server') {
      // mapMcpTargetToDeployEntry drops an entry with no catalog id, and a `__custom__`
      // entry with no URL, returning null — so an incomplete MCP target is dropped from
      // the deploy payload silently, exactly like the lambda case.
      const mcpConfig = target as { serverId?: string; serverUrl?: string };
      if (!mcpConfig.serverId) {
        errors.push({
          componentId: nodeId,
          field: `${fieldBase}.serverId`,
          message: 'Select an MCP server, or choose Custom and provide an endpoint URL',
          severity: 'error',
        });
      } else if (mcpConfig.serverId === '__custom__' && !mcpConfig.serverUrl) {
        errors.push({
          componentId: nodeId,
          field: `${fieldBase}.serverUrl`,
          message: 'A custom MCP server needs an endpoint URL',
          severity: 'error',
        });
      }
    }
  });

  // Warning for semantic search disabled
  if (!config.enableSemanticSearch) {
    warnings.push({
      componentId: nodeId,
      field: 'enableSemanticSearch',
      message: 'Semantic search is disabled - consider enabling for better tool discovery',
      severity: 'warning',
    });
  }
}

function validateIdentityConfig(
  nodeId: string,
  config: IdentityConfiguration,
  errors: ValidationError[],
  warnings: ValidationError[]
): void {
  if (config.credentialType === 'oauth2' && config.oauth2Config) {
    // Validate client ID
    if (config.oauth2Config.clientId) {
      const result = validateCredentialFormat(config.oauth2Config.clientId, 'client_id');
      if (!result.isValid) {
        errors.push({
          componentId: nodeId,
          field: 'oauth2Config.clientId',
          message: result.error || 'Invalid client ID format',
          severity: 'error',
        });
      }
    }

    // Validate client secret reference
    if (config.oauth2Config.clientSecretRef) {
      const result = validateCredentialFormat(config.oauth2Config.clientSecretRef, 'secret_ref');
      if (!result.isValid) {
        errors.push({
          componentId: nodeId,
          field: 'oauth2Config.clientSecretRef',
          message: result.error || 'Invalid secret reference format',
          severity: 'error',
        });
      }
    }

    // Validate custom OAuth2 config
    if (config.oauth2Config.provider === 'custom' && config.oauth2Config.customConfig) {
      if (!config.oauth2Config.customConfig.authorizationUrl) {
        errors.push({
          componentId: nodeId,
          field: 'oauth2Config.customConfig.authorizationUrl',
          message: 'Authorization URL is required for custom OAuth2 provider',
          severity: 'error',
        });
      }
      if (!config.oauth2Config.customConfig.tokenUrl) {
        errors.push({
          componentId: nodeId,
          field: 'oauth2Config.customConfig.tokenUrl',
          message: 'Token URL is required for custom OAuth2 provider',
          severity: 'error',
        });
      }
    }
  }

  if (config.credentialType === 'api_key' && config.apiKeyConfig) {
    if (config.apiKeyConfig.keyValueRef) {
      const result = validateCredentialFormat(config.apiKeyConfig.keyValueRef, 'secret_ref');
      if (!result.isValid) {
        errors.push({
          componentId: nodeId,
          field: 'apiKeyConfig.keyValueRef',
          message: result.error || 'Invalid API key reference format',
          severity: 'error',
        });
      }
    }
  }

  // Warning for empty scopes
  if (config.credentialType === 'oauth2' && config.oauth2Config) {
    if (!config.oauth2Config.scopes || config.oauth2Config.scopes.length === 0) {
      warnings.push({
        componentId: nodeId,
        field: 'oauth2Config.scopes',
        message: 'No OAuth2 scopes configured',
        severity: 'warning',
      });
    }
  }
}

// ============================================================================
// Connection Compatibility Validation
// ============================================================================

/**
 * Check if two component types can be connected.
 * Property 9 & 10: Connection Compatibility
 */
export function areComponentsCompatible(
  sourceType: AgentCoreComponentType,
  targetType: AgentCoreComponentType
): boolean {
  const compatibleTargets = CONNECTION_COMPATIBILITY[sourceType];
  return compatibleTargets?.includes(targetType) ?? false;
}

/**
 * Validate a connection between two nodes.
 */
export function validateConnection(
  edge: WorkflowEdge,
  nodes: WorkflowNode[]
): EdgeValidationState {
  const errors: ValidationError[] = [];

  const sourceNode = nodes.find((n) => n.id === edge.source);
  const targetNode = nodes.find((n) => n.id === edge.target);

  if (!sourceNode) {
    errors.push({
      componentId: edge.id,
      field: 'source',
      message: 'Source node not found',
      severity: 'error',
    });
  }

  if (!targetNode) {
    errors.push({
      componentId: edge.id,
      field: 'target',
      message: 'Target node not found',
      severity: 'error',
    });
  }

  if (sourceNode && targetNode) {
    if (!areComponentsCompatible(sourceNode.type, targetNode.type)) {
      errors.push({
        componentId: edge.id,
        field: 'connection',
        message: `Cannot connect ${sourceNode.type} to ${targetNode.type}`,
        severity: 'error',
      });
    }
  }

  const status: ValidationStatus = errors.length > 0 ? 'error' : 'valid';
  return { edgeId: edge.id, status, errors };
}

// ============================================================================
// Full Workflow Validation
// ============================================================================

/**
 * Validate an entire workflow including all nodes and edges.
 * Property 22, 23, 24: Workflow Validation
 */
export function validateWorkflow(
  nodes: WorkflowNode[],
  edges: WorkflowEdge[]
): WorkflowValidationState {
  const nodeStates = new Map<string, NodeValidationState>();
  const edgeStates = new Map<string, EdgeValidationState>();
  const allErrors: ValidationError[] = [];
  const allWarnings: ValidationError[] = [];

  // Validate all nodes
  for (const node of nodes) {
    const state = validateComponentConfiguration(
      node.id,
      node.type,
      node.data.configuration
    );
    nodeStates.set(node.id, state);
    allErrors.push(...state.errors);
    allWarnings.push(...state.warnings);
  }

  // Validate all edges
  for (const edge of edges) {
    const state = validateConnection(edge, nodes);
    edgeStates.set(edge.id, state);
    allErrors.push(...state.errors);
  }

  // Code generation has dedicated A2A and multi-agent runtime shapes. They do
  // not yet define which peer/sub-agent owns a connected Memory, Gateway,
  // Browser, Code Interpreter, or Knowledge Base capability. Without this
  // workflow-level check, the backend can create those resources and an
  // early-returning generator can silently omit them from the emitted agent.
  //
  // Knowledge Base is a tool node behind a Gateway, so it is deliberately
  // discovered one hop beyond the runtime instead of looking only at direct
  // neighbours.
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const neighbours = (nodeId: string): WorkflowNode[] =>
    edges.flatMap((edge) => {
      if (edge.source === nodeId) {
        const node = nodesById.get(edge.target);
        return node ? [node] : [];
      }
      if (edge.target === nodeId) {
        const node = nodesById.get(edge.source);
        return node ? [node] : [];
      }
      return [];
    });

  const displayCapability = (capability: string): string =>
    capability
      .split('_')
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');

  // A gateway must have something to serve. The deployer refuses a declared target with no
  // payload and would otherwise deploy an EMPTY gateway green (F-24's class): the tools are
  // silently absent. Serving sources: explicit targets in the gateway config, connected tool
  // nodes (deployed as Lambda targets), or a connected MCP-protocol runtime (deployed as an
  // MCP server target). LiteLLM gateways are validated on their own terms.
  for (const gateway of nodes.filter((node) => node.type === 'gateway')) {
    const config = gateway.data.configuration as GatewayConfiguration | undefined;
    if (!config || isLiteLLMGateway(config)) continue;
    const hasExplicitTarget = resolveGatewayTargets(config).length > 0;
    const connected = neighbours(gateway.id);
    const hasTool = connected.some((node) => node.type === 'tool');
    const hasMcpRuntime = connected.some(
      (node) =>
        node.type === 'runtime'
        && (node.data.configuration as RuntimeConfiguration | undefined)?.protocol === 'MCP',
    );
    if (hasExplicitTarget || hasTool || hasMcpRuntime) continue;
    const error: ValidationError = {
      componentId: gateway.id,
      field: 'targets',
      message:
        'This gateway has nothing to serve. Add a Lambda, OpenAPI or MCP target in its '
        + 'configuration, or connect a tool or an MCP server runtime to it.',
      severity: 'error',
    };
    const current = nodeStates.get(gateway.id);
    if (current) {
      nodeStates.set(gateway.id, {
        ...current,
        status: 'error',
        errors: [...current.errors, error],
      });
    } else {
      nodeStates.set(gateway.id, { nodeId: gateway.id, status: 'error', errors: [error], warnings: [] });
    }
    allErrors.push(error);
  }

  for (const runtime of nodes.filter((node) => node.type === 'runtime')) {
    const config = runtime.data.configuration as RuntimeConfiguration | undefined;
    if (!config) continue;

    const capabilities = new Set<string>();
    for (const connected of neighbours(runtime.id)) {
      if (
        connected.type === 'memory' ||
        connected.type === 'gateway' ||
        connected.type === 'browser' ||
        connected.type === 'code_interpreter' ||
        connected.type === 'a2a'
      ) {
        capabilities.add(connected.type);
      }

      if (connected.type === 'gateway') {
        for (const gatewayNeighbour of neighbours(connected.id)) {
          if (gatewayNeighbour.id === runtime.id || gatewayNeighbour.type !== 'tool') continue;
          const toolConfig = gatewayNeighbour.data.configuration as unknown as
            | Record<string, unknown>
            | undefined;
          if (toolConfig?.toolId === 'knowledge_base') {
            capabilities.add('knowledge_base');
          }
        }
      }
    }

    if (config.protocol === 'A2A') {
      capabilities.add('a2a');
    }

    const multiAgentPattern = config.multiAgentPattern || 'none';
    const multiAgentEnabled = multiAgentPattern !== 'none';
    const nonA2A = [...capabilities].filter((capability) => capability !== 'a2a');
    let message: string | undefined;

    if (capabilities.has('a2a') && (nonA2A.length > 0 || multiAgentEnabled)) {
      const requested = [...capabilities].map(displayCapability);
      if (multiAgentEnabled) requested.push(`Multi-Agent ${displayCapability(multiAgentPattern)}`);
      message =
        `A2A cannot currently compose with the other requested capabilities ` +
        `(${requested.sort().join(', ')}). Disconnect them or use a separate runtime; ` +
        `none will be silently omitted.`;
    } else if (multiAgentEnabled && capabilities.size > 0) {
      message =
        `The Multi-Agent ${displayCapability(multiAgentPattern)} pattern cannot currently ` +
        `assign connected capabilities (${[...capabilities]
          .map(displayCapability)
          .sort()
          .join(', ')}) to individual agents. Disconnect them or use a single-agent runtime.`;
    }

    if (message) {
      const error: ValidationError = {
        componentId: runtime.id,
        field: 'connectedComponents',
        message,
        severity: 'error',
      };
      const current = nodeStates.get(runtime.id);
      if (current) {
        nodeStates.set(runtime.id, {
          ...current,
          status: 'error',
          errors: [...current.errors, error],
        });
      }
      allErrors.push(error);
    }
  }

  const isValid = allErrors.length === 0;
  const isReadyToDeploy = isValid && nodes.length > 0;

  return {
    isValid,
    isReadyToDeploy,
    nodeStates,
    edgeStates,
    errors: allErrors,
    warnings: allWarnings,
  };
}

/**
 * Get validation status for a specific node.
 */
export function getNodeValidationStatus(
  nodeId: string,
  validationState: WorkflowValidationState
): ValidationStatus {
  return validationState.nodeStates.get(nodeId)?.status ?? 'pending';
}

/**
 * Get validation errors for a specific node.
 */
export function getNodeValidationErrors(
  nodeId: string,
  validationState: WorkflowValidationState
): ValidationError[] {
  return validationState.nodeStates.get(nodeId)?.errors ?? [];
}

/**
 * Get validation status for a specific edge.
 */
export function getEdgeValidationStatus(
  edgeId: string,
  validationState: WorkflowValidationState
): ValidationStatus {
  return validationState.edgeStates.get(edgeId)?.status ?? 'pending';
}

// ============================================================================
// Utility Functions
// ============================================================================

function getNestedValue(obj: Record<string, unknown>, path: string): unknown {
  const parts = path.split('.');
  let current: unknown = obj;
  for (const part of parts) {
    // Reject prototype-chain keys so a crafted path can't walk into
    // Object.prototype (prototype-pollution read guard).
    if (part === '__proto__' || part === 'constructor' || part === 'prototype') {
      return undefined;
    }
    if (current === null || current === undefined) {
      return undefined;
    }
    if (!Object.hasOwn(current as Record<string, unknown>, part)) {
      return undefined;
    }
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

function formatFieldName(field: string): string {
  return field
    .split('.')
    .pop()!
    .replace(/([A-Z])/g, ' $1')
    .replace(/^./, (str) => str.toUpperCase())
    .trim();
}
