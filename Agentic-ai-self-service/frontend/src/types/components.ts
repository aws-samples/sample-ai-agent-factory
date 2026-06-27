/**
 * Component configuration types for AgentCore components.
 * Aligned with AWS Bedrock AgentCore primitives.
 */

import type { AgentServerProtocol, PythonRuntime, DeploymentType } from './workflow';

// ============================================================================
// Runtime Configuration
// ============================================================================

export interface RuntimeConfiguration {
  name: string;
  entrypoint: string;
  framework: AgentFramework;
  model: ModelConfiguration;
  systemPrompt: string;
  deploymentType: DeploymentType;
  pythonRuntime: PythonRuntime;
  protocol: AgentServerProtocol;
  idleTimeout: number;
  maxLifetime: number;
  vpcConfig?: VPCConfiguration;
  enableOtel: boolean;
  observability?: ObservabilityConfiguration;
  executionRoleArn?: string;
  // Strands model provider
  modelProvider: StrandsModelProvider;
  providerApiKeyRef?: string;
  // Multi-agent pattern
  multiAgentPattern: MultiAgentPattern;
  multiAgentConfig?: MultiAgentConfig;
}

export type AgentFramework = 'strands_agents';

export type StrandsModelProvider =
  | 'bedrock'
  | 'openai'
  | 'anthropic'
  | 'gemini'
  | 'litellm'
  | 'mistral'
  | 'ollama'
  | 'sagemaker'
  | 'writer'
  | 'llamaapi'
  | 'deepseek'
  | 'groq'
  | 'together';

export type MultiAgentPattern = 'none' | 'graph' | 'swarm' | 'workflow';

export interface AgentDefinition {
  agentId: string;
  name: string;
  systemPrompt: string;
  modelProvider: StrandsModelProvider;
  modelId: string;
  tools: string[];
}

export interface MultiAgentConfig {
  agents: AgentDefinition[];
  // Graph-specific
  edges?: { source: string; target: string; condition?: string }[];
  entryPoint?: string;
  // Workflow-specific
  steps?: { stepId: string; agentIds: string[] }[];
}

export interface ModelConfiguration {
  provider: StrandsModelProvider;
  modelId: string;
  temperature: number;
  topP: number;
}

// Backward compat alias
export type ModelProvider = StrandsModelProvider;

export interface VPCConfiguration {
  subnetIds: string[];
  securityGroupIds: string[];
}

// ============================================================================
// Gateway Configuration
// ============================================================================

export interface GatewayConfiguration {
  name: string;
  targetType: GatewayTargetType;
  targetConfig: GatewayTargetConfig;
  enableSemanticSearch: boolean;
  apiKeyCredentials?: APIKeyCredentials;
  oauth2Credentials?: OAuth2Credentials;
  roleArn?: string;
}

export type GatewayTargetType = 'openapi' | 'lambda' | 'smithy' | 'mcp_server';

export type GatewayTargetConfig =
  | OpenAPITargetConfig
  | LambdaTargetConfig
  | SmithyTargetConfig
  | MCPServerTargetConfig;

export interface OpenAPITargetConfig {
  type: 'openapi';
  specUrl?: string;
  specContent?: string;
}

export interface LambdaTargetConfig {
  type: 'lambda';
  functionArn?: string;
}

export interface SmithyTargetConfig {
  type: 'smithy';
  modelName: string;
}

export interface MCPServerTargetConfig {
  type: 'mcp_server';
  serverUrl: string;
}

export interface APIKeyCredentials {
  apiKey: string;
  credentialLocation: 'header' | 'query';
  credentialParameterName: string;
}

export interface OAuth2Credentials {
  clientId: string;
  clientSecretRef: string;
  discoveryUrl: string;
  scopes: string[];
}

// ============================================================================
// Memory Configuration
// ============================================================================

export interface MemoryStrategyConfig {
  type: ExtractionStrategy;
  name: string;
  description: string;
  namespaces?: string[];
}

export interface MemoryConfiguration {
  name: string;
  enabled: boolean;
  eventExpiryDuration?: number;
  strategies?: MemoryStrategyConfig[];
}

// ============================================================================
// Code Interpreter Configuration
// ============================================================================

export interface CodeInterpreterConfiguration {
  name: string;
  enabled: boolean;
}

// ============================================================================
// Browser Configuration
// ============================================================================

export interface BrowserConfiguration {
  name: string;
  enabled: boolean;
}

