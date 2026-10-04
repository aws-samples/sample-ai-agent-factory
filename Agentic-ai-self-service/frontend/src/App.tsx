import { lazy, Suspense, useState, useCallback, useEffect, useRef, useMemo } from 'react';
import { ComponentPalette } from './components/palette/ComponentPalette';
import { DeployPanel } from './components/deploy/DeployPanel';
import { CanvasArea } from './components/canvas/CanvasArea';
import type { ActiveDeployment } from './components/deploy/ActiveDeploymentBanner';
import { ToolGeneratorPanel } from './components/ai/ToolGeneratorPanel';
import { AgentGeneratorPanel } from './components/ai/AgentGeneratorPanel';
import { HarnessAuthoring } from './components/harness/HarnessAuthoring';
import type { GeneratedCanvasSpec, RegistryCanvasSnapshot } from './services/api';
import { snapshotToCanvas } from './utils/cloneSnapshot';
import { TemplateGallery } from './components/templates';
import type { PromptSelection } from './components/modals/PromptLibraryModal';
import { ModalHost } from './components/modals/ModalHost';
import {
  configurationTargetForExistingNode,
  configurationTargetForNewNode,
  getModalKeyForComponentType,
} from './components/modals/modalRegistry';
import { useWorkflowStore, type AgentCoreNode } from './store/workflowStore';
import { useFlowStore } from './store/flowStore';
import { useAutoSave } from './hooks/useAutoSave';
import { instantiateTemplate } from './utils/templates';
import type { AgentCoreComponentType } from './types/workflow';
import type { WorkflowTemplate } from './types/templates';
import type { ComponentConfiguration, RuntimeConfiguration, IdentityConfiguration, ToolConfiguration, ConnectorConfiguration } from './types/components';
import { CONNECTOR_TOOL_PREFIX } from './types/components';
import type { DeployConnector } from './components/deploy/DeployPanel';
import type { GeneratedTool } from './services/api';
import { resolvePersonaRoute, useScopes } from './auth/scopes';
import { ChatPage } from './components/chat/ChatPage';
import { AppHeader } from './components/AppHeader';
import { createNodeFromDrop } from './utils/dragDrop';
import './App.css';

const PromptLibraryModal = lazy(() => import('./components/modals/PromptLibraryModal').then(
  (module) => ({ default: module.PromptLibraryModal }),
));
const RegistryModal = lazy(() => import('./components/modals/RegistryModal').then(
  (module) => ({ default: module.RegistryModal }),
));
const HitlInboxModal = lazy(() => import('./components/modals/HitlInboxModal').then(
  (module) => ({ default: module.HitlInboxModal }),
));

