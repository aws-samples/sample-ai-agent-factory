import { useState, useCallback, useEffect, useRef, useMemo } from 'react';
import { m } from 'motion/react';
import { signOut } from 'aws-amplify/auth';
import { staggerContainer, fadeRise, pressable, spring } from './lib/motion';
import { ThemeToggle } from './components/ThemeToggle';
import WorkflowCanvas from './components/canvas/WorkflowCanvas';
import { ComponentPalette } from './components/palette/ComponentPalette';
import { RuntimeConfigurationModal } from './components/modals/RuntimeConfigurationModal';
import { GatewayConfigurationModal } from './components/modals/GatewayConfigurationModal';
import { IdentityConfigurationModal } from './components/modals/IdentityConfigurationModal';
import { A2AConfigurationModal } from './components/modals/A2AConfigurationModal';
import { DeployPanel } from './components/deploy/DeployPanel';
import { ActiveDeploymentBanner } from './components/deploy/ActiveDeploymentBanner';
import type { ActiveDeployment } from './components/deploy/ActiveDeploymentBanner';
import { ToolGeneratorPanel } from './components/ai/ToolGeneratorPanel';
import { AgentGeneratorPanel } from './components/ai/AgentGeneratorPanel';
import { HarnessAuthoring } from './components/harness/HarnessAuthoring';
import type { GeneratedCanvasSpec } from './services/api';
import { TemplateGallery } from './components/templates';
import { MemoryConfigurationModal } from './components/modals/MemoryConfigurationModal';
import { PolicyConfigurationModal } from './components/modals/PolicyConfigurationModal';
import { KnowledgeBaseConfigModal } from './components/modals/KnowledgeBaseConfigModal';
import { ToolConfigModal } from './components/modals/ToolConfigModal';
import { ConnectorConfigModal } from './components/modals/ConnectorConfigModal';
import { GuardrailsConfigurationModal } from './components/modals/GuardrailsConfigurationModal';
import { ObservabilityConfigurationModal } from './components/modals/ObservabilityConfigurationModal';
import { EvaluationConfigurationModal, type EvaluationNodeConfig } from './components/modals/EvaluationConfigurationModal';
import { PromptLibraryModal, type PromptSelection } from './components/modals/PromptLibraryModal';
import { RegistryModal } from './components/modals/RegistryModal';
import { HitlInboxModal } from './components/modals/HitlInboxModal';
import { useWorkflowStore } from './store/workflowStore';
import { useFlowStore } from './store/flowStore';
import { useAutoSave } from './hooks/useAutoSave';
import { instantiateTemplate } from './utils/templates';
import type { AgentCoreComponentType } from './types/workflow';
import type { WorkflowTemplate } from './types/templates';
import type { RuntimeConfiguration, GatewayConfiguration, IdentityConfiguration, MemoryConfiguration, PolicyConfiguration, GuardrailsConfiguration, ObservabilityConfiguration, ComponentConfiguration, ToolConfiguration, KnowledgeBaseToolConfig, A2AConfiguration, ConnectorConfiguration } from './types/components';
import { CONNECTOR_TOOL_PREFIX } from './types/components';
import type { DeployConnector } from './components/deploy/DeployPanel';
import type { GeneratedTool } from './services/api';
import './App.css';