// ============================================================================
// Guardrails Configuration
// ============================================================================

export type GuardrailFilterStrength = 'NONE' | 'LOW' | 'MEDIUM' | 'HIGH';

export interface GuardrailContentFilter {
  hate: GuardrailFilterStrength;
  insults: GuardrailFilterStrength;
  sexual: GuardrailFilterStrength;
  violence: GuardrailFilterStrength;
  misconduct: GuardrailFilterStrength;
  prompt_attack: GuardrailFilterStrength;
}

export interface GuardrailPiiFilter {
  type: string;
  action: 'BLOCK' | 'ANONYMIZE';
}

export interface GuardrailDeniedTopic {
  name: string;
  definition: string;
}

export interface GuardrailsConfiguration {
  name: string;
  enabled: boolean;
  mode: 'existing' | 'create_new';
  guardrailId?: string;
  guardrailVersion?: string;
  contentFilters?: Partial<GuardrailContentFilter>;
  piiFilters?: GuardrailPiiFilter[];
  deniedTopics?: GuardrailDeniedTopic[];
  wordFilters?: string[];
}

// ============================================================================
// Observability Configuration
// ============================================================================

export type ObservabilityProvider =
  | 'langfuse'
  | 'custom';

export type OtlpProtocol = 'http/protobuf' | 'grpc';

export interface ObservabilityConfiguration {
  name: string;
  enableOtel: boolean;
  provider: ObservabilityProvider;
  otlpEndpoint?: string;
  otlpProtocol: OtlpProtocol;
  serviceName?: string;
  sampleRate: number;
  resourceAttributes: Record<string, string>;
  authHeaderSecretArn?: string;
  extraHeaders: Record<string, string>;
}

// ============================================================================
// Identity Configuration
// ============================================================================

export interface IdentityConfiguration {
  name: string;
  credentialType: 'oauth2' | 'api_key';
  oauth2Config?: OAuth2Configuration;
  apiKeyConfig?: APIKeyConfiguration;
  // Gap P3.3B — execution-role isolation. 'shared' (default) keeps the Bug-60
  // stack shared role; 'per_agent' opts into a least-privilege per-runtime
  // role (slower first deploy due to IAM propagation). Absent => 'shared'.
  mode?: 'shared' | 'per_agent';
}

export interface OAuth2Configuration {
  provider: OAuth2Provider;
  clientId: string;
  clientSecretRef: string;
  scopes: string[];
  discoveryUrl?: string;
  audience?: string;
  customConfig?: CustomOAuth2Config;
}

export type OAuth2Provider =
  | 'google'
  | 'microsoft'
  | 'github'
  | 'salesforce'
  | 'slack'
  | 'cognito'
  | 'okta'
  | 'azure_ad'
  | 'auth0'
  | 'custom';

export interface CustomOAuth2Config {
  authorizationUrl: string;
  tokenUrl: string;
  userInfoUrl?: string;
}

export interface APIKeyConfiguration {
  keyName: string;
  keyValueRef: string;
  headerName: string;
}

// ============================================================================
// Evaluation Configuration
// ============================================================================

export interface EvaluationConfiguration {
  name: string;
  enabled: boolean;
  evaluators: EvaluatorConfig[];
  mode: 'on_demand' | 'continuous';
  samplingRate: number;
  enableDashboard: boolean;
  extractionStrategy: ExtractionStrategy;
}

export interface EvaluatorConfig {
  evaluatorType: EvaluatorType;
  enabled: boolean;
  threshold: number;
  customConfig?: CustomEvaluatorConfig;
}

export interface CustomEvaluatorConfig {
  evaluatorName: string;
  evaluatorCode: string;
  description?: string;
}

export type EvaluatorType =
  | 'correctness'
  | 'helpfulness'
  | 'faithfulness'
  | 'answer_relevance'
  | 'context_relevance'
  | 'harmfulness'
  | 'maliciousness'
  | 'coherence'
  | 'conciseness'
  | 'tool_selection'
  | 'tool_call_quality'
  | 'sql_correctness'
  | 'summarization_quality'
  | 'custom';

export type ExtractionStrategy = 'semantic' | 'summary' | 'episodic' | 'user_preferences' | 'custom';

// ============================================================================
// Policy Configuration (Cedar-based)
// ============================================================================

