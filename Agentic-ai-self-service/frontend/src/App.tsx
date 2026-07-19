import { useState, useCallback, useEffect, useRef, useMemo } from 'react';
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
import { PromptLibraryModal, type PromptSelection } from './components/modals/PromptLibraryModal';
import { RegistryModal } from './components/modals/RegistryModal';
import { HitlInboxModal } from './components/modals/HitlInboxModal';
import { ModalHost } from './components/modals/ModalHost';
import { getModalKeyForComponentType } from './components/modals/modalRegistry';
import { useWorkflowStore } from './store/workflowStore';
import { useFlowStore } from './store/flowStore';
import { useAutoSave } from './hooks/useAutoSave';
import { instantiateTemplate } from './utils/templates';
import type { AgentCoreComponentType } from './types/workflow';
import type { WorkflowTemplate } from './types/templates';
import type { ComponentConfiguration, RuntimeConfiguration, IdentityConfiguration, ToolConfiguration, ConnectorConfiguration } from './types/components';
import { CONNECTOR_TOOL_PREFIX } from './types/components';
import type { DeployConnector } from './components/deploy/DeployPanel';
import type { GeneratedTool } from './services/api';
import { useScopes } from './auth/scopes';
import { ChatPage } from './components/chat/ChatPage';
import { AppHeader } from './components/AppHeader';
import './App.css';