function App() {
  const { activeFlowId, activeFlowName } = useFlowStore();

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

  // Modal state
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

  // Phase B — segmented authoring-mode toggle, shared by both modes' top nav.
  // MotionSites: glass badge with refined transitions.
  const authoringToggle = (
    <div className="no-darkmap flex items-center gap-0.5 p-0.5 backdrop-blur-sm rounded-md" style={{ background: 'rgba(255,255,255,0.08)' }} role="tablist" aria-label="Authoring mode">
      <button
        role="tab"
        aria-selected={authoringMode === 'visual'}
        onClick={() => setAuthoringMode('visual')}
        className="no-darkmap px-2.5 py-1 rounded text-xs font-semibold transition-colors duration-200"
        style={{
          transitionTimingFunction: 'var(--ease-out-quint)',
          background: authoringMode === 'visual' ? 'var(--accent)' : 'transparent',
          color: authoringMode === 'visual' ? '#06080f' : 'rgba(255,255,255,0.88)',
          boxShadow: authoringMode === 'visual' ? '0 0 14px -4px var(--accent)' : 'none',
        }}
      >
        Visual Canvas
      </button>
      <button
        role="tab"
        aria-selected={authoringMode === 'harness'}
        onClick={() => setAuthoringMode('harness')}
        className="no-darkmap px-2.5 py-1 rounded text-xs font-semibold transition-colors duration-200"
        style={{
          transitionTimingFunction: 'var(--ease-out-quint)',
          background: authoringMode === 'harness' ? 'var(--accent)' : 'transparent',
          color: authoringMode === 'harness' ? '#06080f' : 'rgba(255,255,255,0.88)',
          boxShadow: authoringMode === 'harness' ? '0 0 14px -4px var(--accent)' : 'none',
        }}
      >
        Harness
      </button>
    </div>
  );

  // Harness mode swaps in the additive form-based authoring path. The visual
  // canvas (palette + canvas + deploy) below is rendered UNCHANGED otherwise.
  if (authoringMode === 'harness') {
    return (
      <div className="w-screen h-screen flex flex-col bg-[#f2f3f3]">
        <div className="h-12 bg-[#232f3e] flex items-center gap-4 px-4 z-20 border-b border-white/10 flex-shrink-0">
          <div className="flex items-center gap-2.5">
            <div className="w-7 h-7 rounded-md bg-[#ff9900] flex items-center justify-center">
              <svg className="w-4 h-4 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
              </svg>
            </div>
            <span className="font-semibold text-white text-sm tracking-tight">AgentCore Flows</span>
          </div>
          {authoringToggle}
        </div>
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
        {/* Top Header Bar */}
        <div
          className="no-darkmap h-12 flex items-center justify-between px-4 z-20 relative"
          style={{
            background: 'var(--header-bg)',
            backdropFilter: 'blur(12px)',
            borderBottom: '1px solid var(--color-border)',
            boxShadow: '0 1px 0 var(--header-hairline)',
          }}
        >
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2.5">
              <div
                className="w-7 h-7 rounded-md flex items-center justify-center"
                style={{
                  background: 'linear-gradient(135deg, var(--neon-cyan), var(--neon-violet))',
                  boxShadow: '0 0 14px -2px var(--neon-cyan)',
                }}
              >
                <svg className="w-4 h-4 text-[#06080f]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
                </svg>
              </div>
              <span className="font-semibold text-sm tracking-tight u-neon-text">AgentCore Flows</span>
            </div>
            <div className="h-5 w-px bg-white/25" />
            <span className="font-medium text-white/95 text-sm">
              {activeFlowName || 'Untitled Flow'}
            </span>
            <div className="h-5 w-px bg-white/25" />
            <div className="flex items-center gap-2 text-xs text-white/80">
              <span className="px-2 py-0.5 backdrop-blur-sm rounded font-normal" style={{ backgroundColor: 'rgba(255, 255, 255, 0.14)' }}>{nodes.length} node{nodes.length !== 1 ? 's' : ''}</span>
            </div>
            <div className="h-5 w-px bg-white/20" />
            {authoringToggle}
          </div>

          <div className="flex items-center gap-3">
            {/* Status indicator - MotionSites glass badge */}
            {deployableConfig && (
              <m.div
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                transition={spring.bouncy}
                className="no-darkmap flex items-center gap-1.5 px-2.5 py-1 backdrop-blur-sm rounded-md text-xs font-semibold border" style={{ backgroundColor: 'rgba(52, 211, 153, 0.22)', color: '#6ee7b7', borderColor: 'rgba(52, 211, 153, 0.5)', boxShadow: '0 0 14px -4px rgba(52,211,153,0.7)' }}
              >
                <div className="w-1.5 h-1.5 rounded-full animate-pulse" style={{ background: '#34d399', boxShadow: '0 0 8px #34d399' }} />
                Ready to deploy
              </m.div>
            )}

            {/* Phase 2 Gap 2A — Registry (browse/clone agents) */}
            <button
              onClick={() => setShowRegistry(true)}
              className="px-3 py-1.5 rounded-md text-sm text-white/85 hover:text-white hover:bg-white/10 transition-colors duration-200 flex items-center gap-1.5"
              style={{ transitionTimingFunction: 'var(--ease-out-quint)' }}
              title="Browse the agent registry"
              aria-label="Browse agent registry"
            >
              <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
              </svg>
              Registry
            </button>
            {/* Phase 2 Gap 2D — HITL approvals inbox */}
            <button
              onClick={() => setShowHitlInbox(true)}
              className="px-3 py-1.5 rounded-md text-sm text-white/85 hover:text-white hover:bg-white/10 transition-colors duration-200 flex items-center gap-1.5"
              style={{ transitionTimingFunction: 'var(--ease-out-quint)' }}
              title="Human-in-the-loop approvals"
              aria-label="Human-in-the-loop approvals inbox"
            >
              <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" />
              </svg>
              Approvals
            </button>

            {/* Deploy Button - MotionSites off-white CTA with rounded-[2px] */}
            <m.button
              onClick={() => setShowDeployPanel(true)}
              disabled={!canDeploy}
              whileHover={canDeploy ? { scale: 1.03 } : undefined}
              whileTap={canDeploy ? { scale: 0.96 } : undefined}
              transition={spring.snappy}
              className={`
                no-darkmap relative overflow-hidden px-4 py-1.5 font-semibold flex items-center gap-2 text-sm
                ${canDeploy ? 'text-[#06080f]' : 'text-white/30 cursor-not-allowed'}
                ${canDeploy ? 'u-gradient-anim' : ''}
              `}
              style={{
                background: canDeploy
                  ? 'linear-gradient(90deg, var(--neon-cyan), var(--neon-violet), var(--neon-magenta))'
                  : 'rgba(255,255,255,0.06)',
                borderRadius: '2px',
                boxShadow: canDeploy ? '0 0 18px -4px var(--neon-cyan)' : 'none',
              }}
              title={!canDeploy ? 'Configure a Runtime node first' : 'Deploy to AgentCore'}
              aria-label={!canDeploy ? 'Configure a Runtime node first' : 'Deploy agent to AgentCore'}
            >
              {/* sheen sweep on hover when enabled */}
              {canDeploy && (
                <span
                  className="pointer-events-none absolute inset-y-0 -left-1/3 w-1/3 opacity-0 group-hover:opacity-100"
                  style={{ background: 'linear-gradient(90deg, transparent, rgba(255,255,255,0.7), transparent)' }}
                />
              )}
              <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7z" />
              </svg>
              Deploy
            </m.button>
            <ThemeToggle />
            <button
              onClick={() => signOut()}
              className="px-3 py-1.5 rounded-md text-sm text-white/80 hover:text-white hover:bg-white/10 transition-colors duration-200"
              style={{ transitionTimingFunction: 'var(--ease-out-quint)' }}
              title="Sign out"
              aria-label="Sign out"
            >
              Sign out
            </button>
          </div>
        </div>

        {/* Canvas Area */}
        <div className="flex-1 relative">
          <WorkflowCanvas
            onNodeCreate={handleNodeCreate}
            onNodeDoubleClick={handleOpenConfig}
          />

          {/* Active Deployment Restore Banner */}
          <ActiveDeploymentBanner
            onRestore={handleRestoreDeployment}
          />

          {/* Auto-save error toast (audit issue #8) — appears bottom-right
              so it doesn't collide with the selected-node info card on the
              bottom-left. Dismissable; auto-cleared on next successful save. */}
          {lastSaveError && (
            <div
              data-testid="autosave-error-toast"
              role="alert"
              className="absolute bottom-4 right-4 z-40 max-w-sm rounded-md border border-red-300 bg-red-50 shadow-md"
            >
              <div className="flex items-start gap-2 px-3 py-2.5">
                <svg
                  className="mt-0.5 h-4 w-4 shrink-0 text-red-500"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={2}
                  aria-hidden="true"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z"
                  />
                </svg>
                <div className="flex-1 min-w-0">
                  <div className="text-[13px] font-semibold text-red-800">
                    Auto-save failed
                  </div>
                  <div className="text-[12px] text-red-700 mt-0.5 break-words">
                    Your recent changes have not been saved. Check your connection and try again.
                  </div>
                </div>
                <button
                  type="button"
                  onClick={clearLastSaveError}
                  aria-label="Dismiss auto-save error"
                  className="-mr-1 -mt-1 rounded p-1 text-red-500 hover:bg-red-100 hover:text-red-700"
                >
                  <svg
                    className="h-3.5 w-3.5"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={2.5}
                    aria-hidden="true"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>
          )}

          {/* Selected Node Info Card - MotionSites hover:scale */}
          {selectedNode && (
            <div
              className="absolute bottom-4 left-4 z-30 bg-white rounded-xl border border-[#e9ebed] p-4 min-w-[240px] transition-transform duration-200"
              style={{
                boxShadow: 'var(--shadow-md)',
                transitionTimingFunction: 'var(--ease-out-quint)',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.transform = 'scale(1.01)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.transform = 'scale(1)';
              }}
            >
              <div className="flex items-start gap-3">
                <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-[#232f3e] to-[#16191f] flex items-center justify-center text-white text-base flex-shrink-0 shadow-sm">
                  {selectedNode.data.componentType === 'runtime' ? '🤖' :
                   selectedNode.data.componentType === 'gateway' ? '🔌' :
                   selectedNode.data.componentType === 'memory' ? '🧠' :
                   selectedNode.data.componentType === 'code_interpreter' ? '💻' :
                   selectedNode.data.componentType === 'browser' ? '🌐' :
                   selectedNode.data.componentType === 'observability' ? '📊' :
                   selectedNode.data.componentType === 'tool' ? '🔧' : '🔑'}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-[#16191f] text-sm truncate tracking-tight">
                    {selectedNode.data.label || selectedNode.data.componentType}
                  </div>
                  <div className="text-xs text-[#5f6b7a] capitalize mt-1 font-light">
                    {selectedNode.data.componentType.replace(/_/g, ' ')}
                  </div>
                </div>
              </div>
              <button
                onClick={() => handleOpenConfig(selectedNode.id)}
                className="mt-3 w-full py-2 px-3 text-sm text-[#0972d3] hover:bg-[#0972d3]/8 active:bg-[#0972d3]/12 rounded-lg transition-colors duration-200 font-medium flex items-center justify-center gap-2 border border-[#0972d3]/25 hover:border-[#0972d3]/40"
                style={{ transitionTimingFunction: 'var(--ease-out-quint)' }}
                aria-label={`Configure ${selectedNode.data.label || selectedNode.data.componentType}`}
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
                Configure
              </button>
            </div>
          )}

          {/* Help hint when no nodes - MotionSites empty state with glass badge */}
          {nodes.length === 0 && (
            <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
              {/* Twin aurora glows behind the headline for cinematic depth */}
              <div
                className="absolute pointer-events-none"
                style={{
                  width: 720, height: 440,
                  backgroundImage:
                    'radial-gradient(40% 50% at 38% 42%, rgba(34,211,238,0.16), transparent 70%), radial-gradient(40% 50% at 64% 55%, rgba(167,139,250,0.16), transparent 70%)',
                  filter: 'blur(10px)',
                }}
              />
              <m.div
                className="relative text-center max-w-xl px-4"
                variants={staggerContainer(0.09, 0.05)}
                initial="hidden"
                animate="visible"
              >
                {/* Glass badge (dark neon) */}
                <m.div variants={fadeRise} className="inline-flex mb-6">
                  <div
                    className="no-darkmap flex items-center gap-2 rounded-full px-3 py-1 text-sm font-medium tracking-tight"
                    style={{
                      background: 'var(--glass-bg)',
                      backdropFilter: 'blur(10px)',
                      border: '1px solid var(--glass-border)',
                      color: 'var(--color-text-secondary)',
                      boxShadow: '0 0 20px -8px var(--neon-cyan)',
                    }}
                  >
                    <span className="relative flex h-1.5 w-1.5">
                      <span className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-75" style={{ background: 'var(--neon-cyan)' }} />
                      <span className="relative inline-flex h-1.5 w-1.5 rounded-full" style={{ background: 'var(--neon-cyan)' }} />
                    </span>
                    Visual Workflow Builder
                  </div>
                </m.div>

                {/* Headline - Instrument Serif italic, neon-gradient clipped */}
                <m.h3
                  variants={fadeRise}
                  className="no-darkmap text-4xl sm:text-5xl md:text-6xl mb-3 leading-tight u-neon-text u-gradient-anim"
                  style={{ fontFamily: 'var(--font-accent)', fontStyle: 'italic', fontWeight: 400 }}
                >
                  Build Your First Agent
                </m.h3>
                <m.p
                  variants={fadeRise}
                  className="no-darkmap text-base sm:text-lg mb-8 font-light tracking-tight leading-relaxed"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
                  Drag components from the sidebar, start with a template, or let AI generate an agent for you.
                </m.p>

                {/* CTAs — neon gradient primary + glass secondary */}
                <m.div variants={fadeRise} className="flex gap-3 justify-center">
                  <m.button
                    {...pressable}
                    onClick={() => setShowTemplateGallery(true)}
                    className="no-darkmap u-gradient-anim pointer-events-auto px-5 py-2.5 text-sm font-semibold"
                    style={{
                      background: 'linear-gradient(90deg, var(--neon-cyan), var(--neon-violet), var(--neon-magenta))',
                      color: '#06080f',
                      borderRadius: '2px',
                      boxShadow: '0 0 22px -6px var(--neon-cyan)',
                    }}
                  >
                    Browse Templates
                  </m.button>
                  <m.button
                    {...pressable}
                    onClick={() => setShowAgentGenerator(true)}
                    className="no-darkmap pointer-events-auto px-5 py-2.5 text-sm font-medium"
                    style={{
                      background: 'var(--glass-bg)',
                      backdropFilter: 'blur(10px)',
                      color: 'var(--neon-cyan)',
                      border: '1px solid color-mix(in srgb, var(--neon-cyan) 40%, transparent)',
                      borderRadius: '2px',
                    }}
                  >
                    Generate with AI
                  </m.button>
                </m.div>
              </m.div>
            </div>
          )}
        </div>
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

      {/* Configuration Modals */}
      {configModal.componentType === 'runtime' && (
        <RuntimeConfigurationModal
          isOpen={configModal.isOpen}
          onClose={handleCloseConfig}
          onSave={(config) => handleSaveConfig(config)}
          initialConfig={configModal.initialConfig as RuntimeConfiguration}
        />
      )}

      {configModal.componentType === 'gateway' && (
        <GatewayConfigurationModal
          isOpen={configModal.isOpen}
          onClose={handleCloseConfig}
          onSave={(config) => handleSaveConfig(config)}
          initialConfig={configModal.initialConfig as GatewayConfiguration}
        />
      )}

      {configModal.componentType === 'identity' && (
        <IdentityConfigurationModal
          isOpen={configModal.isOpen}
          onClose={handleCloseConfig}
          onSave={(config) => handleSaveConfig(config)}
          initialConfig={configModal.initialConfig as IdentityConfiguration}
        />
      )}

      {configModal.componentType === 'memory' && (
        <MemoryConfigurationModal
          isOpen={configModal.isOpen}
          onClose={handleCloseConfig}
          onSave={(config) => handleSaveConfig(config)}
          initialConfig={configModal.initialConfig as MemoryConfiguration}
        />
      )}

      {configModal.componentType === 'policy' && (
        <PolicyConfigurationModal
          isOpen={configModal.isOpen}
          onClose={handleCloseConfig}
          onSave={(config) => handleSaveConfig(config)}
          initialConfig={configModal.initialConfig as PolicyConfiguration}
        />
      )}

      {configModal.componentType === 'guardrails' && (
        <GuardrailsConfigurationModal
          isOpen={configModal.isOpen}
          onClose={handleCloseConfig}
          onSave={(config) => handleSaveConfig(config)}
          initialConfig={configModal.initialConfig as Partial<GuardrailsConfiguration>}
        />
      )}

      {configModal.componentType === 'observability' && (
        <ObservabilityConfigurationModal
          isOpen={configModal.isOpen}
          onClose={handleCloseConfig}
          onSave={(config) => handleSaveConfig(config)}
          initialConfig={configModal.initialConfig as Partial<ObservabilityConfiguration>}
          apiBaseUrl={import.meta.env.VITE_API_BASE_URL ?? ''}
        />
      )}

      {configModal.componentType === 'evaluation' && (
        <EvaluationConfigurationModal
          isOpen={configModal.isOpen}
          onClose={handleCloseConfig}
          onSave={(config) => handleSaveConfig(config)}
          initialConfig={configModal.initialConfig as Partial<EvaluationNodeConfig>}
        />
      )}

      {(() => {
        // Tool-typed nodes fan out to three modals by config shape:
        //   connector (toolId "connector:*" / isConnector) -> ConnectorConfigModal
        //   knowledge base (isKnowledgeBase)               -> KnowledgeBaseConfigModal
        //   everything else (built-in / custom tools)      -> ToolConfigModal
        if (configModal.componentType !== 'tool') return null;
        const cfg = configModal.initialConfig as unknown as Record<string, unknown> | undefined;
        const isConnector =
          !!cfg?.isConnector ||
          (typeof cfg?.toolId === 'string' && (cfg.toolId as string).startsWith(CONNECTOR_TOOL_PREFIX));
        if (isConnector) {
          return (
            <ConnectorConfigModal
              isOpen={configModal.isOpen}
              onClose={handleCloseConfig}
              onSave={(config) => handleSaveConfig(config)}
              initialConfig={configModal.initialConfig as Partial<ConnectorConfiguration>}
            />
          );
        }
        if (cfg?.isKnowledgeBase) {
          return (
            <KnowledgeBaseConfigModal
              isOpen={configModal.isOpen}
              onClose={handleCloseConfig}
              onSave={(config) => handleSaveConfig(config)}
              initialConfig={configModal.initialConfig as Partial<KnowledgeBaseToolConfig>}
            />
          );
        }
        return (
          <ToolConfigModal
            isOpen={configModal.isOpen}
            onClose={handleCloseConfig}
            onSave={(config) => handleSaveConfig(config)}
            initialConfig={configModal.initialConfig as Partial<ToolConfiguration>}
          />
        );
      })()}

      {configModal.componentType === 'a2a' && (
        <A2AConfigurationModal
          isOpen={configModal.isOpen}
          onClose={handleCloseConfig}
          onSave={(config) => handleSaveConfig(config)}
          initialConfig={configModal.initialConfig as Partial<A2AConfiguration>}
        />
      )}

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
          handleApplyGeneratedSpec(snapshot as GeneratedCanvasSpec);
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
