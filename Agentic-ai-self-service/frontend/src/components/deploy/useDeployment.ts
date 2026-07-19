/**
 * useDeployment hook.
 * Manages deploy state machine, polling, and handleDeploy logic.
 */

import { useState, useCallback } from 'react';
import { authFetch } from '../../auth/authFetch';
import { useWorkflowStore } from '../../store/workflowStore';
import { STEP_TO_NODE_TYPE, STEP_ORDER, STEP_LABELS } from './deploySteps';
import type { RuntimeConfiguration, GatewayConfiguration, IdentityConfiguration } from '../../types/components';
import type { CustomToolData, DeployConnector } from './DeployPanel';

interface DeploymentStatus {
  state: 'idle' | 'deploying' | 'deployed' | 'error';
  message?: string;
  endpoint?: string;
  runtimeId?: string;
  gatewayUrl?: string;
  simulated?: boolean;
}

interface UseDeploymentParams {
  config: RuntimeConfiguration | null;
  nodeId: string | null;
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
  resourceTagState: { tags: Record<string, string>; profileName: string | null };
  warmupRuntime: (runtimeId: string, endpoint?: string) => void;
  onVersionsRefresh: () => void;
  onTabChange: (tab: 'chat') => void;
}

export function useDeployment(params: UseDeploymentParams) {
  const {
    config,
    nodeId,
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

    try {
      // Merge config with backend-required defaults
      const fullConfig = {
        ...config,
        entrypoint: config.entrypoint || 'agent.py',
        deploymentType: config.deploymentType || 'S3_CODE_DEPLOY',
        idleTimeout: config.idleTimeout ?? 900,
        maxLifetime: config.maxLifetime ?? 28800,
        enableOtel: config.enableOtel ?? false,
      };

      const response = await authFetch('/api/deploy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          deployment_mode: deploymentMode,
          deploymentMode,
          nodeId,
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
        setDeploymentStatus({
          state: 'deployed',
          message: result.message || 'Deployed successfully!',
          endpoint: result.endpoint,
          runtimeId: result.runtimeId,
          gatewayUrl: result.gatewayUrl,
          simulated: result.simulated,
        });
        onTabChange('chat');
        if (result.runtimeId && !result.simulated) {
          warmupRuntime(result.runtimeId, result.endpoint);
        }
        return;
      }

      // Handle asynchronous response (AWS Step Functions)
      const deploymentId = result.deploymentId || result.deployment_id;
      if (!deploymentId) {
        throw new Error('No deployment ID returned from server');
      }

      setDeploymentStatus({ state: 'deploying', message: 'Deployment started. Waiting for completion... (this may take 5-10 minutes)' });

      // Poll for deployment status
      const maxPolls = 120; // 10 minutes at 5s intervals
      for (let i = 0; i < maxPolls; i++) {
        await new Promise((r) => setTimeout(r, 5000));

        try {
          const statusResp = await authFetch(`/api/deploy/${deploymentId}`);
          if (!statusResp.ok) continue;

          const statusResult = await statusResp.json();
          const status = statusResult.status;
          const currentStep = statusResult.current_step || statusResult.currentStep;

          // Update progress message with current step
          const stepMsg = currentStep ? STEP_LABELS[currentStep] || `Step: ${currentStep}` : 'Deploying...';
          setDeploymentStatus({ state: 'deploying', message: stepMsg });

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
            // Mark all nodes as completed
            for (const step of STEP_ORDER) {
              const nodeType = STEP_TO_NODE_TYPE[step];
              if (nodeType) setNodeExecutionStateByType(nodeType, 'completed');
            }
            setDeploymentStatus({
              state: 'deployed',
              message: 'Deployed successfully!',
              endpoint: rEndpoint,
              runtimeId: rId,
              gatewayUrl: statusResult.gateway_url || statusResult.gatewayUrl || undefined,
            });
            onVersionsRefresh();
            onTabChange('chat');
            warmupRuntime(rId, rEndpoint);
            return;
          }

          if (status === 'failed') {
            // Mark current step node as failed
            if (currentStep) {
              const failedNodeType = STEP_TO_NODE_TYPE[currentStep];
              if (failedNodeType) setNodeExecutionStateByType(failedNodeType, 'failed');
            }
            throw new Error(statusResult.error_details || statusResult.errorDetails || 'Deployment failed');
          }
        } catch (pollErr) {
          if (pollErr instanceof Error && pollErr.message !== 'Failed to fetch') {
            if (pollErr.message.includes('Deployment failed') || pollErr.message.includes('failed')) {
              throw pollErr;
            }
          }
          // Network errors during polling are ok, keep retrying
        }
      }

      // Polling timed out
      throw new Error('Deployment timed out after 10 minutes. Check the AWS Step Functions console.');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Deployment failed';
      setDeploymentStatus({
        state: 'error',
        message,
      });
    }
  }, [
    config, nodeId, deploymentMode, connectedTools, gatewayConfig, externalMcpServers, gatewayTools, templateId,
    identityConfig, customTools, connectors, memoryConfig, evaluationConfig, policyConfig, guardrailsConfig,
    mcpServerConfig, a2aConfig, knowledgeBaseConfig, observabilityConfig, resourceTagState, warmupRuntime,
    resetAllExecutionStates, setNodeExecutionStateByType, onVersionsRefresh, onTabChange,
  ]);

  return {
    deploymentStatus,
    setDeploymentStatus,
    handleDeploy,
  };
}
