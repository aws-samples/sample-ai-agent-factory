/**
 * Canvas → deploy-request derivation, as one pure function.
 *
 * This is the exact mapping `App.tsx` applied inline (in a `useMemo`) before Spec v2:
 * walk the edges around the deployable runtime node, collect the connected component
 * types and their configurations, then walk the gateway's edges for tool, connector,
 * knowledge-base and MCP-server-runtime nodes. It is extracted, unchanged in behaviour,
 * so that
 *
 *   1. the frontend can unit-test it directly instead of through the App shell, and
 *   2. `backend/src/app/services/spec_compiler.py` has a recorded reference to match:
 *      `src/data/templates.specV2Fixtures.test.ts` writes, for every gallery template,
 *      the canvas this derives from and the request it derives, and the backend's
 *      Spec v2 compiler must reproduce that request byte for byte from the migrated spec.
 *
 * Nothing in here may read React state, the store or `import.meta.env`: the inputs are
 * the arguments, the output is a plain object, and the same inputs give the same output.
 */
import type { Edge } from '@xyflow/react';
import type { AgentCoreNode } from '../store/workflowStore';
import type {
  ConnectorConfiguration,
  GatewayConfiguration,
  IdentityConfiguration,
  RuntimeConfiguration,
} from '../types/components';
import { CONNECTOR_TOOL_PREFIX } from '../types/components';
import type { DeployConnector } from '../components/deploy/DeployPanel';

/** The component types a direct edge to the runtime turns into a `connectedTools` entry. */
export const CONNECTED_COMPONENT_TYPES = [
  'browser',
  'code_interpreter',
  'memory',
  'gateway',
  'identity',
  'observability',
  'evaluation',
  'policy',
  'guardrails',
  'a2a',
] as const;

export interface CustomToolDeployEntry {
  toolName: string;
  displayName: string;
  description: string;
  lambdaCode: string;
  inputSchema: Record<string, unknown>;
}

export interface CanvasDeployInputs {
  tools: string[];
  gatewayConfig: GatewayConfiguration | null;
  gatewayTools: string[];
  identityConfig: IdentityConfiguration | null;
  customTools: CustomToolDeployEntry[];
  connectors: DeployConnector[];
  memoryConfig: Record<string, unknown> | null;
  evaluationConfig: Record<string, unknown> | null;
  policyConfig: Record<string, unknown> | null;
  guardrailsConfig: Record<string, unknown> | null;
  observabilityConfig: Record<string, unknown> | null;
  mcpServerConfig: Record<string, unknown> | null;
  knowledgeBaseConfig: Record<string, unknown> | null;
  a2aConfig: Record<string, unknown> | null;
}

export const EMPTY_CANVAS_DEPLOY_INPUTS: CanvasDeployInputs = {
  tools: [],
  gatewayConfig: null,
  gatewayTools: [],
  identityConfig: null,
  customTools: [],
  connectors: [],
  memoryConfig: null,
  evaluationConfig: null,
  policyConfig: null,
  guardrailsConfig: null,
  observabilityConfig: null,
  mcpServerConfig: null,
  knowledgeBaseConfig: null,
  a2aConfig: null,
};

/**
 * Derive every canvas-sourced input of the deploy request for `deployableNodeId`.
 *
 * `connectorSecrets` is the in-memory map of transient connector secret values keyed by
 * node id; the persisted node has the raw value stripped, so the deploy reads it from here
 * and falls back to any inline value.
 */
export function deriveCanvasDeployInputs(
  deployableNodeId: string | null,
  nodes: AgentCoreNode[],
  edges: Edge[],
  connectorSecrets: Record<string, string>,
): CanvasDeployInputs {
  if (!deployableNodeId) return { ...EMPTY_CANVAS_DEPLOY_INPUTS, tools: [], gatewayTools: [], customTools: [], connectors: [] };
  const connectedTools: string[] = [];
  const gatewayTools: string[] = [];
  let gatewayConfig: GatewayConfiguration | null = null;
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
        if ((CONNECTED_COMPONENT_TYPES as readonly string[]).includes(type)) {
          connectedTools.push(type);
          if (type === 'gateway' && otherNode.data.configuration) {
            gatewayConfig = otherNode.data.configuration as GatewayConfiguration;
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
  const customTools: CustomToolDeployEntry[] = [];
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
}

/**
 * The identity block the deploy request carries, exactly as `useDeployment` builds it:
 * present only for an OAuth2-configured or per-agent identity node, absent otherwise.
 */
export function identityConfigForRequest(identityConfig: IdentityConfiguration | null): Record<string, unknown> | undefined {
  if (!(identityConfig?.oauth2Config || identityConfig?.mode === 'per_agent')) return undefined;
  return {
    mode: identityConfig?.mode ?? 'shared',
    provider: identityConfig?.oauth2Config?.provider,
    clientId: identityConfig?.oauth2Config?.clientId,
    clientSecretRef: identityConfig?.oauth2Config?.clientSecretRef,
    discoveryUrl: identityConfig?.oauth2Config?.discoveryUrl || '',
    scopes: identityConfig?.oauth2Config?.scopes || [],
    audience: identityConfig?.oauth2Config?.audience || undefined,
  };
}

/**
 * The runtime node the deploy targets when the user has not selected one.
 *
 * Prefer HTTP runtimes over MCP runtimes (the MCP server is a target, not the deployable
 * agent), and exclude runtime nodes that are MCP server targets: on a gateway with two or
 * more runtimes, the ones with protocol MCP are the servers, and when every one is HTTP,
 * all but the most-connected runtime are (the agent carries identity, memory, ... edges).
 */
export function findDefaultDeployableRuntime(nodes: AgentCoreNode[], edges: Edge[]): AgentCoreNode | undefined {
  const mcpServerNodeIds = new Set<string>();
  const gatewayNodes = nodes.filter((n) => n.data.componentType === 'gateway');
  for (const gw of gatewayNodes) {
    const connectedRuntimeIds = edges
      .filter((e) => e.source === gw.id || e.target === gw.id)
      .map((e) => (e.source === gw.id ? e.target : e.source))
      .filter((nid) => nodes.find((n) => n.id === nid)?.data.componentType === 'runtime');
    if (connectedRuntimeIds.length >= 2) {
      const runtimeInfos = connectedRuntimeIds.map((rid) => {
        const rn = nodes.find((n) => n.id === rid)!;
        const cfg = rn.data.configuration as RuntimeConfiguration | undefined;
        const totalEdges = edges.filter((e) => e.source === rid || e.target === rid).length;
        return { id: rid, protocol: cfg?.protocol || 'HTTP', totalEdges };
      });
      const mcpOnes = runtimeInfos.filter((r) => r.protocol === 'MCP');
      if (mcpOnes.length > 0) {
        mcpOnes.forEach((r) => mcpServerNodeIds.add(r.id));
      } else {
        const sorted = [...runtimeInfos].sort((a, b) => a.totalEdges - b.totalEdges);
        sorted.slice(0, -1).forEach((r) => mcpServerNodeIds.add(r.id));
      }
    }
  }

  return nodes.find((n) => {
    if (n.data.componentType !== 'runtime') return false;
    if (mcpServerNodeIds.has(n.id)) return false;
    const cfg = n.data.configuration as RuntimeConfiguration | undefined;
    return !cfg || cfg.protocol !== 'MCP';
  }) || nodes.find((n) => n.data.componentType === 'runtime' && !mcpServerNodeIds.has(n.id))
     || nodes.find((n) => n.data.componentType === 'runtime');
}