function App() {
  const { activeFlowId, activeFlowName } = useFlowStore();

  // Loom-study Phase 3 — persona routing. t-user (non-admin) accounts land on the
  // end-user ChatPage; admins get the builder. isTypeAdmin comes from the Cognito
  // type group (t-admin). `previewAsEndUser` lets an admin preview the chat (3.2).
  const { isTypeAdmin, loaded: scopesLoaded } = useScopes();
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
  // Phase 2 Gap 2A — agent registry (browse/clone). Phase 2 Gap 2D — HITL inbox.
  const [showRegistry, setShowRegistry] = useState(false);
  const [showHitlInbox, setShowHitlInbox] = useState(false);
  const [restoredDeployment, setRestoredDeployment] = useState<{
    runtimeId: string;
    endpoint: string;
    gatewayUrl?: string;
  } | null>(null);

  const handleRestoreDeployment = useCallback((deployment: ActiveDeployment) => {
    setRestoredDeployment({
      runtimeId: deployment.runtime_id || deployment.deployment_id,
      endpoint: deployment.runtime_endpoint || '',
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

  // Pending node creation (to open modal after node is added)
  const [pendingNodeConfig, setPendingNodeConfig] = useState<{
    componentType: AgentCoreComponentType;
    position: { x: number; y: number };
  } | null>(null);

  const { nodes, edges, updateNodeConfiguration, selectedNodeId, runValidation, loadTemplate, activeTemplateId, addNode } = useWorkflowStore();

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

  // Handle pending node creation - open modal when node appears (adjust state during render pattern)
  if (pendingNodeConfig) {
    const newNode = nodes.find((n) =>
      n.data.componentType === pendingNodeConfig.componentType &&
      Math.abs(n.position.x - pendingNodeConfig.position.x) < 20 &&
      Math.abs(n.position.y - pendingNodeConfig.position.y) < 20
    );

    if (newNode && !configModal.isOpen) {
      setConfigModal({
        isOpen: true,
        nodeId: newNode.id,
        componentType: pendingNodeConfig.componentType,
        initialConfig: newNode.data.configuration,
      });
      setPendingNodeConfig(null);
    }
  }

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
      },
    };
  }, [configModal.isOpen, configModal.componentType, configModal.nodeId, configModal.initialConfig, handleCloseConfig, handleSaveConfig]);

  const handleToggleCollapse = useCallback(() => {
    setPaletteCollapsed((prev) => !prev);
  }, []);

  const handleSearchChange = useCallback((query: string) => {
    setSearchQuery(query);
  }, []);

  // Open config modal for a node
  const handleOpenConfig = useCallback((nodeId: string) => {
    const node = nodes.find((n) => n.id === nodeId);
    if (node) {
      setConfigModal({
        isOpen: true,
        nodeId,
        componentType: node.data.componentType,
        initialConfig: node.data.configuration,
      });
    }
  }, [nodes]);

  // Handle node creation from drop - set pending to open modal when node appears.
  // Built-in / custom tool nodes come pre-configured, so skip the modal for them.
  // Connector tool nodes need credentials before deploy, so they DO open a modal
  // (the pending effect resolves the new node and dispatches ConnectorConfigModal).
  const handleNodeCreate = useCallback((componentType: AgentCoreComponentType, position: { x: number; y: number }, toolId?: string | null) => {
    if (componentType === 'tool' && !toolId?.startsWith(CONNECTOR_TOOL_PREFIX)) return;
    setPendingNodeConfig({ componentType, position });
  }, []);

  // Handle template selection
  const handleSelectTemplate = useCallback((template: WorkflowTemplate) => {
    // New canvas content => drop any transient connector secrets from the old one.
    connectorSecretsRef.current = {};
    setConnectorSecretsRev(r => r + 1);
    const { nodes: templateNodes, edges: templateEdges } = instantiateTemplate(template);
    loadTemplate(templateNodes, templateEdges, template.id);
  }, [loadTemplate]);

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
    const { nodes, edges } = snapshotToCanvas(snapshot);
    if (!nodes.length) {
      // Defensive: an empty/legacy snapshot — surface it rather than silently
      // loading a blank canvas that then "generates an incorrect template".
      console.warn('Clone: snapshot had no nodes; nothing to load', snapshot);
      return;
    }
    loadTemplate(nodes, edges, `cloned-${Date.now()}`);
    setTimeout(() => runValidation(), 10);
  }, [loadTemplate, runValidation]);

  const handleApplyGeneratedSpec = useCallback((spec: GeneratedCanvasSpec) => {
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
  }, [loadTemplate, runValidation]);

  // Handle AI-generated tool → add as custom tool node on canvas
  const handleAddGeneratedTool = useCallback((tool: GeneratedTool) => {
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
  }, [nodes, addNode]);

  // Check if we have a valid runtime to deploy
  const canDeploy = deployableConfig && deployableConfig.name && deployableConfig.systemPrompt;

  // Loom-study Phase 3 — end-user chat routing. A non-admin (t-user) lands on the
  // ChatPage; an admin can preview it via View-as. Wait for scopes to load so we
  // don't flash the builder before resolving the persona. (Local dev with no
  // Cognito token resolves as admin — the builder — matching the backend default.)
  if (scopesLoaded && (!isTypeAdmin || previewAsEndUser)) {
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
          authoringMode={authoringMode}
          onAuthoringModeChange={setAuthoringMode}
          onDeploy={() => setShowDeployPanel(true)}
          onOpenRegistry={() => setShowRegistry(true)}
          onPreviewAsEndUser={() => setPreviewAsEndUser(true)}
          onOpenHitlInbox={() => setShowHitlInbox(true)}
          canDeploy={!!canDeploy}
        />
        <HarnessAuthoring />
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
        onOpenRegistry={() => setShowRegistry(true)}
      />

      <div className="flex-1 relative flex flex-col">
        <AppHeader
          activeFlowName={activeFlowName}
          nodesCount={nodes.length}
          deployableConfig={deployableConfig}
          authoringMode={authoringMode}
          onAuthoringModeChange={setAuthoringMode}
          onDeploy={() => setShowDeployPanel(true)}
          onOpenRegistry={() => setShowRegistry(true)}
          onPreviewAsEndUser={() => setPreviewAsEndUser(true)}
          onOpenHitlInbox={() => setShowHitlInbox(true)}
          canDeploy={!!canDeploy}
        />

        <CanvasArea
          nodes={nodes}
          selectedNode={selectedNode || null}
          lastSaveError={lastSaveError ? lastSaveError.message : null}
          onNodeCreate={handleNodeCreate}
          onNodeDoubleClick={handleOpenConfig}
          onRestoreDeployment={handleRestoreDeployment}
          onClearSaveError={clearLastSaveError}
          onOpenTemplateGallery={() => setShowTemplateGallery(true)}
          onOpenAgentGenerator={() => setShowAgentGenerator(true)}
          onOpenConfig={handleOpenConfig}
        />
      </div>

      {/* Deploy Panel */}
      <DeployPanel
        config={deployableConfig || null}
        nodeId={deployableNodeId}
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
    </div>
  );
}

export default App;