function App() {
  const {
    activeFlowId,
    activeFlowName,
    error: flowError,
    saveConflict,
    resolveSaveConflict,
  } = useFlowStore();
  // Content on a canvas that no flow owns cannot be saved (useAutoSave requires
  // documentFlowId === activeFlowId) and would be replaced by the sidebar's
  // auto-open of the most recent flow. Measured live: a template chosen while
  // that GET was in flight vanished with no confirmation. Until a flow is active,
  // every control that puts content on the canvas is disabled and says why; the
  // flow store additionally refuses to open a flow over such content.
  const authoringDisabledReason = activeFlowId
    ? null
    : flowError
      ? 'Open or create a flow to start building'
      : 'Opening your flow…';

  // Loom-study Phase 3 — persona routing. t-user (non-admin) accounts land on the
  // end-user ChatPage; admins get the builder. isTypeAdmin comes from the Cognito
  // type group (t-admin). `previewAsEndUser` lets an admin preview the chat (3.2).
  const {
    scopes,
    isTypeAdmin,
    loaded: scopesLoaded,
    hasScope,
  } = useScopes();
  const [previewAsEndUser, setPreviewAsEndUser] = useState(false);

  // Auto-save active flow workflow (only saves when activeFlowId is set).
  // Audit issue #8: surface auto-save errors via a toast so users can see
  // when their work has stopped persisting.
  const { lastSaveError, clearLastSaveError } = useAutoSave(activeFlowId);

  // Phase B — authoring mode. "visual" (default) renders the existing
  // canvas + palette + deploy UI UNCHANGED. "harness" swaps in the additive
  // form-based AgentCore Harness authoring path.
  const [authoringMode, setAuthoringMode] = useState<'visual' | 'harness'>('visual');
  // Bug 193 — connector secrets are stripped from the persisted node config (so
  // they never reach canvas JSON / DDB), but the deploy payload still needs the
  // raw value to mint the Secrets Manager secret. Hold it HERE: an in-memory,
  // never-persisted map keyed by nodeId, written at config-save and read when the
  // deploy payload's connectors[] is built. Cleared on a fresh canvas load.
  const connectorSecretsRef = useRef<Record<string, string>>({});
  const [connectorSecretsRev, setConnectorSecretsRev] = useState(0);
  const [connectorSecrets, setConnectorSecrets] = useState<Record<string, string>>({});

  // Sync connector secrets state with ref whenever it changes
  useEffect(() => {
    setConnectorSecrets({ ...connectorSecretsRef.current });
  }, [connectorSecretsRev]);

  const [paletteCollapsed, setPaletteCollapsed] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [showDeployPanel, setShowDeployPanel] = useState(false);
  const [showTemplateGallery, setShowTemplateGallery] = useState(false);
  const [showToolGenerator, setShowToolGenerator] = useState(false);
  const [showAgentGenerator, setShowAgentGenerator] = useState(false);
  // Phase 3 Gap 3H — prompt management library. `showPromptLibrary` opens it
  // in management mode; `promptPicker` (when set) opens it in picker mode and
  // receives the resolved {promptName, versionId, body} via its onSelect.
  const [showPromptLibrary, setShowPromptLibrary] = useState(false);
  const [promptPicker, setPromptPicker] = useState<((sel: PromptSelection) => void) | null>(null);
  const openPromptPicker = useCallback(
    (onSelect: (selection: PromptSelection) => void) => {
      setPromptPicker(() => onSelect);
    },
    [],
  );
  // Phase 2 Gap 2A — agent registry (browse/clone). Phase 2 Gap 2D — HITL inbox.
  const [showRegistry, setShowRegistry] = useState(false);
  const [showHitlInbox, setShowHitlInbox] = useState(false);
  const [restoredDeployment, setRestoredDeployment] = useState<{
    deploymentId: string;
    runtimeId: string;
    endpoint: string;
    runtimeProtocol: 'HTTP' | 'MCP' | 'A2A';
    gatewayUrl?: string;
  } | null>(null);

  const handleRestoreDeployment = useCallback((deployment: ActiveDeployment) => {
    setRestoredDeployment({
      deploymentId: deployment.deployment_id,
      runtimeId: deployment.runtime_id || deployment.deployment_id,
      endpoint: deployment.runtime_endpoint || '',
      runtimeProtocol: deployment.runtime_protocol || 'HTTP',
      gatewayUrl: deployment.gateway_url,
    });
    setShowDeployPanel(true);
  }, []);

  // Legacy state for extracting config - kept to avoid extensive refactor
  const [configModal, setConfigModal] = useState<{
    isOpen: boolean;
    nodeId: string | null;
    componentType: AgentCoreComponentType | null;
    initialConfig?: ComponentConfiguration;
  }>({ isOpen: false, nodeId: null, componentType: null });

  const {
    nodes,
    edges,
    isReadyToDeploy,
    validationState,
    updateNodeConfiguration,
    selectedNodeId,
    runValidation,
    loadTemplate,
    replaceWorkflowDocument,
    activeTemplateId,
    addNode,
  } = useWorkflowStore();

  // Get selected runtime node for deployment
  const selectedNode = selectedNodeId ? nodes.find((n) => n.id === selectedNodeId) : null;
  const selectedRuntimeConfig = selectedNode?.data.componentType === 'runtime'
    ? selectedNode.data.configuration as RuntimeConfiguration
    : null;

  // Find first HTTP-protocol runtime node if none selected.
  // Prefer HTTP runtimes over MCP runtimes — the MCP server is a target, not the deployable agent.
  // Also exclude runtime nodes that are MCP server targets (connected to a gateway alongside another runtime).
  const mcpServerNodeIds = new Set<string>();
  // Detect multi-runtime-gateway pattern: if a gateway has 2+ runtimes connected, the non-agent ones are MCP servers.
  const gatewayNodes = nodes.filter((n) => n.data.componentType === 'gateway');
  for (const gw of gatewayNodes) {
    const connectedRuntimeIds = edges
      .filter((e) => e.source === gw.id || e.target === gw.id)
      .map((e) => (e.source === gw.id ? e.target : e.source))
      .filter((nid) => nodes.find((n) => n.id === nid)?.data.componentType === 'runtime');
    if (connectedRuntimeIds.length >= 2) {
      // Multiple runtimes on one gateway — identify which is the MCP server.
      // Prefer the runtime with protocol=MCP as the server. If none, pick the one with fewer total connections.
      const runtimeInfos = connectedRuntimeIds.map((rid) => {
        const rn = nodes.find((n) => n.id === rid)!;
        const cfg = rn.data.configuration as RuntimeConfiguration | undefined;
        const totalEdges = edges.filter((e) => e.source === rid || e.target === rid).length;
        return { id: rid, protocol: cfg?.protocol || 'HTTP', totalEdges };
      });
      // First pass: any with MCP protocol is the server
      const mcpOnes = runtimeInfos.filter((r) => r.protocol === 'MCP');
      if (mcpOnes.length > 0) {
        mcpOnes.forEach((r) => mcpServerNodeIds.add(r.id));
      } else {
        // Both HTTP: the one with fewer connections is likely the MCP server (agent has more connections: identity, memory, etc.)
        const sorted = [...runtimeInfos].sort((a, b) => a.totalEdges - b.totalEdges);
        // Mark all except the one with most connections as MCP servers
        sorted.slice(0, -1).forEach((r) => mcpServerNodeIds.add(r.id));
      }
    }
  }

  const firstRuntimeNode = nodes.find((n) => {
    if (n.data.componentType !== 'runtime') return false;
    if (mcpServerNodeIds.has(n.id)) return false; // Exclude MCP server targets
    const cfg = n.data.configuration as RuntimeConfiguration | undefined;
    return !cfg || cfg.protocol !== 'MCP';
  }) || nodes.find((n) => n.data.componentType === 'runtime' && !mcpServerNodeIds.has(n.id))
     || nodes.find((n) => n.data.componentType === 'runtime');
  const deployableConfig = selectedRuntimeConfig || (firstRuntimeNode?.data.configuration as RuntimeConfiguration | undefined);
  // Always use the runtime node's ID (not a selected non-runtime node like a gateway)
  const deployableNodeId = (selectedRuntimeConfig ? selectedNodeId : null) || firstRuntimeNode?.id || null;

  // Get connected tools, gateway config, identity config, custom tools, and MCP server config
  const { tools: connectedTools, gatewayConfig, gatewayTools, identityConfig, customTools, connectors, memoryConfig, evaluationConfig, policyConfig, guardrailsConfig, observabilityConfig, mcpServerConfig, knowledgeBaseConfig, a2aConfig } = useMemo(() => {
    if (!deployableNodeId) return { tools: [], gatewayConfig: null, gatewayTools: [], identityConfig: null, customTools: [], connectors: [] as DeployConnector[], memoryConfig: null, evaluationConfig: null, policyConfig: null, guardrailsConfig: null, observabilityConfig: null, mcpServerConfig: null, knowledgeBaseConfig: null, a2aConfig: null };
    const connectedTools: string[] = [];
    const gatewayTools: string[] = [];
    let gatewayConfig = null;
    let gatewayNodeId: string | null = null;
    let identityConfig: IdentityConfiguration | null = null;
    let memoryConfig: Record<string, unknown> | null = null;
    let evaluationConfig: Record<string, unknown> | null = null;
    let policyConfig: Record<string, unknown> | null = null;
    let guardrailsConfig: Record<string, unknown> | null = null;
    let observabilityConfig: Record<string, unknown> | null = null;
    let a2aConfig: Record<string, unknown> | null = null;
    let mcpServerConfig: Record<string, unknown> | null = null;

    // Find direct connections to the runtime node
    edges.forEach(edge => {
      if (edge.source === deployableNodeId || edge.target === deployableNodeId) {
        const otherNodeId = edge.source === deployableNodeId ? edge.target : edge.source;
        const otherNode = nodes.find(n => n.id === otherNodeId);
        if (otherNode) {
          const type = otherNode.data.componentType;
          if (['browser', 'code_interpreter', 'memory', 'gateway', 'identity', 'observability', 'evaluation', 'policy', 'guardrails', 'a2a'].includes(type)) {
            connectedTools.push(type);
            if (type === 'gateway' && otherNode.data.configuration) {
              gatewayConfig = otherNode.data.configuration;
              gatewayNodeId = otherNode.id;
            }
            if (type === 'identity' && otherNode.data.configuration) {
              identityConfig = otherNode.data.configuration as IdentityConfiguration;
            }
            if (type === 'memory') {
              memoryConfig = (otherNode.data.configuration as unknown as Record<string, unknown>) || { enabled: true };
            }
            if (type === 'evaluation') {
              evaluationConfig = (otherNode.data.configuration as unknown as Record<string, unknown>) || { enabled: true };
            }
            if (type === 'observability') {
              observabilityConfig = (otherNode.data.configuration as unknown as Record<string, unknown>) || { enabled: false };
            }
            if (type === 'a2a') {
              const cfg = (otherNode.data.configuration as unknown as Record<string, unknown>) || {};
              a2aConfig = {
                capabilities: cfg.capabilities || [],
                advertised_description: cfg.advertisedDescription || '',
                peer_allowlist: cfg.peerAllowlist || [],
              };
            }
            if (type === 'policy') {
              policyConfig = (otherNode.data.configuration as unknown as Record<string, unknown>) || { enabled: true };
            }
            if (type === 'guardrails') {
              guardrailsConfig = (otherNode.data.configuration as unknown as Record<string, unknown>) || { enabled: true };
            }
          }
        }
      }
    });

    // Find tool nodes and MCP Server Runtime nodes connected to the gateway
    const customTools: Array<{ toolName: string; displayName: string; description: string; lambdaCode: string; inputSchema: Record<string, unknown> }> = [];
    // Phase A — SaaS connectors. A connector is a `tool`-typed node whose
    // toolId is "connector:<id>". They wire through the gateway like tools but
    // are emitted as a separate `connectors` array (snake_case, see below).
    const connectors: DeployConnector[] = [];
    let knowledgeBaseConfig: Record<string, unknown> | null = null;
    const mcpServerTools: string[] = [];
    if (gatewayNodeId) {
      edges.forEach(edge => {
        if (edge.source === gatewayNodeId || edge.target === gatewayNodeId) {
          const otherNodeId = edge.source === gatewayNodeId ? edge.target : edge.source;
          // Skip the main deployable runtime
          if (otherNodeId === deployableNodeId) return;
          const otherNode = nodes.find(n => n.id === otherNodeId);
          if (otherNode?.data.componentType === 'tool') {
            const toolConfig = otherNode.data.configuration as { toolId?: string; isCustom?: boolean; isKnowledgeBase?: boolean; isConnector?: boolean; lambdaCode?: string; inputSchema?: Record<string, unknown>; displayName?: string; description?: string } | undefined;
            if (toolConfig?.isConnector || toolConfig?.toolId?.startsWith(CONNECTOR_TOOL_PREFIX)) {
              const c = toolConfig as unknown as ConnectorConfiguration;
              const isOauth = c.authMethod === 'oauth2_cc';
              connectors.push({
                connector_id: c.connectorId || (c.toolId || '').slice(CONNECTOR_TOOL_PREFIX.length),
                auth_method: c.authMethod,
                // Transient — minted into Secrets Manager backend-side, then dropped.
                // Read from the in-memory secrets map (the persisted node has the
                // raw value stripped for security); fall back to any inline value.
                secret_value: connectorSecrets[otherNodeId] || c.secretValue || undefined,
                secret_arn: c.secretArn || undefined,
                spec_url: c.specUrl || undefined,
                spec_inline: c.specContent || undefined,
                scopes: isOauth ? (c.scopes || []) : undefined,
                client_id: isOauth ? (c.clientId || undefined) : undefined,
                oauth_vendor: isOauth ? (c.oauthVendor || undefined) : undefined,
                discovery_url: isOauth ? (c.discoveryUrl || undefined) : undefined,
                credential_location: !isOauth ? (c.credentialLocation || undefined) : undefined,
                credential_parameter_name: !isOauth ? (c.credentialParameterName || undefined) : undefined,
                credential_prefix: !isOauth ? (c.credentialPrefix || undefined) : undefined,
              });
            } else if (toolConfig?.toolId === 'knowledge_base' && toolConfig?.isKnowledgeBase) {
              knowledgeBaseConfig = toolConfig as unknown as Record<string, unknown>;
            } else if (toolConfig?.toolId && !toolConfig?.isCustom) {
              gatewayTools.push(toolConfig.toolId);
            }
            if (toolConfig?.isCustom && toolConfig?.lambdaCode) {
              customTools.push({
                toolName: toolConfig.toolId || '',
                displayName: toolConfig.displayName || toolConfig.toolId || '',
                description: toolConfig.description || '',
                lambdaCode: toolConfig.lambdaCode,
                inputSchema: toolConfig.inputSchema || {},
              });
            }
          }
          // Detect Runtime nodes connected to gateway (MCP Server pattern).
          // Any non-deployable runtime connected to the gateway is treated as an MCP server target.
          if (otherNode?.data.componentType === 'runtime' && otherNode.data.configuration) {
            const runtimeCfg = otherNode.data.configuration as RuntimeConfiguration;
            const protocol = runtimeCfg.protocol || 'HTTP';
            // If this runtime has HTTP protocol, it's likely misconfigured — still treat it as MCP server
            // since it's connected to gateway and is not the deployable runtime.
            if (protocol === 'HTTP') {
              console.warn(`Runtime "${runtimeCfg.name}" connected to gateway has HTTP protocol — consider changing to MCP for MCP Server pattern.`);
            }
            mcpServerConfig = {
              name: runtimeCfg.name || 'mcp-server',
              framework: runtimeCfg.framework || 'strands_agents',
              systemPrompt: runtimeCfg.systemPrompt || '',
              model: runtimeCfg.model,
              tools: mcpServerTools, // will be populated from tool nodes connected to this runtime
            };
            // Find tool nodes connected to the MCP Server Runtime
            edges.forEach(mcpEdge => {
              if (mcpEdge.source === otherNodeId || mcpEdge.target === otherNodeId) {
                const mcpToolNodeId = mcpEdge.source === otherNodeId ? mcpEdge.target : mcpEdge.source;
                if (mcpToolNodeId === gatewayNodeId) return; // skip the gateway itself
                const mcpToolNode = nodes.find(n => n.id === mcpToolNodeId);
                if (mcpToolNode?.data.componentType === 'tool') {
                  const mcpToolCfg = mcpToolNode.data.configuration as { toolId?: string } | undefined;
                  if (mcpToolCfg?.toolId) {
                    mcpServerTools.push(mcpToolCfg.toolId);
                  }
                }
              }
            });
          }
        }
      });
    }

    return { tools: connectedTools, gatewayConfig, gatewayTools, identityConfig, customTools, connectors, memoryConfig, evaluationConfig, policyConfig, guardrailsConfig, observabilityConfig, mcpServerConfig, knowledgeBaseConfig, a2aConfig };
  }, [deployableNodeId, edges, nodes, connectorSecrets]);

  // Close config modal
  const handleCloseConfig = useCallback(() => {
    setConfigModal({ isOpen: false, nodeId: null, componentType: null });
  }, []);

  // Save configuration and run validation.
  // SECURITY: connector secrets are transient. The raw `secretValue` is stripped
  // before the node is persisted so it never reaches the canvas JSON / DDB —
  // only Secrets Manager holds it (backend mints from the deploy payload). The
  // node keeps just `configured` + non-secret fields.
  const handleSaveConfig = useCallback((config: ComponentConfiguration) => {
    if (configModal.nodeId) {
      const persisted = { ...config } as ComponentConfiguration & { secretValue?: string };
      // Capture the transient secret into the in-memory map (keyed by nodeId)
      // BEFORE stripping it from the persisted node — the deploy payload reads it
      // back from here so the backend can mint the Secrets Manager secret.
      if (persisted.secretValue) {
        connectorSecretsRef.current[configModal.nodeId] = persisted.secretValue;
        setConnectorSecretsRev(r => r + 1);
      }
      if ('secretValue' in persisted) delete persisted.secretValue;
      updateNodeConfiguration(configModal.nodeId, persisted);
      // Run validation after config update
      setTimeout(() => runValidation(), 10);
    }
    handleCloseConfig();
  }, [configModal.nodeId, updateNodeConfiguration, runValidation, handleCloseConfig]);

  // Compute activeModal props from configModal state (no effect needed)
  const activeModal = useMemo(() => {
    if (!configModal.isOpen || !configModal.componentType || !configModal.nodeId) {
      return { key: null, props: null };
    }
    const cfg = configModal.initialConfig as Record<string, unknown> | undefined;
    const modalKey = getModalKeyForComponentType(configModal.componentType, cfg);
    if (!modalKey) {
      return { key: null, props: null };
    }
    return {
      key: modalKey,
      props: {
        isOpen: true,
        onClose: handleCloseConfig,
        onSave: handleSaveConfig,
        initialConfig: configModal.initialConfig,
        // Observability needs apiBaseUrl
        ...(modalKey === 'observability' ? { apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? '' } : {}),
        ...(modalKey === 'runtime' ? { onOpenPromptLibrary: openPromptPicker } : {}),
      },
    };
  }, [configModal.isOpen, configModal.componentType, configModal.nodeId, configModal.initialConfig, handleCloseConfig, handleSaveConfig, openPromptPicker]);

  const handleToggleCollapse = useCallback(() => {
    setPaletteCollapsed((prev) => !prev);
  }, []);

  const handleSearchChange = useCallback((query: string) => {
    setSearchQuery(query);
  }, []);

  // Open config modal for a node
  const handleOpenConfig = useCallback((nodeId: string) => {
    const node = nodes.find((n) => n.id === nodeId);
    if (!node) return;
    const target = configurationTargetForExistingNode(node);
    if (!target) return;
    setConfigModal({
      isOpen: true,
      ...target,
    });
  }, [nodes]);

  // The creator hands us the exact node. Do not re-find it by type/position:
  // keyboard placement wraps after nine nodes and tool nodes share one type.
  const handleNodeCreate = useCallback((newNode: AgentCoreNode) => {
    const target = configurationTargetForNewNode(newNode);
    if (!target) return;
    setConfigModal({
      isOpen: true,
      ...target,
    });
  }, []);

  // Keyboard and switch-device users cannot perform an HTML drag operation.
  // Palette add buttons create the same node shape and follow the same
  // configuration-modal path as a drop. Stagger the default position so
  // successive additions remain individually visible on an empty viewport.
  const handlePaletteAdd = useCallback((
    componentType: AgentCoreComponentType,
    toolId?: string,
  ) => {
    if (!activeFlowId) return;
    const index = nodes.length;
    const position = {
      x: 100 + (index % 3) * 240,
      y: 100 + (Math.floor(index / 3) % 3) * 180,
    };
    const newNode = createNodeFromDrop(componentType, position, toolId);
    addNode(newNode);
    handleNodeCreate(newNode);
  }, [activeFlowId, addNode, handleNodeCreate, nodes.length]);

  // Handle template selection
  const handleSelectTemplate = useCallback((template: WorkflowTemplate) => {
    if (!activeFlowId) return;
    // New canvas content => drop any transient connector secrets from the old one.
    connectorSecretsRef.current = {};
    setConnectorSecretsRev(r => r + 1);
    const { nodes: templateNodes, edges: templateEdges } = instantiateTemplate(template);
    loadTemplate(templateNodes, templateEdges, template.id);
  }, [activeFlowId, loadTemplate]);

  // Phase 1 Gap 1E — apply NL-generated canvas spec.
  // Adapts the generator's spec shape onto the existing
  // instantiateTemplate / loadTemplate pipeline so a generated agent
  // lands on the canvas exactly like a hand-built template would.
  // Phase 2 Gap 2A — apply a CLONED registry snapshot.
  // A registry snapshot is a RAW React-Flow canvas ({name, nodes, edges} exactly
  // as the store holds it — captured verbatim at publish time), NOT the NL
  // generator's {idSuffix, configuration, sourceIdSuffix} spec shape. It must be
  // loaded DIRECTLY via loadTemplate — routing it through handleApplyGeneratedSpec
  // (as this used to) mis-reads n.idSuffix/n.configuration (undefined on real
  // nodes) and e.sourceIdSuffix/targetIdSuffix, silently DROPPING every edge —
  // so a Runtime→Memory / Gateway→tool wiring came back unwired and generated a
  // broken template. Clone REPLACES the canvas, so the snapshot's internal node
  // ids are self-consistent and need no remap.
  const handleCloneSnapshot = useCallback((snapshot: RegistryCanvasSnapshot) => {
    if (!activeFlowId) return;
    const cloned = snapshotToCanvas(snapshot);
    const { nodes } = cloned;
    if (!nodes.length) {
      // Defensive: an empty/legacy snapshot — surface it rather than silently
      // loading a blank canvas that then "generates an incorrect template".
      console.warn('Clone: snapshot had no nodes; nothing to load', snapshot);
      return;
    }
    connectorSecretsRef.current = {};
    setConnectorSecretsRev((revision) => revision + 1);
    replaceWorkflowDocument(cloned, {
      flowId: activeFlowId,
      markDirty: true,
    });
  }, [activeFlowId, replaceWorkflowDocument]);

  const handleApplyGeneratedSpec = useCallback((spec: GeneratedCanvasSpec) => {
    if (!activeFlowId) return;
    const fakeTemplate = {
      id: `ai-generated-${Date.now()}`,
      name: spec.name,
      description: spec.description ?? '',
      longDescription: spec.rationale ?? '',
      icon: '✨',
      difficulty: 'intermediate' as const,
      tags: ['ai-generated'],
      componentTypes: [],
      builtInTools: [],
      nodes: spec.nodes.map((n) => ({
        idSuffix: n.idSuffix,
        type: n.type as never,
        label: n.label,
        position: n.position,
        configuration: n.configuration as never,
      })),
      edges: spec.edges.map((e) => ({
        sourceIdSuffix: e.sourceIdSuffix,
        targetIdSuffix: e.targetIdSuffix,
        connectionType: e.connectionType as never,
      })),
    };
    const { nodes: instNodes, edges: instEdges } = instantiateTemplate(
      fakeTemplate as unknown as WorkflowTemplate,
    );
    loadTemplate(instNodes, instEdges, fakeTemplate.id);
    // Run validation after template loads to surface any issues
    setTimeout(() => runValidation(), 10);
  }, [activeFlowId, loadTemplate, runValidation]);

  // Handle AI-generated tool → add as custom tool node on canvas
  const handleAddGeneratedTool = useCallback((tool: GeneratedTool) => {
    if (!activeFlowId) return;
    const toolConfig: ToolConfiguration = {
      name: tool.displayName,
      toolId: tool.toolName,
      description: tool.description,
      enabled: true,
      isCustom: true,
      lambdaCode: tool.lambdaCode,
      inputSchema: tool.inputSchema,
      displayName: tool.displayName,
    };

    // Place at a reasonable position on the canvas
    const existingToolNodes = nodes.filter(n => n.data.componentType === 'tool');
    const yOffset = existingToolNodes.length * 80;

    addNode({
      id: `tool-ai-${Date.now()}`,
      type: 'agentComponent',
      position: { x: 700, y: 150 + yOffset },
      data: {
        label: tool.displayName,
        componentType: 'tool',
        configuration: toolConfig,
        validationStatus: 'valid',
      },
    });

    setShowToolGenerator(false);
  }, [activeFlowId, nodes, addNode]);

  // Check if we have a valid runtime to deploy
  const canDeploy = (
    deployableConfig
    && deployableConfig.name
    && (
      deployableConfig.protocol === 'MCP'
      || (
        deployableConfig.systemPrompt
        && deployableConfig.model?.modelId
      )
    )
  );
  const personaRoute = resolvePersonaRoute({
    loaded: scopesLoaded,
    scopes,
    isTypeAdmin,
    previewAsEndUser,
  });

  if (personaRoute === 'loading') {
    return (
      <main
        className="w-screen h-screen flex items-center justify-center bg-[#f2f3f3]"
        aria-busy="true"
      >
        <p role="status" className="text-sm text-[#5f6b7a]">
          Loading your workspace access…
        </p>
      </main>
    );
  }

  if (personaRoute === 'denied') {
    return (
      <main className="w-screen h-screen flex items-center justify-center bg-[#f2f3f3] p-6">
        <section
          className="max-w-lg rounded-xl border border-[#d5dbdb] bg-white p-6 text-center shadow-sm"
          aria-labelledby="workspace-access-denied"
        >
          <h1 id="workspace-access-denied" className="text-lg font-semibold text-[#16191f]">
            Workspace access is not assigned
          </h1>
          <p className="mt-2 text-sm text-[#5f6b7a]">
            Your account has no builder or invocation scopes. Ask an administrator to
            assign the appropriate Cognito groups, then sign in again.
          </p>
        </section>
      </main>
    );
  }

  if (personaRoute === 'chat') {
    const banner = previewAsEndUser ? (
      <div className="px-4 py-2 text-xs flex items-center justify-between" style={{ background: 'rgba(245,166,35,.12)', borderBottom: '1px solid var(--accent)', color: 'var(--color-text-secondary)' }}>
        <span>👁 Previewing the end-user experience (View as).</span>
        <button type="button" onClick={() => setPreviewAsEndUser(false)} className="font-medium hover:underline" style={{ color: 'var(--accent)' }}>
          Exit preview
        </button>
      </div>
    ) : undefined;
    return <ChatPage previewBanner={banner} />;
  }

  // Harness mode swaps in the additive form-based authoring path.
  if (authoringMode === 'harness') {
    return (
      <div className="w-screen h-screen flex flex-col bg-[#f2f3f3]">
        <AppHeader
          activeFlowName={activeFlowName}
          nodesCount={nodes.length}
          deployableConfig={deployableConfig}
          isReadyToDeploy={isReadyToDeploy}
          validationErrorCount={validationState?.errors.length ?? 0}
          authoringMode={authoringMode}
          onAuthoringModeChange={setAuthoringMode}
          onDeploy={() => setShowDeployPanel(true)}
          onOpenRegistry={() => setShowRegistry(true)}
          onPreviewAsEndUser={() => setPreviewAsEndUser(true)}
          onOpenHitlInbox={() => setShowHitlInbox(true)}
          canDeploy={!!canDeploy && hasScope('agent:write')}
          canOpenRegistry={hasScope('registry:read')}
          showAdminControls={isTypeAdmin}
          canOpenHitlInbox={isTypeAdmin && hasScope('hitl:read')}
        />
        <main className="flex-1 min-h-0 flex">
          {/* Keep every tab's aria-controls target in the accessibility tree's DOM
              contract even while its expensive authoring surface is unmounted. */}
          <div
            id="authoring-panel-visual"
            role="tabpanel"
            aria-labelledby="authoring-mode-visual"
            hidden
          />
          <div
            id="authoring-panel-harness"
            role="tabpanel"
            aria-labelledby="authoring-mode-harness"
            className="flex-1 min-h-0 flex"
          >
            <HarnessAuthoring />
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className="w-screen h-screen flex bg-[#f2f3f3]">
      <ComponentPalette
        collapsed={paletteCollapsed}
        onToggleCollapse={handleToggleCollapse}
        searchQuery={searchQuery}
        onSearchChange={handleSearchChange}
        onOpenTemplates={() => setShowTemplateGallery(true)}
        onOpenToolGenerator={() => setShowToolGenerator(true)}
        onOpenAgentGenerator={() => setShowAgentGenerator(true)}
        onOpenRegistry={hasScope('registry:read') ? () => setShowRegistry(true) : undefined}
        onOpenPromptLibrary={hasScope('prompt:read') ? () => setShowPromptLibrary(true) : undefined}
        onAddComponent={handlePaletteAdd}
        authoringDisabledReason={authoringDisabledReason}
      />

      <div className="flex-1 relative flex flex-col">
        <AppHeader
          activeFlowName={activeFlowName}
          nodesCount={nodes.length}
          deployableConfig={deployableConfig}
          isReadyToDeploy={isReadyToDeploy}
          validationErrorCount={validationState?.errors.length ?? 0}
          authoringMode={authoringMode}
          onAuthoringModeChange={setAuthoringMode}
          onDeploy={() => setShowDeployPanel(true)}
          onOpenRegistry={() => setShowRegistry(true)}
          onPreviewAsEndUser={() => setPreviewAsEndUser(true)}
          onOpenHitlInbox={() => setShowHitlInbox(true)}
          canDeploy={!!canDeploy && hasScope('agent:write')}
          canOpenRegistry={hasScope('registry:read')}
          showAdminControls={isTypeAdmin}
          canOpenHitlInbox={isTypeAdmin && hasScope('hitl:read')}
        />

        <main className="flex-1 min-h-0 flex">
          <div
            id="authoring-panel-visual"
            role="tabpanel"
            aria-labelledby="authoring-mode-visual"
            className="flex-1 min-h-0 flex"
          >
            <CanvasArea
              nodes={nodes}
              selectedNode={selectedNode || null}
              lastSaveError={lastSaveError && !saveConflict ? lastSaveError.message : null}
              saveConflict={saveConflict}
              onResolveSaveConflict={(choice) => { void resolveSaveConflict(choice); }}
              onNodeCreate={handleNodeCreate}
              onNodeDoubleClick={handleOpenConfig}
              onRestoreDeployment={handleRestoreDeployment}
              onClearSaveError={clearLastSaveError}
              onOpenTemplateGallery={() => setShowTemplateGallery(true)}
              onOpenAgentGenerator={() => setShowAgentGenerator(true)}
              onOpenConfig={handleOpenConfig}
              authoringDisabledReason={authoringDisabledReason}
            />
          </div>
          {/* The Harness form stays unmounted in visual mode, but the controlled
              tabpanel itself must exist so aria-controls is never a dangling IDREF. */}
          <div
            id="authoring-panel-harness"
            role="tabpanel"
            aria-labelledby="authoring-mode-harness"
            hidden
          />
        </main>
      </div>

      {/* Deploy Panel */}
      <DeployPanel
        config={deployableConfig || null}
        nodeId={deployableNodeId}
        flowId={activeFlowId}
        connectedTools={connectedTools}
        gatewayConfig={gatewayConfig}
        gatewayTools={gatewayTools}
        templateId={activeTemplateId}
        identityConfig={identityConfig}
        customTools={customTools}
        connectors={connectors}
        memoryConfig={memoryConfig}
        evaluationConfig={evaluationConfig}
        policyConfig={policyConfig}
        guardrailsConfig={guardrailsConfig}
        mcpServerConfig={mcpServerConfig}
        knowledgeBaseConfig={knowledgeBaseConfig}
        observabilityConfig={observabilityConfig}
        a2aConfig={a2aConfig}
        isVisible={showDeployPanel}
        onClose={() => setShowDeployPanel(false)}
        restoredDeployment={restoredDeployment}
      />

      {/* Configuration Modals - registry-driven */}
      <ModalHost modalKey={activeModal.key} modalProps={activeModal.props} />

      {/* Template Gallery Modal */}
      <TemplateGallery
        isOpen={showTemplateGallery}
        onClose={() => setShowTemplateGallery(false)}
        onSelectTemplate={handleSelectTemplate}
        hasExistingNodes={nodes.length > 0}
      />

      {/* AI Tool Generator Panel */}
      <ToolGeneratorPanel
        isVisible={showToolGenerator}
        onClose={() => setShowToolGenerator(false)}
        onAddToolToCanvas={handleAddGeneratedTool}
      />

      {/* Phase 1 Gap 1E — NL Agent Generator Panel */}
      <AgentGeneratorPanel
        isVisible={showAgentGenerator}
        onClose={() => setShowAgentGenerator(false)}
        onApplySpec={handleApplyGeneratedSpec}
        hasExistingNodes={nodes.length > 0}
      />

      <Suspense fallback={null}>
        {/* Phase 3 Gap 3H — Prompt Management Library */}
        <PromptLibraryModal
          isOpen={showPromptLibrary || promptPicker !== null}
          mode={promptPicker !== null ? 'picker' : 'management'}
          onClose={() => { setShowPromptLibrary(false); setPromptPicker(null); }}
          onSelect={(sel) => { promptPicker?.(sel); setPromptPicker(null); }}
        />

        {/* Phase 2 Gap 2A — Agent Registry (browse / clone to canvas) */}
        <RegistryModal
          isOpen={showRegistry}
          onClose={() => setShowRegistry(false)}
          onClone={(snapshot) => {
            handleCloneSnapshot(snapshot);
            setShowRegistry(false);
          }}
        />

        {/* Phase 2 Gap 2D — Human-in-the-loop approvals inbox */}
        <HitlInboxModal
          isOpen={showHitlInbox}
          onClose={() => setShowHitlInbox(false)}
        />
      </Suspense>
    </div>
  );
}

export default App;
