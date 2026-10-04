/**
 * useDeployment hook.
 * Manages deploy state machine, polling, and handleDeploy logic.
 */

import { useState, useCallback } from 'react';
import { authFetch } from '../../auth/authFetch';
import { SESSION_EXPIRED_MESSAGE } from '../../services/api/client';
import { useWorkflowStore } from '../../store/workflowStore';
import { STEP_TO_NODE_TYPE, STEP_ORDER, STEP_LABELS } from './deploySteps';
import type { RuntimeConfiguration, GatewayConfiguration, IdentityConfiguration } from '../../types/components';
import type { CustomToolData, DeployConnector } from './DeployPanel';
import type { ResourceTagState } from './resourceTagState';
import { runtimeConfigForRequest } from '../../utils/runtimeRequestConfig';

interface DeploymentStatus {
  state: 'idle' | 'deploying' | 'deployed' | 'error';
  deploymentId?: string;
  message?: string;
  endpoint?: string;
  runtimeId?: string;
  runtimeProtocol?: RuntimeConfiguration['protocol'];
  gatewayUrl?: string;
  simulated?: boolean;
}

interface UseDeploymentParams {
  config: RuntimeConfiguration | null;
  nodeId: string | null;
  flowId?: string | null;
  deploymentMode: 'runtime' | 'harness';
  connectedTools: string[];
  gatewayConfig: GatewayConfiguration | null;
  externalMcpServers: unknown[] | undefined;
  gatewayTools: string[];
  templateId: string | null;
  identityConfig: IdentityConfiguration | null;
  customTools: CustomToolData[];
  connectors: DeployConnector[];
  memoryConfig: Record<string, unknown> | null;
  evaluationConfig: Record<string, unknown> | null;
  policyConfig: Record<string, unknown> | null;
  guardrailsConfig: Record<string, unknown> | null;
  mcpServerConfig: Record<string, unknown> | null;
  knowledgeBaseConfig: Record<string, unknown> | null;
  observabilityConfig: Record<string, unknown> | null;
  a2aConfig: Record<string, unknown> | null;
  resourceTagState: ResourceTagState;
  targetAccountId?: string;
  targetRegion?: string;
  warmupRuntime: (runtimeId: string, endpoint?: string) => void;
  onVersionsRefresh: () => void;
  onTabChange: (tab: 'chat' | 'tools') => void;
}

/**
 * A poll outcome that ends the deployment, as opposed to a network blip that the
 * loop should retry. The poll's catch used to decide this by grepping the error
 * message for the lowercase word "failed" (F-13): a Step Functions Cause such as
 * "ValidationException: ..." or "Failed to create runtime" did not match, so the
 * same failed row was re-read every 5 s for ten minutes and reported as a
 * timeout. The type of the throw, not the wording of the server's text, is what
 * says the poll is over.
 */
class TerminalDeploymentError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TerminalDeploymentError';
  }
}

function normalizedRuntimeProtocol(
  value: unknown,
  fallback: RuntimeConfiguration['protocol'],
): RuntimeConfiguration['protocol'] {
  const normalized = String(value || fallback || 'HTTP').toUpperCase();
  return normalized === 'MCP' || normalized === 'A2A' ? normalized : 'HTTP';
}

