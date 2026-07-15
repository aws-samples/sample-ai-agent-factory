/**
 * Utility functions for Observability configuration.
 * Extracted to support React Fast Refresh requirements.
 */

import type { ObservabilityConfiguration } from '../../types/components';

/**
 * Create default observability configuration.
 */
export function createDefaultObservabilityConfig(): ObservabilityConfiguration {
  return {
    name: 'Observability',
    enableOtel: true,
    provider: 'langfuse',
    otlpEndpoint: 'https://cloud.langfuse.com/api/public/otel',
    otlpProtocol: 'http/protobuf',
    serviceName: undefined,
    sampleRate: 1.0,
    resourceAttributes: {},
    extraHeaders: {},
  };
}