export interface PolicyConfiguration {
  name: string;
  enabled: boolean;
  rules: PolicyRule[];
  defaultEffect: PolicyEffect;
  enableNlAuthoring: boolean;
  strictValidation: boolean;
  enableAuditLog: boolean;
}

export interface PolicyRule {
  ruleId: string;
  effect: PolicyEffect;
  principal?: string;
  action?: string;
  resource?: string;
  conditions: PolicyCondition[];
  description?: string;
}

export interface PolicyCondition {
  attribute: string;
  operator: '==' | '!=' | '<' | '>' | '<=' | '>=' | 'in' | 'contains';
  value: string;
}

export type PolicyEffect = 'permit' | 'forbid';

// ============================================================================
// A2A (Agent-to-Agent) Configuration
// ============================================================================

export interface A2AConfiguration {
  name: string;
  enabled: boolean;
  pattern: A2ACommunicationPattern;
  agentEndpoints: AgentEndpoint[];
  timeoutSeconds: number;
  maxRetries: number;
  enableParallelExecution: boolean;
  enableMessageRouting: boolean;
  routingStrategy: 'round_robin' | 'capability_based' | 'load_balanced';
  shareContext: boolean;
  contextWindowSize: number;
  // Gap 3A - fields consumed by the deploy payload / runtime agent card.
  // Mapped at deploy time to backend snake_case (advertisedDescription ->
  // advertised_description, peerAllowlist -> peer_allowlist).
  capabilities?: string[];
  advertisedDescription?: string;
  peerAllowlist?: string[];
}

export interface AgentEndpoint {
  agentId: string;
  endpointUrl: string;
  protocol: 'HTTP' | 'MCP' | 'A2A';
  description?: string;
}

export type A2ACommunicationPattern =
  | 'hierarchical'
  | 'peer_to_peer'
  | 'broadcast'
  | 'handoff'
  | 'orchestrator';

// ============================================================================
// Tool Configuration (standalone tools for Gateway integration)
// ============================================================================

export type ToolId =
  | 'duckduckgo_search'
  | 'web_page_fetcher'
  | 'wikipedia_search'
  | 'weather_api'
  | 'knowledge_base'
  | string;

export interface ToolConfiguration {
  name: string;
  toolId: ToolId;
  description: string;
  enabled: boolean;
  isCustom?: boolean;
  lambdaCode?: string;
  inputSchema?: Record<string, unknown>;
  displayName?: string;
}

// ============================================================================
// Knowledge Base Tool Configuration
// ============================================================================

export type KBMode = 'existing' | 'create_new';
export type KBDataSourceType = 's3' | 'web_crawler' | 'confluence' | 'salesforce' | 'sharepoint';
export type KBChunkingStrategy = 'FIXED_SIZE' | 'SEMANTIC' | 'HIERARCHICAL' | 'NONE';
export type KBVectorStoreType = 's3_vectors' | 'opensearch_serverless' | 'rds';
export type KBParsingStrategy = 'default' | 'bedrock_data_automation' | 'bedrock_foundation_model';

