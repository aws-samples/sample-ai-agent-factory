/**
 * Validation and error types for workflow validation.
 * Aligned with AWS Bedrock AgentCore primitives.
 */

import type { AgentCoreComponentType } from './workflow';

export interface ValidationError {
  componentId?: string;
  field: string;
  message: string;
  severity: 'error' | 'warning';
}

export interface ValidationResult {
  isValid: boolean;
  errors: ValidationError[];
  warnings: ValidationError[];
}

// Connection compatibility matrix for AgentCore primitives
export const CONNECTION_COMPATIBILITY: Record<AgentCoreComponentType, AgentCoreComponentType[]> = {
  runtime: ['gateway', 'memory', 'code_interpreter', 'browser', 'observability', 'identity', 'evaluation', 'policy', 'guardrails', 'a2a'],
  gateway: ['runtime', 'identity', 'policy', 'tool'],
  memory: ['runtime'],
  code_interpreter: ['runtime'],
  browser: ['runtime'],
  observability: ['runtime'],
  identity: ['runtime', 'gateway'],
  evaluation: ['runtime'],
  policy: ['runtime', 'gateway'],
  guardrails: ['runtime'],
  a2a: ['runtime'],
  // `tool` covers both built-in/custom tools AND SaaS connector nodes (a
  // connector is a `tool`-typed node with toolId "connector:<id>"). Both wire
  // through the gateway (tool/connector -> gateway -> runtime); neither may
  // attach directly to the runtime.
  tool: ['gateway'],
};

// Required fields per component type
export const REQUIRED_FIELDS: Record<AgentCoreComponentType, string[]> = {
  runtime: ['name', 'framework', 'model', 'systemPrompt'],
  gateway: ['name', 'targetType', 'targetConfig'],
  memory: ['name'],
  code_interpreter: ['name'],
  browser: ['name'],
  observability: ['name'],
  identity: ['name', 'credentialType'],
  evaluation: ['name'],
  policy: ['name'],
  guardrails: ['name'],
  a2a: ['name'],
  tool: ['name', 'toolId'],
};

// Connection colors by type
export const CONNECTION_COLORS = {
  data: '#3B82F6',     // blue
  tool: '#22C55E',     // green
  identity: '#F97316', // orange
} as const;
