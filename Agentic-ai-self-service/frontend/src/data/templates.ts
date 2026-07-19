/**
 * Prebuilt workflow template definitions.
 * Based on validated patterns from amazon-bedrock-agentcore-samples.
 */

import type { WorkflowTemplate } from '../types/templates';
import type { RuntimeConfiguration, GatewayConfiguration, IdentityConfiguration, MemoryConfiguration, ObservabilityConfiguration, ToolConfiguration } from '../types/components';
import { getRegionPrefix } from '../utils/runtimeConfig';

/** Replace `us.` prefix with the deployment region prefix for Bedrock model IDs. */
const rm = (modelId: string): string =>
  modelId.startsWith('us.') ? `${getRegionPrefix()}.${modelId.slice(3)}` : modelId;

// ============================================================================
// Template Definitions
// ============================================================================

export const WORKFLOW_TEMPLATES: WorkflowTemplate[] = [
  // ──────────────────────────────────────────────────────────────────────────
  // Template 1: Lightweight Web Search Agent (Beginner)
  // Source: boto3 Converse API with DuckDuckGo via urllib
  // ──────────────────────────────────────────────────────────────────────────
  {
    id: 'web-search-agent',
    name: 'Lightweight Web Search Agent',
    description: 'A web search agent using boto3 Converse API with DuckDuckGo search integration.',
    longDescription: 'Deploy a lightweight agent that can search the web using DuckDuckGo via urllib. Uses the boto3 Converse API tool-calling loop for structured, reliable search workflows. Perfect for getting started with AgentCore.',
    icon: '🔍',
    difficulty: 'beginner',
    tags: ['boto3', 'web-search', 'duckduckgo'],
    componentTypes: ['runtime'],
    builtInTools: [
      { name: 'DuckDuckGo Search', icon: '🦆', description: 'Web search via DuckDuckGo API - returns top 5 results' },
      { name: 'Weather (Open-Meteo)', icon: '🌤️', description: 'Real-time weather data via Open-Meteo API - temperature, humidity, wind' },
      { name: 'Web Page Fetcher', icon: '🌐', description: 'Fetches actual page content from URLs for up-to-date information' },
      { name: 'Converse Tool Loop', icon: '🔧', description: 'Automatic tool routing via boto3 Converse API tool-calling loop' },
    ],
    nodes: [
      {
        idSuffix: 'runtime',
        type: 'runtime',
        position: { x: 400, y: 250 },
        label: 'Web Search Agent',
        configuration: {
          name: 'web_search_agent',
          entrypoint: 'agent.py',
          framework: 'strands_agents',
          model: {
            provider: 'anthropic',
            modelId: rm('us.anthropic.claude-haiku-4-5-20251001-v1:0'),
            temperature: 0.7,
            topP: 0.9,
          },
          systemPrompt: 'You are a helpful web search assistant. Use the DuckDuckGo search tool to find relevant URLs, then use the fetch_webpage tool to retrieve the actual page content for up-to-date information. Always fetch page content rather than relying on search snippets alone. Cite your sources.',
          deploymentType: 'direct_code_deploy',
          pythonRuntime: 'PYTHON_3_13',
          protocol: 'HTTP',
          idleTimeout: 300,
          maxLifetime: 3600,
          enableOtel: false,
          modelProvider: 'bedrock',
          multiAgentPattern: 'none',
        } as RuntimeConfiguration,
      },
    ],
    edges: [],
  },

  // ──────────────────────────────────────────────────────────────────────────
  // Template 2: Strands Agent + Gateway (Intermediate)
  // Source: 01-tutorials/02-AgentCore-gateway/04-integration/
  // ──────────────────────────────────────────────────────────────────────────
  {
    id: 'strands-gateway-agent',
    name: 'Strands Agent + Gateway',
    description: 'A Strands agent connected to an MCP Gateway with JWT auth forwarding.',
    longDescription: 'Deploy a Strands-based agent that connects to an MCP Gateway for tool access. Authentication is handled automatically via JWT forwarding — the backend acquires a Cognito token and the agent forwards it to the gateway.',
    icon: '🔌',
    difficulty: 'intermediate',
    tags: ['strands', 'gateway', 'mcp', 'jwt'],
    componentTypes: ['runtime', 'gateway', 'identity'],
    builtInTools: [
      { name: 'MCP Gateway Tools', icon: '🔌', description: 'Dynamic tools discovered via MCP Gateway with semantic search' },
      { name: 'JWT Auth Forwarding', icon: '🔐', description: 'Automatic Cognito JWT forwarding for secure gateway access' },
    ],
    nodes: [
      {
        idSuffix: 'runtime',
        type: 'runtime',
        position: { x: 300, y: 250 },
        label: 'Strands Agent',
        configuration: {
          name: 'strands_gateway_agent',
          entrypoint: 'agent.py',
          framework: 'strands_agents',
          model: {
            provider: 'anthropic',
            modelId: rm('us.anthropic.claude-haiku-4-5-20251001-v1:0'),
            temperature: 0.7,
            topP: 0.9,
          },
          systemPrompt: 'You are a helpful assistant with access to tools through the MCP Gateway. Use available tools to answer user questions. Be precise and helpful.',
          deploymentType: 'direct_code_deploy',
          pythonRuntime: 'PYTHON_3_13',
          protocol: 'HTTP',
          idleTimeout: 300,
          maxLifetime: 3600,
          enableOtel: false,
        } as RuntimeConfiguration,
      },
      {
        idSuffix: 'gateway',
        type: 'gateway',
        position: { x: 600, y: 150 },
        label: 'MCP Gateway',
        configuration: {
          name: 'agent_gateway',
          targetType: 'lambda',
          targetConfig: { type: 'lambda' },
          enableSemanticSearch: true,
        } as GatewayConfiguration,
      },
      {
        idSuffix: 'identity',
        type: 'identity',
        position: { x: 600, y: 350 },
        label: 'Cognito Identity',
        configuration: {
          name: 'cognito_auth',
          credentialType: 'oauth2',
          oauth2Config: {
            provider: 'cognito',
            clientId: '',
            clientSecretRef: '',
            scopes: [],
          },
        } as IdentityConfiguration,
      },
    ],
    edges: [
      { sourceIdSuffix: 'runtime', targetIdSuffix: 'gateway', connectionType: 'data' },
      { sourceIdSuffix: 'runtime', targetIdSuffix: 'identity', connectionType: 'tool' },
    ],
  },

  // ──────────────────────────────────────────────────────────────────────────
  // Template 3: Customer Support Assistant (Advanced)
  // Source: 02-use-cases/customer-support-assistant/
  // ──────────────────────────────────────────────────────────────────────────
  {
    id: 'customer-support-assistant',
    name: 'Customer Support Assistant',
    description: 'Full-stack support agent with memory, gateway tools, and observability.',
    longDescription: 'An advanced customer support agent using Strands with persistent memory, MCP Gateway for tool access, JWT auth forwarding, and OpenTelemetry observability. Demonstrates production patterns; review and harden before production use.',
    icon: '🎧',
    difficulty: 'advanced',
    tags: ['strands', 'support', 'memory', 'gateway', 'observability'],
    componentTypes: ['runtime', 'gateway', 'identity', 'memory', 'observability'],
    builtInTools: [
      { name: 'MCP Gateway Tools', icon: '🔌', description: 'Customer data lookup, order status, and KB articles via Gateway' },
      { name: 'Conversation Memory', icon: '🧠', description: 'Persistent memory for multi-turn customer conversations' },
      { name: 'JWT Auth Forwarding', icon: '🔐', description: 'Automatic Cognito JWT forwarding for secure gateway access' },
      { name: 'OpenTelemetry', icon: '📊', description: 'Distributed tracing and monitoring for production observability' },
    ],
    nodes: [
      {
        idSuffix: 'runtime',
        type: 'runtime',
        position: { x: 250, y: 250 },
        label: 'Support Agent',
        configuration: {
          name: 'support_assistant',
          entrypoint: 'agent.py',
          framework: 'strands_agents',
          model: {
            provider: 'anthropic',
            modelId: rm('us.anthropic.claude-haiku-4-5-20251001-v1:0'),
            temperature: 0.7,
            topP: 0.9,
          },
          systemPrompt: `You are a customer support assistant. You have access to tools through the MCP Gateway to look up customer information, order status, and knowledge base articles.

Guidelines:
- Always greet the customer warmly
- Use available tools to look up relevant information before answering
- If you cannot find the answer, escalate to a human agent
- Keep responses concise and helpful
- Remember context from previous messages in the conversation`,
          deploymentType: 'direct_code_deploy',
          pythonRuntime: 'PYTHON_3_13',
          protocol: 'HTTP',
          idleTimeout: 300,
          maxLifetime: 3600,
          enableOtel: false,
        } as RuntimeConfiguration,
      },
      {
        idSuffix: 'gateway',
        type: 'gateway',
        position: { x: 550, y: 100 },
        label: 'Support Gateway',
        configuration: {
          name: 'support_gateway',
          targetType: 'lambda',
          targetConfig: { type: 'lambda' },
          enableSemanticSearch: true,
        } as GatewayConfiguration,
      },
      {
        idSuffix: 'identity',
        type: 'identity',
        position: { x: 550, y: 250 },
        label: 'Cognito Identity',
        configuration: {
          name: 'support_cognito_auth',
          credentialType: 'oauth2',
          oauth2Config: {
            provider: 'cognito',
            clientId: '',
            clientSecretRef: '',
            scopes: [],
          },
        } as IdentityConfiguration,
      },
      {
        idSuffix: 'memory',
        type: 'memory',
        position: { x: 550, y: 400 },
        label: 'Conversation Memory',
        configuration: {
          name: 'support_memory',
          enabled: true,
        } as MemoryConfiguration,
      },
      {
        idSuffix: 'observability',
        type: 'observability',
        position: { x: 850, y: 250 },
        label: 'OTEL Monitoring',
        configuration: {
          name: 'support_observability',
          enableOtel: false,
        } as ObservabilityConfiguration,
      },
    ],
    edges: [
      { sourceIdSuffix: 'runtime', targetIdSuffix: 'gateway', connectionType: 'data' },
      { sourceIdSuffix: 'runtime', targetIdSuffix: 'identity', connectionType: 'tool' },
      { sourceIdSuffix: 'runtime', targetIdSuffix: 'memory', connectionType: 'tool' },
      { sourceIdSuffix: 'runtime', targetIdSuffix: 'observability', connectionType: 'data' },
    ],
  },
  // ──────────────────────────────────────────────────────────────────────────
  // Template 4: Customer Support Agent (Advanced)
  // Source: 05-blueprints/customer-support-agent-with-agentcore
  // ──────────────────────────────────────────────────────────────────────────
  {
    id: 'customer-support-blueprint',
    name: 'Customer Support Agent (Blueprint)',
    description: 'Full-stack customer support agent with order management, refunds, and memory.',
    longDescription: 'Based on the official AWS blueprint: a full-featured customer support agent with Gateway tools for order lookup, customer info, and refund processing. Demonstrates production patterns; review and harden before production use.',
    icon: '🎧',
    difficulty: 'advanced',
    tags: ['blueprint', 'customer-support', 'gateway', 'memory', 'orders', 'refunds'],
    componentTypes: ['runtime', 'gateway', 'tool', 'memory'],
    builtInTools: [
      { name: 'Get Order', icon: '📦', description: 'Look up order details — items, status, tracking, total' },
      { name: 'Get Customer', icon: '👤', description: 'Customer info with order history summary' },
      { name: 'List Orders', icon: '📋', description: 'List all orders for a customer sorted by date' },
      { name: 'Process Refund', icon: '💰', description: 'Refund processing with amount validation' },
      { name: 'Conversation Memory', icon: '🧠', description: 'Persistent memory across sessions' },
    ],
    nodes: [
      {
        idSuffix: 'runtime',
        type: 'runtime',
        position: { x: 200, y: 250 },
        label: 'Support Agent',
        configuration: {
          name: 'customer_support_agent',
          entrypoint: 'agent.py',
          framework: 'strands_agents',
          model: {
            provider: 'anthropic',
            modelId: rm('us.anthropic.claude-sonnet-5'),
            temperature: 0.7,
            topP: 0.9,
          },
          systemPrompt: `You are a customer support agent. Your role is to answer customer questions about orders, account information, and refund requests.

Guidelines:
- Use the customer's ID to look up their account and orders automatically
- When showing orders, always fetch full order details (get_order) to include item names, quantities, and prices
- Summarize information clearly and concisely for the customer
- For refunds, validate the order exists and the amount before processing

Demo customers: CUST-001 (John Doe), CUST-002 (Jane Smith)`,
          deploymentType: 'direct_code_deploy',
          pythonRuntime: 'PYTHON_3_13',
          protocol: 'HTTP',
          idleTimeout: 300,
          maxLifetime: 3600,
          enableOtel: false,
        } as RuntimeConfiguration,
      },
      {
        idSuffix: 'gateway',
        type: 'gateway',
        position: { x: 500, y: 150 },
        label: 'Support Gateway',
        configuration: {
          name: 'support_gateway',
          targetType: 'lambda',
          targetConfig: { type: 'lambda' },
          enableSemanticSearch: true,
        } as GatewayConfiguration,
      },
      {
        idSuffix: 'tool-get-order',
        type: 'tool',
        position: { x: 800, y: 50 },
        label: 'Get Order',
        configuration: {
          name: 'Get Order',
          toolId: 'get_order',
          description: 'Look up order details by order ID',
          enabled: true,
        } as ToolConfiguration,
      },
      {
        idSuffix: 'tool-get-customer',
        type: 'tool',
        position: { x: 800, y: 150 },
        label: 'Get Customer',
        configuration: {
          name: 'Get Customer',
          toolId: 'get_customer',
          description: 'Look up customer information',
          enabled: true,
        } as ToolConfiguration,
      },
      {
        idSuffix: 'tool-list-orders',
        type: 'tool',
        position: { x: 800, y: 250 },
        label: 'List Orders',
        configuration: {
          name: 'List Orders',
          toolId: 'list_orders',
          description: 'List orders for a customer',
          enabled: true,
        } as ToolConfiguration,
      },
      {
        idSuffix: 'tool-process-refund',
        type: 'tool',
        position: { x: 800, y: 350 },
        label: 'Process Refund',
        configuration: {
          name: 'Process Refund',
          toolId: 'process_refund',
          description: 'Process a refund for an order',
          enabled: true,
        } as ToolConfiguration,
      },
      {
        idSuffix: 'memory',
        type: 'memory',
        position: { x: 500, y: 400 },
        label: 'Conversation Memory',
        configuration: {
          name: 'support_memory',
          enabled: true,
        } as MemoryConfiguration,
      },
    ],
    edges: [
      { sourceIdSuffix: 'runtime', targetIdSuffix: 'gateway', connectionType: 'data' },
      { sourceIdSuffix: 'runtime', targetIdSuffix: 'memory', connectionType: 'tool' },
      { sourceIdSuffix: 'gateway', targetIdSuffix: 'tool-get-order', connectionType: 'tool' },
      { sourceIdSuffix: 'gateway', targetIdSuffix: 'tool-get-customer', connectionType: 'tool' },
      { sourceIdSuffix: 'gateway', targetIdSuffix: 'tool-list-orders', connectionType: 'tool' },
      { sourceIdSuffix: 'gateway', targetIdSuffix: 'tool-process-refund', connectionType: 'tool' },
    ],
  },

  // ──────────────────────────────────────────────────────────────────────────
  // Template 5: MCP Server as Gateway Target (Intermediate)
  // Source: 01-tutorials/02-AgentCore-gateway/05-mcp-server-as-a-target
  // ──────────────────────────────────────────────────────────────────────────
  {
    id: 'mcp-server-gateway-target',
    name: 'MCP Server as Gateway Target',
    description: 'Deploy an MCP server as a Runtime, then connect it as a Gateway target.',
    longDescription: 'Based on the official AWS tutorial: deploy a FastMCP server as an AgentCore Runtime (MCP protocol), then add it as a gateway target. A second Strands agent connects to the gateway to discover and use the MCP server\'s tools. Demonstrates the multi-runtime + gateway pattern.',
    icon: '🔗',
    difficulty: 'intermediate',
    tags: ['mcp-server', 'gateway-target', 'multi-runtime', 'fastmcp'],
    componentTypes: ['runtime', 'gateway'],
    builtInTools: [
      { name: 'MCP Server Runtime', icon: '🛠️', description: 'FastMCP server deployed as an AgentCore Runtime with MCP protocol' },
      { name: 'Gateway Discovery', icon: '🔌', description: 'Agent discovers MCP server tools through the Gateway' },
      { name: 'Order Tools', icon: '📦', description: 'get_order, get_customer, list_orders, process_refund served by the MCP server' },
    ],
    nodes: [
      {
        idSuffix: 'runtime',
        type: 'runtime',
        position: { x: 100, y: 250 },
        label: 'Agent Runtime',
        configuration: {
          name: 'mcp_gateway_agent',
          entrypoint: 'agent.py',
          framework: 'strands_agents',
          model: {
            provider: 'anthropic',
            modelId: rm('us.anthropic.claude-haiku-4-5-20251001-v1:0'),
            temperature: 0.7,
            topP: 0.9,
          },
          systemPrompt: 'You are a helpful assistant with access to order management tools through the MCP Gateway. Use the available tools to help users look up orders, customers, and process refunds.',
          deploymentType: 'direct_code_deploy',
          pythonRuntime: 'PYTHON_3_13',
          protocol: 'HTTP',
          idleTimeout: 300,
          maxLifetime: 3600,
          enableOtel: false,
        } as RuntimeConfiguration,
      },
      {
        idSuffix: 'gateway',
        type: 'gateway',
        position: { x: 450, y: 250 },
        label: 'MCP Gateway',
        configuration: {
          name: 'mcp_server_gateway',
          targetType: 'lambda',
          targetConfig: { type: 'lambda' },
          enableSemanticSearch: true,
        } as GatewayConfiguration,
      },
      {
        idSuffix: 'mcp-server',
        type: 'runtime',
        position: { x: 800, y: 250 },
        label: 'MCP Server Agent',
        configuration: {
          name: 'mcp_server_agent',
          entrypoint: 'agent.py',
          framework: 'strands_agents',
          model: {
            provider: 'anthropic',
            modelId: rm('us.anthropic.claude-haiku-4-5-20251001-v1:0'),
            temperature: 0.7,
            topP: 0.9,
          },
          systemPrompt: 'MCP Server that exposes order management tools: get_order, get_customer, list_orders, and process_refund.',
          deploymentType: 'direct_code_deploy',
          pythonRuntime: 'PYTHON_3_13',
          protocol: 'MCP',
          idleTimeout: 300,
          maxLifetime: 3600,
          enableOtel: false,
        } as RuntimeConfiguration,
      },
    ],
    edges: [
      { sourceIdSuffix: 'runtime', targetIdSuffix: 'gateway', connectionType: 'data' },
      { sourceIdSuffix: 'gateway', targetIdSuffix: 'mcp-server', connectionType: 'data' },
    ],
  },

  // ──────────────────────────────────────────────────────────────────────────
  // Template 6: MCP Server Runtime (Intermediate)
  // Source: Embedded tools served via MCP protocol on Runtime
  // ──────────────────────────────────────────────────────────────────────────
  {
    id: 'mcp-server-runtime',
    name: 'MCP Server Runtime',
    description: 'Host tools directly on the Runtime via MCP protocol — no Gateway or Lambda needed.',
    longDescription: 'Deploy an agent with embedded tools served directly on the AgentCore Runtime. Tools (weather, web search, URL fetch) are Python functions bundled into the runtime. No Gateway, Lambda, or external infrastructure required — the simplest path to a tool-using agent.',
    icon: '🛠️',
    difficulty: 'intermediate',
    tags: ['mcp', 'server', 'embedded-tools', 'no-gateway'],
    componentTypes: ['runtime'],
    builtInTools: [
      { name: 'Weather Lookup', icon: '🌤️', description: 'Get current weather for any city via wttr.in' },
      { name: 'Web Search', icon: '🔍', description: 'DuckDuckGo search for quick information retrieval' },
      { name: 'URL Fetcher', icon: '🌐', description: 'Fetch and extract text from web pages' },
      { name: 'Converse Tool Loop', icon: '🔧', description: 'Automatic tool routing via boto3 Converse API' },
    ],
    nodes: [
      {
        idSuffix: 'runtime',
        type: 'runtime',
        position: { x: 400, y: 250 },
        label: 'MCP Server Agent',
        configuration: {
          name: 'mcp_server_agent',
          entrypoint: 'agent.py',
          framework: 'strands_agents',
          model: {
            provider: 'anthropic',
            modelId: rm('us.anthropic.claude-haiku-4-5-20251001-v1:0'),
            temperature: 0.7,
            topP: 0.9,
          },
          systemPrompt: 'You are a helpful assistant with embedded tools. Use the get_weather tool to check weather, search_web to find information, and fetch_url to read web pages. Always use tools when the user asks for real-time data.',
          deploymentType: 'direct_code_deploy',
          pythonRuntime: 'PYTHON_3_13',
          // Generated agent uses BedrockAgentCoreApp HTTP entrypoint, not FastMCP.
          // Setting protocol: 'MCP' makes AgentCore reject every invocation with 406.
          // See tasks/lessons.md Bug 28. (A real FastMCP server is a v2 effort.)
          protocol: 'HTTP',
          idleTimeout: 300,
          maxLifetime: 3600,
          enableOtel: false,
          modelProvider: 'bedrock',
          multiAgentPattern: 'none',
        } as RuntimeConfiguration,
      },
    ],
    edges: [],
  },
];
