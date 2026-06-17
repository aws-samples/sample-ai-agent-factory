/**
 * Core workflow type definitions for AgentCore Visual Workflow Platform.
 * These types define the structure of workflows, components, and connections.
 */

import type { ComponentConfiguration } from './components';

// ============================================================================
// Core Workflow Types
// ============================================================================

export interface WorkflowDefinition {
  id: string;
  name: string;
  description: string;
  version: string;
  nodes: ComponentNode[];
  edges: ConnectionEdge[];
  viewport: Viewport;
  metadata: WorkflowMetadata;
  createdAt: string;
  updatedAt: string;
}

export interface ComponentNode {
  id: string;
  type: AgentCoreComponentType;
  position: Position;
  data: ComponentConfiguration;
  selected: boolean;
  validationStatus: ValidationStatus;
}

export interface Position {
  x: number;
  y: number;
}

export interface ConnectionEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle: string;
  targetHandle: string;
  type: ConnectionType;
  animated: boolean;
  data: EdgeData;
}

export interface EdgeData {
  label?: string;
  validationStatus: ValidationStatus;
}

export interface Viewport {
  x: number;
  y: number;
  zoom: number;
}

export interface WorkflowMetadata {
  author: string;
  tags: string[];
  awsRegion: string;
  deploymentStatus: DeploymentStatus;
  lastDeployedAt?: string;
  endpointUrl?: string;
}

// ============================================================================
// Enums and Union Types
// ============================================================================

export type AgentCoreComponentType =
  | 'runtime'
  | 'gateway'
  | 'memory'
  | 'code_interpreter'
  | 'browser'
  | 'observability'
  | 'identity'
  | 'evaluation'
  | 'policy'
  | 'guardrails'
  | 'a2a'
  | 'tool';

export type ConnectionType = 'data' | 'tool' | 'identity';

export type ValidationStatus = 'valid' | 'warning' | 'error' | 'pending';

export type DeploymentStatus = 'not_deployed' | 'deploying' | 'deployed' | 'failed';

export type SaveStatus = 'saved' | 'saving' | 'pending' | 'error';

export type AgentServerProtocol = 'HTTP' | 'MCP' | 'A2A';

export type PythonRuntime = 'PYTHON_3_10' | 'PYTHON_3_11' | 'PYTHON_3_12' | 'PYTHON_3_13';

export type DeploymentType = 'direct_code_deploy' | 'container';
