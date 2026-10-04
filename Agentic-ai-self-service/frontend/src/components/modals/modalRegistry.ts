/**
 * Modal registry - maps modal keys to lazy-loaded component + props type.
 * Centralized modal management for App.tsx.
 */

import { lazy, type ComponentType } from 'react';
import type { AgentCoreComponentType } from '../../types/workflow';
import type {
  RuntimeConfiguration,
  GatewayConfiguration,
  IdentityConfiguration,
  MemoryConfiguration,
  PolicyConfiguration,
  GuardrailsConfiguration,
  ObservabilityConfiguration,
  ToolConfiguration,
  KnowledgeBaseToolConfig,
  A2AConfiguration,
  ConnectorConfiguration,
  ComponentConfiguration,
} from '../../types/components';
import type { AgentCoreNode } from '../../store/workflowStore';
import type { EvaluationNodeConfig } from '../modals/EvaluationConfigurationModal';
import type { PromptSelection } from '../modals/PromptLibraryModal';

// Lazy-load all modals
const RuntimeConfigurationModal = lazy(() => import('./RuntimeConfigurationModal').then(m => ({ default: m.RuntimeConfigurationModal })));
const GatewayConfigurationModal = lazy(() => import('./GatewayConfigurationModal').then(m => ({ default: m.GatewayConfigurationModal })));
const IdentityConfigurationModal = lazy(() => import('./IdentityConfigurationModal').then(m => ({ default: m.IdentityConfigurationModal })));
const MemoryConfigurationModal = lazy(() => import('./MemoryConfigurationModal').then(m => ({ default: m.MemoryConfigurationModal })));
const PolicyConfigurationModal = lazy(() => import('./PolicyConfigurationModal').then(m => ({ default: m.PolicyConfigurationModal })));
const GuardrailsConfigurationModal = lazy(() => import('./GuardrailsConfigurationModal').then(m => ({ default: m.GuardrailsConfigurationModal })));
const ObservabilityConfigurationModal = lazy(() => import('./ObservabilityConfigurationModal').then(m => ({ default: m.ObservabilityConfigurationModal })));
const EvaluationConfigurationModal = lazy(() => import('./EvaluationConfigurationModal').then(m => ({ default: m.EvaluationConfigurationModal })));
const ToolConfigModal = lazy(() => import('./ToolConfigModal').then(m => ({ default: m.ToolConfigModal })));
const ConnectorConfigModal = lazy(() => import('./ConnectorConfigModal').then(m => ({ default: m.ConnectorConfigModal })));
const KnowledgeBaseConfigModal = lazy(() => import('./KnowledgeBaseConfigModal').then(m => ({ default: m.KnowledgeBaseConfigModal })));
const A2AConfigurationModal = lazy(() => import('./A2AConfigurationModal').then(m => ({ default: m.A2AConfigurationModal })));

// Modal props interfaces
export interface RuntimeModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: RuntimeConfiguration) => void;
  initialConfig?: RuntimeConfiguration;
  onOpenPromptLibrary?: (onSelect: (selection: PromptSelection) => void) => void;
}

export interface GatewayModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: GatewayConfiguration) => void;
  initialConfig?: GatewayConfiguration;
}

export interface IdentityModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: IdentityConfiguration) => void;
  initialConfig?: IdentityConfiguration;
}

export interface MemoryModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: MemoryConfiguration) => void;
  initialConfig?: MemoryConfiguration;
}

export interface PolicyModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: PolicyConfiguration) => void;
  initialConfig?: PolicyConfiguration;
}

export interface GuardrailsModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: GuardrailsConfiguration) => void;
  initialConfig?: Partial<GuardrailsConfiguration>;
}

export interface ObservabilityModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: ObservabilityConfiguration) => void;
  initialConfig?: Partial<ObservabilityConfiguration>;
  apiBaseUrl: string;
}

export interface EvaluationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: EvaluationNodeConfig) => void;
  initialConfig?: Partial<EvaluationNodeConfig>;
}

export interface ToolModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: ToolConfiguration) => void;
  initialConfig?: Partial<ToolConfiguration>;
}

export interface ConnectorModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: ConnectorConfiguration) => void;
  initialConfig?: Partial<ConnectorConfiguration>;
}