export interface KnowledgeBaseToolConfig extends ToolConfiguration {
  isKnowledgeBase: true;
  kbMode: KBMode;
  // Use Existing KB
  knowledgeBaseId?: string;
  // Create New KB
  kbName?: string;
  kbDescription?: string;
  dataSourceType?: KBDataSourceType;
  chunkingStrategy?: KBChunkingStrategy;
  maxTokens?: number;
  overlapPercentage?: number;
  embeddingModelId?: string;
  // Vector Store
  vectorStoreType?: KBVectorStoreType;
  // S3 Vectors vector store (advanced; optional — defaults to fully managed)
  s3VectorsBucketArn?: string;
  s3VectorsIndexName?: string;
  s3VectorsIndexArn?: string;
  // S3 data source
  s3BucketUri?: string;
  // Web Crawler data source
  webCrawlerUrl?: string;
  webCrawlerScope?: 'HOST_ONLY' | 'SUBDOMAINS';
  // Confluence data source
  confluenceHostUrl?: string;
  confluenceHostType?: 'SAAS' | 'ON_PREMISE';
  confluenceCredentialsSecretArn?: string;
  // Salesforce data source
  salesforceHostUrl?: string;
  salesforceCredentialsSecretArn?: string;
  // SharePoint data source
  sharePointDomain?: string;
  sharePointSiteUrls?: string;
  sharePointTenantId?: string;
  sharePointHostType?: 'ONLINE';
  sharePointCredentialsSecretArn?: string;
  // OpenSearch Serverless vector store
  opensearchCollectionArn?: string;
  opensearchVectorIndexName?: string;
  opensearchVectorField?: string;
  opensearchTextField?: string;
  opensearchMetadataField?: string;
  // RDS Aurora PostgreSQL vector store
  rdsResourceArn?: string;
  rdsCredentialsSecretArn?: string;
  rdsDatabaseName?: string;
  rdsTableName?: string;
  rdsPrimaryKeyField?: string;
  rdsVectorField?: string;
  rdsTextField?: string;
  rdsMetadataField?: string;
  // Parsing Strategy
  parsingStrategy?: KBParsingStrategy;
  parsingModelId?: string;
  parsingPrompt?: string;
  // Custom Transformation Lambda
  transformationLambdaArn?: string;
  transformationS3Uri?: string;
  // Data Deletion Policy
  dataDeletionPolicy?: 'DELETE' | 'RETAIN';
  // KMS Key for transient data encryption
  kmsKeyArn?: string;
  // Shared - RAG foundation model
  foundationModelId?: string;
  // Gap 3C — agentic retrieval strategy. 'simple' (default/absent) keeps the
  // single-shot retrieve_from_kb tool; the others swap in a strategy-specific tool.
  retrievalStrategy?: 'simple' | 'multi_hop' | 'hybrid' | 'reranked';
}

// ============================================================================
// Connector Configuration (Phase A — SaaS connectors)
// ============================================================================

// Connector nodes are `tool`-typed nodes whose `toolId` is prefixed with
// CONNECTOR_TOOL_PREFIX (e.g. "connector:github"). They wire to the gateway
// exactly like tools, but the gateway turns them into OpenAPI MCP targets
// backed by an AgentCore credential provider + Secrets Manager secret.
export const CONNECTOR_TOOL_PREFIX = 'connector:';

// Branded catalog ids understood by backend services/connectors.py, plus the
// generic OpenAPI/MCP connector. `string` keeps it open for future catalog
// growth without a frontend change.
export type ConnectorId =
  | 'jira'
  | 'asana'
  | 'slack'
  | 'github'
  | 'salesforce'
  | 'generic_openapi'
  | string;

// Mirrors AUTH_API_KEY / AUTH_OAUTH2_CC in services/connectors.py.
export type ConnectorAuthMethod = 'api_key' | 'oauth2_cc';

// Where an API key is injected on outbound requests (matches the boto3
// credentialLocation enum: HEADER | QUERY_PARAMETER).
export type ConnectorCredentialLocation = 'HEADER' | 'QUERY_PARAMETER';

export interface ConnectorConfiguration extends ToolConfiguration {
  // Marks this tool node as a SaaS connector so dispatch/extraction can branch.
  isConnector: true;
  connectorId: ConnectorId;
  authMethod: ConnectorAuthMethod;
  // Set true once the secret has been handed to the backend (Secrets Manager).
  // The raw `secretValue` is transient and stripped before the node is
  // persisted — never written to the canvas JSON / DDB.
  configured: boolean;
  // Transient: the raw secret the user typed. Stripped before persist; only
  // forwarded to the deploy payload (which mints a Secrets Manager secret).
  secretValue?: string;
  // Reference to an already-minted secret (returned by the credential POST).
  secretArn?: string;
  // oauth2_cc only
  oauthVendor?: string;
  scopes?: string[];
  clientId?: string;
  discoveryUrl?: string;
  // api_key only (catalog provides sensible defaults)
  credentialLocation?: ConnectorCredentialLocation;
  credentialParameterName?: string;
  credentialPrefix?: string;
  // generic_openapi only — one of a hosted spec URL or an inline spec body.
  specUrl?: string;
  specContent?: string;
}

// ============================================================================
// Union Type for All Component Configurations
// ============================================================================

export type ComponentConfiguration =
  | RuntimeConfiguration
  | GatewayConfiguration
  | MemoryConfiguration
  | CodeInterpreterConfiguration
  | BrowserConfiguration
  | ObservabilityConfiguration
  | IdentityConfiguration
  | EvaluationConfiguration
  | PolicyConfiguration
  | GuardrailsConfiguration
  | A2AConfiguration
  | ToolConfiguration
  | KnowledgeBaseToolConfig
  | ConnectorConfiguration;
