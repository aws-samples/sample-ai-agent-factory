import type { RuntimeConfiguration } from '../types/components';

const STANDALONE_MCP_TEMPLATE_ID = 'mcp-server-runtime';

/**
 * Fields that belong to a conversational model runtime, not a protocol-only
 * MCP tool server. Keep this list aligned with the backend admission contract.
 */
export const MODEL_ONLY_RUNTIME_FIELDS = [
  'framework',
  'model',
  'modelProvider',
  'providerApiKeyRef',
  'providerBaseUrl',
  'systemPrompt',
  'multiAgentPattern',
  'multiAgentConfig',
] as const satisfies ReadonlyArray<keyof RuntimeConfiguration>;

export function stripStandaloneMcpModelFields(
  config: RuntimeConfiguration,
): RuntimeConfiguration {
  const result = { ...config };
  for (const field of MODEL_ONLY_RUNTIME_FIELDS) {
    delete result[field];
  }
  return result;
}

/**
 * Build the exact runtime config sent to deploy/export APIs.
 *
 * The template id, not protocol alone, is the authority for model-free MCP.
 * That preserves fail-closed backend handling for a malformed generic template
 * that merely claims protocol=MCP.
 */
export function runtimeConfigForRequest(
  config: RuntimeConfiguration,
  templateId: string | null | undefined,
): RuntimeConfiguration {
  const withOperationalDefaults: RuntimeConfiguration = {
    ...config,
    entrypoint: config.entrypoint || 'agent.py',
    deploymentType: config.deploymentType || 'S3_CODE_DEPLOY',
    idleTimeout: config.idleTimeout ?? 900,
    maxLifetime: config.maxLifetime ?? 28800,
    enableOtel: config.enableOtel ?? false,
  };

  if (templateId !== STANDALONE_MCP_TEMPLATE_ID) {
    return withOperationalDefaults;
  }

  return {
    ...stripStandaloneMcpModelFields(withOperationalDefaults),
    protocol: 'MCP',
  };
}