export interface KnowledgeBaseModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: KnowledgeBaseToolConfig) => void;
  initialConfig?: Partial<KnowledgeBaseToolConfig>;
}

export interface A2AModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: A2AConfiguration) => void;
  initialConfig?: Partial<A2AConfiguration>;
}

// Registry entry type
export interface ModalRegistryEntry<P = unknown> {
  component: ComponentType<P>;
  props: P;
}

// Modal registry mapping
export const MODAL_REGISTRY = {
  runtime: RuntimeConfigurationModal,
  gateway: GatewayConfigurationModal,
  identity: IdentityConfigurationModal,
  memory: MemoryConfigurationModal,
  policy: PolicyConfigurationModal,
  guardrails: GuardrailsConfigurationModal,
  observability: ObservabilityConfigurationModal,
  evaluation: EvaluationConfigurationModal,
  tool: ToolConfigModal,
  connector: ConnectorConfigModal,
  knowledgeBase: KnowledgeBaseConfigModal,
  a2a: A2AConfigurationModal,
} as const;

export type ModalKey = keyof typeof MODAL_REGISTRY;

// Helper to check if a component type maps to a modal key
export function getModalKeyForComponentType(
  componentType: AgentCoreComponentType,
  toolConfig?: { isConnector?: boolean; isKnowledgeBase?: boolean; toolId?: string }
): ModalKey | null {
  if (componentType === 'tool') {
    const isConnector = toolConfig?.isConnector || (toolConfig?.toolId && toolConfig.toolId.startsWith('connector:'));
    if (isConnector) return 'connector';
    if (toolConfig?.isKnowledgeBase) return 'knowledgeBase';
    return 'tool';
  }
  if (componentType === 'runtime') return 'runtime';
  if (componentType === 'gateway') return 'gateway';
  if (componentType === 'identity') return 'identity';
  if (componentType === 'memory') return 'memory';
  if (componentType === 'policy') return 'policy';
  if (componentType === 'guardrails') return 'guardrails';
  if (componentType === 'observability') return 'observability';
  if (componentType === 'evaluation') return 'evaluation';
  if (componentType === 'a2a') return 'a2a';
  return null;
}

/**
 * Whether a newly-created palette/drop node should enter App's pending-modal
 * state. Built-in tools are preconfigured, while connector tools need their
 * credential form. Components without a registered modal (currently Browser
 * and Code Interpreter) must never enter that state: there would be no dialog
 * capable of closing it, and every later configuration modal would be blocked.
 */
export function shouldOpenConfigurationForNewNode(
  componentType: AgentCoreComponentType,
  toolId?: string | null,
): boolean {
  if (componentType === 'tool' && !toolId?.startsWith('connector:')) {
    return false;
  }

  return getModalKeyForComponentType(
    componentType,
    toolId ? { toolId } : undefined,
  ) !== null;
}

export interface NewNodeConfigurationTarget {
  nodeId: string;
  componentType: AgentCoreComponentType;
  initialConfig?: ComponentConfiguration;
}

/**
 * Preserve the identity of the node that was just created. Resolving it later
 * from type/position is unsafe because palette positions intentionally repeat
 * after nine additions and multiple tool nodes share the same component type.
 */
export function configurationTargetForNewNode(
  node: AgentCoreNode,
): NewNodeConfigurationTarget | null {
  const initialConfig = node.data.configuration;
  const toolId = (initialConfig as { toolId?: string } | undefined)?.toolId;

  if (!shouldOpenConfigurationForNewNode(node.data.componentType, toolId)) {
    return null;
  }

  return {
    nodeId: node.id,
    componentType: node.data.componentType,
    initialConfig,
  };
}

export function configurationTargetForExistingNode(
  node: AgentCoreNode,
): NewNodeConfigurationTarget | null {
  const initialConfig = node.data.configuration;
  const modalKey = getModalKeyForComponentType(
    node.data.componentType,
    initialConfig as
      | { isConnector?: boolean; isKnowledgeBase?: boolean; toolId?: string }
      | undefined,
  );

  if (!modalKey) {
    return null;
  }

  return {
    nodeId: node.id,
    componentType: node.data.componentType,
    initialConfig,
  };
}
