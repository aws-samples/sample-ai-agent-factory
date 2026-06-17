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
import { isValidLambdaArn } from './gatewayConfig';
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

  // Validate required fields
  const requiredFields = REQUIRED_FIELDS[componentType];
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
  if (!config.systemPrompt || config.systemPrompt.trim().length === 0) {
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
  // Validate Lambda ARN if target type is lambda
  if (config.targetType === 'lambda' && config.targetConfig) {
    const lambdaConfig = config.targetConfig as LambdaTargetConfig;
    if (lambdaConfig.functionArn && !isValidLambdaArn(lambdaConfig.functionArn)) {
      errors.push({
        componentId: nodeId,
        field: 'targetConfig.functionArn',
        message: 'Invalid Lambda ARN format. Expected: arn:aws:lambda:<region>:<account>:function:<name>',
        severity: 'error',
      });
    }
  }

  // Validate OpenAPI spec if target type is openapi
  if (config.targetType === 'openapi' && config.targetConfig) {
    const openApiConfig = config.targetConfig as { specUrl?: string; specContent?: string };
    if (!openApiConfig.specUrl && !openApiConfig.specContent) {
      errors.push({
        componentId: nodeId,
        field: 'targetConfig',
        message: 'OpenAPI specification URL or content is required',
        severity: 'error',
      });
    }
  }

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
