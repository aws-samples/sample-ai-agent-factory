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
