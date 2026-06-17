/**
 * Template type definitions for prebuilt workflow templates.
 */

import type { AgentCoreComponentType, ConnectionType } from './workflow';
import type { ComponentConfiguration } from './components';

// ============================================================================
// Template Types
// ============================================================================

export type TemplateDifficulty = 'beginner' | 'intermediate' | 'advanced';

export interface TemplateNodeDefinition {
  idSuffix: string;
  type: AgentCoreComponentType;
  position: { x: number; y: number };
  label: string;
  configuration: ComponentConfiguration;
}

export interface TemplateEdgeDefinition {
  sourceIdSuffix: string;
  targetIdSuffix: string;
  connectionType: ConnectionType;
}

export interface TemplateToolInfo {
  name: string;
  icon: string;
  description: string;
}

export interface WorkflowTemplate {
  id: string;
  name: string;
  description: string;
  longDescription: string;
  icon: string;
  difficulty: TemplateDifficulty;
  tags: string[];
  componentTypes: AgentCoreComponentType[];
  builtInTools: TemplateToolInfo[];
  nodes: TemplateNodeDefinition[];
  edges: TemplateEdgeDefinition[];
}