export function useDeployment(params: UseDeploymentParams) {
  const {
    config,
    nodeId,
    flowId,
    deploymentMode,
    connectedTools,
    gatewayConfig,
    externalMcpServers,
    gatewayTools,
    templateId,
    identityConfig,
    customTools,
    connectors,
    memoryConfig,
    evaluationConfig,
    policyConfig,
    guardrailsConfig,
    mcpServerConfig,
    knowledgeBaseConfig,
    observabilityConfig,
    a2aConfig,
    resourceTagState,
    targetAccountId,
    targetRegion,
    warmupRuntime,
    onVersionsRefresh,
    onTabChange,
  } = params;

  const [deploymentStatus, setDeploymentStatus] = useState<DeploymentStatus>({ state: 'idle' });
  const { setNodeExecutionStateByType, resetAllExecutionStates } = useWorkflowStore();

  const handleDeploy = useCallback(async () => {
    if (!config || !nodeId) return;
    setDeploymentStatus({ state: 'deploying', message: 'Starting deployment...' });
    resetAllExecutionStates();
    let deploymentId: string | undefined;

    try {
      const fullConfig = runtimeConfigForRequest(config, templateId);

      const response = await authFetch('/api/deploy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          // camelCase only. Sending `deployment_mode` as well was a hedge against the
          // backend's alias handling, and DeployRequest now rejects unknown keys so that a
          // misspelled field cannot be silently ignored. The duplicate is still tolerated
          // server-side for older clients, but every other field here is camelCase and
          // this one being both was the only reason that tolerance was needed.
          deploymentMode,
          nodeId,
          // A flow and a node are different resources. Harness/unsaved deploys
          // have no flow, and JSON.stringify omits this undefined value.
          flowId: flowId || undefined,
          config: fullConfig,
          connectedTools,
          gatewayConfig,
          gatewayTools,
          templateId,
          identityConfig: (identityConfig?.oauth2Config || identityConfig?.mode === 'per_agent') ? {
            mode: identityConfig?.mode ?? 'shared',
            provider: identityConfig?.oauth2Config?.provider,
            clientId: identityConfig?.oauth2Config?.clientId,
            clientSecretRef: identityConfig?.oauth2Config?.clientSecretRef,
            discoveryUrl: identityConfig?.oauth2Config?.discoveryUrl || '',
            scopes: identityConfig?.oauth2Config?.scopes || [],
            audience: identityConfig?.oauth2Config?.audience || undefined,
          } : undefined,
          customTools: customTools.length > 0 ? customTools : undefined,
          connectors: connectors.length > 0 ? connectors : undefined,
          externalMcpServers,
          memoryConfig: memoryConfig || undefined,
          evaluationConfig: evaluationConfig || undefined,
          policyConfig: policyConfig || undefined,
          guardrailsConfig: guardrailsConfig || undefined,
          mcpServerConfig: mcpServerConfig || undefined,
          knowledgeBaseConfig: knowledgeBaseConfig || undefined,
          observabilityConfig: observabilityConfig || undefined,
          a2aConfig: a2aConfig || undefined,
          resourceTags: Object.keys(resourceTagState.tags).length ? resourceTagState.tags : undefined,
          tagProfile: resourceTagState.profileName || undefined,
          policyRevision: resourceTagState.policyRevision || undefined,
          tagProfileUpdatedAt: resourceTagState.profileName
            ? resourceTagState.profileUpdatedAt || undefined
            : undefined,
          targetAccountId,
          targetRegion,
        }),
      });

      if (!response.ok) {
        const errorBody = await response.text();
        throw new Error(`Deployment request failed (${response.status}): ${errorBody}`);
      }

      const result = await response.json();

      // Handle synchronous response (local dev / direct deploy)
      if (result.success !== undefined) {
        if (!result.success) {
          throw new Error(result.message || 'Deployment failed');
        }
        const runtimeProtocol = normalizedRuntimeProtocol(
          result.runtimeProtocol || result.runtime_protocol,
          config.protocol,
        );
        setDeploymentStatus({
          state: 'deployed',
          deploymentId: result.deploymentId || result.deployment_id,
          message: result.message || 'Deployed successfully!',
          endpoint: result.endpoint,
          runtimeId: result.runtimeId,
          runtimeProtocol,
          gatewayUrl: result.gatewayUrl,
          simulated: result.simulated,
        });
        onTabChange(runtimeProtocol === 'MCP' ? 'tools' : 'chat');
        // MCP runtimes expose tools through JSON-RPC and cannot accept the
        // prompt-shaped HTTP-agent warmup. HTTP/A2A keep their existing path.
        if (runtimeProtocol !== 'MCP' && result.runtimeId && !result.simulated) {
          warmupRuntime(result.runtimeId, result.endpoint);
        }
        return;
      }

      // Handle asynchronous response (AWS Step Functions)
      deploymentId = result.deploymentId || result.deployment_id;
      if (!deploymentId) {
        throw new Error('No deployment ID returned from server');
      }

      setDeploymentStatus({
        state: 'deploying',
        deploymentId,
        message: 'Deployment started. Waiting for completion... (this may take 5-10 minutes)',
      });

      // Poll for deployment status
      const maxPolls = 120; // 10 minutes at 5s intervals
      for (let i = 0; i < maxPolls; i++) {
        await new Promise((r) => setTimeout(r, 5000));

        try {
          const statusResp = await authFetch(`/api/deploy/${deploymentId}`);
          if (!statusResp.ok) {
            // An expired session or a revoked scope will answer the same way on
            // every remaining poll; ten minutes of silence would follow.
            if (statusResp.status === 401) {
              throw new TerminalDeploymentError(SESSION_EXPIRED_MESSAGE);
            }
            if (statusResp.status === 403) {
              throw new TerminalDeploymentError(
                'You no longer have permission to read this deployment. Check the AWS Step Functions console.',
              );
            }
            continue;
          }

          const statusResult = await statusResp.json();
          const status = statusResult.status;
          const currentStep = statusResult.current_step || statusResult.currentStep;

          // Update progress message with current step
          const stepMsg = currentStep ? STEP_LABELS[currentStep] || `Step: ${currentStep}` : 'Deploying...';
          setDeploymentStatus({
            state: 'deploying',
            deploymentId,
            message: stepMsg,
          });

          // Update canvas node execution states based on current step
          if (currentStep) {
            const currentIdx = STEP_ORDER.indexOf(currentStep);
            if (currentIdx >= 0) {
              const currentNodeType = STEP_TO_NODE_TYPE[currentStep];
              // Mark prior steps' node types as completed
              const completedTypes = new Set<string>();
              for (let s = 0; s < currentIdx; s++) {
                const nodeType = STEP_TO_NODE_TYPE[STEP_ORDER[s]];
                if (nodeType && nodeType !== currentNodeType && !completedTypes.has(nodeType)) {
                  completedTypes.add(nodeType);
                  setNodeExecutionStateByType(nodeType, 'completed');
                }
              }
              // Mark current step's node as running
              if (currentNodeType) {
                setNodeExecutionStateByType(currentNodeType, 'running');
              }
            }
          }

          if (status === 'succeeded') {
            const rId = statusResult.runtime_id || statusResult.runtimeId || deploymentId;
            const rEndpoint = statusResult.runtime_endpoint || statusResult.runtimeEndpoint || '';
            const runtimeProtocol = normalizedRuntimeProtocol(
              statusResult.runtime_protocol || statusResult.runtimeProtocol,
              config.protocol,
            );
            // Mark all nodes as completed
            for (const step of STEP_ORDER) {
              const nodeType = STEP_TO_NODE_TYPE[step];
              if (nodeType) setNodeExecutionStateByType(nodeType, 'completed');
            }
            setDeploymentStatus({
              state: 'deployed',
              deploymentId,
              message: 'Deployed successfully!',
              endpoint: rEndpoint,
              runtimeId: rId,
              runtimeProtocol,
              gatewayUrl: statusResult.gateway_url || statusResult.gatewayUrl || undefined,
            });
            onVersionsRefresh();
            onTabChange(runtimeProtocol === 'MCP' ? 'tools' : 'chat');
            if (runtimeProtocol !== 'MCP') {
              warmupRuntime(rId, rEndpoint);
            }
            return;
          }

          if (status === 'failed') {
            // Mark current step node as failed
            if (currentStep) {
              const failedNodeType = STEP_TO_NODE_TYPE[currentStep];
              if (failedNodeType) setNodeExecutionStateByType(failedNodeType, 'failed');
            }
            // The server's error_details is the user's only account of what
            // went wrong. Surface it as-is; do not pattern-match its wording.
            throw new TerminalDeploymentError(
              statusResult.error_details || statusResult.errorDetails || 'Deployment failed',
            );
          }
        } catch (pollErr) {
          if (pollErr instanceof TerminalDeploymentError) {
            throw pollErr;
          }
          // Network errors and malformed poll bodies are transient: keep retrying
          // within the 120-poll budget.
        }
      }

      // Polling timed out
      throw new Error('Deployment timed out after 10 minutes. Check the AWS Step Functions console.');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Deployment failed';
      setDeploymentStatus({
        state: 'error',
        deploymentId,
        message,
      });
    }
  }, [
    config, nodeId, flowId, deploymentMode, connectedTools, gatewayConfig, externalMcpServers, gatewayTools, templateId,
    identityConfig, customTools, connectors, memoryConfig, evaluationConfig, policyConfig, guardrailsConfig,
    mcpServerConfig, a2aConfig, knowledgeBaseConfig, observabilityConfig, resourceTagState, warmupRuntime,
    targetAccountId, targetRegion, resetAllExecutionStates, setNodeExecutionStateByType,
    onVersionsRefresh, onTabChange,
  ]);

  return {
    deploymentStatus,
    setDeploymentStatus,
    handleDeploy,
  };
}
