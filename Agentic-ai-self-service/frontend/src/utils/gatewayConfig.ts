/**
 * Gateway configuration utilities including validation.
 * Requirements: 4.1, 4.2, 4.3, 4.4
 */

import type {
  GatewayConfiguration,
  GatewayTargetType,
  GatewayTargetConfig,
  OpenAPITargetConfig,
  LambdaTargetConfig,
  SmithyTargetConfig,
  MCPServerTargetConfig,
} from '../types/components';

// ============================================================================
// Deploy mapping — split mixed targets[] into backend-shaped arrays
// ============================================================================

/** One external MCP server entry, as the deploy API expects it (snake_case). */
export interface McpServerDeployEntry {
  server_id?: string;
  endpoint?: string;
  auth_type?: string;
  name?: string;
  endpoint_vars?: Record<string, string>;
  secret_value?: string;
  oauth?: { client_id?: string; client_secret?: string; discovery_url?: string; scopes?: string[] };
}

export interface GatewayDeployTargets {
  /** All `mcp_server` targets, mapped for the deploy request (secret-carrying). */
  externalMcpServers: McpServerDeployEntry[];
  /** All non-MCP targets (openapi / lambda / smithy) — carried inside
   *  gateway_config.targets to the backend deploy loop. */
  gatewayTargets: GatewayTargetConfig[];
}

/** Map a single MCP-server target config into the deploy entry shape, or
 *  `null` if it isn't wireable yet (no catalog id and no custom URL). */
export function mapMcpTargetToDeployEntry(tc: MCPServerTargetConfig): McpServerDeployEntry | null {
  if (!tc.serverId) return null;
  const isCustom = tc.serverId === '__custom__';
  if (isCustom && !tc.serverUrl) return null;

  const entry: McpServerDeployEntry = isCustom
    ? { endpoint: tc.serverUrl, auth_type: tc.authType || 'none', name: tc.customName || 'custom-mcp' }
    : { server_id: tc.serverId };

  if (tc.endpointVars && Object.keys(tc.endpointVars).length) entry.endpoint_vars = tc.endpointVars;
  if (tc.apiKey) entry.secret_value = tc.apiKey;
  if (tc.oauth?.clientId) {
    entry.oauth = {
      client_id: tc.oauth.clientId,
      client_secret: tc.oauth.clientSecret,
      discovery_url: tc.oauth.discoveryUrl,
      scopes: tc.oauth.scopes,
    };
  }
  return entry;
}

/**
 * Split a gateway config's effective targets into the arrays the deploy path
 * needs: `externalMcpServers` (mcp_server family, secret-carrying, minted
 * backend-side) and `gatewayTargets` (openapi / lambda / smithy, threaded
 * inside gateway_config.targets for the backend target loop).
 */
export function mapGatewayDeployTargets(
  config: GatewayConfiguration | null | undefined,
): GatewayDeployTargets {
  if (!config) return { externalMcpServers: [], gatewayTargets: [] };
  const externalMcpServers: McpServerDeployEntry[] = [];
  const gatewayTargets: GatewayTargetConfig[] = [];
  for (const target of resolveGatewayTargets(config)) {
    if (target.type === 'mcp_server') {
      const entry = mapMcpTargetToDeployEntry(target as MCPServerTargetConfig);
      if (entry) externalMcpServers.push(entry);
    } else {
      gatewayTargets.push(target);
    }
  }
  return { externalMcpServers, gatewayTargets };
}

// ============================================================================
// Target Type Options
// ============================================================================

export interface TargetTypeOption {
  value: GatewayTargetType;
  label: string;
  description: string;
}

export const TARGET_TYPE_OPTIONS: TargetTypeOption[] = [
  { value: 'openapi', label: 'OpenAPI', description: 'Import tools from OpenAPI specification' },
  { value: 'lambda', label: 'AWS Lambda', description: 'Invoke Lambda functions as tools' },
  { value: 'smithy', label: 'Smithy Model', description: 'Use pre-configured Smithy models (e.g., DynamoDB)' },
  { value: 'mcp_server', label: 'MCP Server', description: 'Connect to existing MCP servers' },
];

// ============================================================================
// Lambda ARN Validation
// ============================================================================

/**
 * Validate Lambda function ARN format.
 */
export function isValidLambdaArn(arn: string): boolean {
  if (!arn || typeof arn !== 'string') {
    return false;
  }

  const lambdaArnPattern = /^arn:aws:lambda:[a-z]{2}(-gov)?-[a-z]+-\d{1}:\d{12}:function:[a-zA-Z0-9_-]{1,64}(:\$LATEST|:[a-zA-Z0-9_-]+)?$/;
  return lambdaArnPattern.test(arn);
}

// ============================================================================
// Default Configurations
// ============================================================================

export function createDefaultTargetConfig(targetType: GatewayTargetType): GatewayTargetConfig {
  switch (targetType) {
    case 'openapi':
      return { type: 'openapi', specUrl: '', specContent: '' } as OpenAPITargetConfig;
    case 'lambda':
      return { type: 'lambda', functionArn: '' } as LambdaTargetConfig;
    case 'smithy':
      return { type: 'smithy', modelName: 'dynamodb' } as SmithyTargetConfig;
    case 'mcp_server':
      return { type: 'mcp_server', serverUrl: '' } as MCPServerTargetConfig;
    default:
      return { type: 'lambda', functionArn: '' } as LambdaTargetConfig;
  }
}

export function createDefaultGatewayConfig(): GatewayConfiguration {
  return {
    name: '',
    targetType: 'lambda',
    targetConfig: createDefaultTargetConfig('lambda'),
    enableSemanticSearch: true,
  };
}

/**
 * Resolve the effective list of gateway targets. When `targets[]` is present
 * and non-empty it is the source of truth; otherwise fall back to the single
 * legacy `targetConfig`. This keeps existing single-target canvases working
 * while letting a gateway wire multiple targets of different families.
 */
export function resolveGatewayTargets(config: GatewayConfiguration): GatewayTargetConfig[] {
  if (config.targets && config.targets.length > 0) {
    return config.targets;
  }
  return config.targetConfig ? [config.targetConfig] : [];
}
