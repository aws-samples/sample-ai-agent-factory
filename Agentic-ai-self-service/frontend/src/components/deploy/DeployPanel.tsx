/**
 * DeployPanel component for deploying and testing AgentCore Runtime.
 */

import { useState, useCallback, useMemo, useRef, useEffect } from 'react';
import type { RuntimeConfiguration, GatewayConfiguration, IdentityConfiguration } from '../../types/components';
import { authFetch } from '../../auth/authFetch';
import { WORKFLOW_TEMPLATES } from '../../data/templates';
import { useWorkflowStore } from '../../store/workflowStore';
import { publishToRegistryApi } from '../../services/api';
import type { AgentCoreComponentType } from '../../types/workflow';
import { VersionsList } from './VersionsList';
import { EvaluationResultsPanel } from './EvaluationResultsPanel';
import { CostPanel } from './CostPanel';
import { ObservabilityPanel } from './ObservabilityPanel';
import { TriggersPanel } from './TriggersPanel';

interface DeploymentStatus {
  state: 'idle' | 'deploying' | 'deployed' | 'error';
  message?: string;
  endpoint?: string;
  runtimeId?: string;
  gatewayUrl?: string;
  simulated?: boolean;
}

interface TestResult {
  success: boolean;
  response?: string;
  error?: string;
  latencyMs?: number;
  sessionId?: string;
  requestId?: string;
  arn?: string;
  logs?: string;
}

export interface CustomToolData {
  toolName: string;
  displayName: string;
  description: string;
  lambdaCode: string;
  inputSchema: Record<string, unknown>;
}

// Phase A — SaaS connector deploy entry. snake_case keys match the backend
// DeployRequest connectors[] schema. secret_value
// is transient (minted into Secrets Manager backend-side, never persisted).
export interface DeployConnector {
  connector_id: string;
  auth_method: 'api_key' | 'oauth2_cc';
  secret_value?: string;
  secret_arn?: string;
  spec_url?: string;
  spec_inline?: string;
  scopes?: string[];
  client_id?: string;
  oauth_vendor?: string;
  discovery_url?: string;
  credential_location?: 'HEADER' | 'QUERY_PARAMETER';
  credential_parameter_name?: string;
  credential_prefix?: string;
}

export interface DeployPanelProps {
  config: RuntimeConfiguration | null;
  nodeId: string | null;
  connectedTools?: string[];
  gatewayConfig?: GatewayConfiguration | null;
  gatewayTools?: string[];
  templateId?: string | null;
  identityConfig?: IdentityConfiguration | null;
  customTools?: CustomToolData[];
  connectors?: DeployConnector[];
  memoryConfig?: Record<string, unknown> | null;
  evaluationConfig?: Record<string, unknown> | null;
  policyConfig?: Record<string, unknown> | null;
  guardrailsConfig?: Record<string, unknown> | null;
  mcpServerConfig?: Record<string, unknown> | null;
  knowledgeBaseConfig?: Record<string, unknown> | null;
  observabilityConfig?: Record<string, unknown> | null;
  a2aConfig?: Record<string, unknown> | null;
  // Phase B — authoring/deploy path. "runtime" (default/omitted) keeps the
  // unchanged visual-canvas Runtime path; "harness" routes the SAME deploy
  // payload through the additive AgentCore Harness path (deployment_mode is
  // forwarded so it reaches both the SFN and direct-deploy branches, Bug 9).
  deploymentMode?: 'runtime' | 'harness';
  isVisible: boolean;
  onClose: () => void;
  restoredDeployment?: {
    runtimeId: string;
    endpoint: string;
    gatewayUrl?: string;
  } | null;
}

// Map SFN step names to the canvas node type they correspond to
const STEP_TO_NODE_TYPE: Record<string, AgentCoreComponentType | null> = {
  validate: 'runtime',
  mcp_server: 'runtime',
  codegen: 'runtime',
  iam: 'runtime',
  runtime_configure: 'runtime',
  runtime_launch: 'runtime',
  gateway: 'gateway',
  knowledge_base: 'tool',
  memory: 'memory',
  policy: 'policy',
  guardrails: 'guardrails',
  evaluation: 'observability',
  auth: 'identity',
  status_update: null,
};

// Ordered list of all SFN steps for tracking completed steps
const STEP_ORDER = [
  'validate', 'guardrails', 'mcp_server', 'knowledge_base', 'gateway', 'memory', 'policy',
  'codegen', 'iam', 'runtime_configure', 'runtime_launch',
  'evaluation', 'auth', 'status_update',
];

export function DeployPanel({ config, nodeId, connectedTools = [], gatewayConfig, gatewayTools = [], templateId, identityConfig, customTools = [], connectors = [], memoryConfig, evaluationConfig, policyConfig, guardrailsConfig, mcpServerConfig, knowledgeBaseConfig, observabilityConfig, a2aConfig, deploymentMode = 'runtime', isVisible, onClose, restoredDeployment }: DeployPanelProps) {
  // ============================================================
  // State Hooks
  // (Audit #14: deploy state, chat state, refs, memoized template)
  // ============================================================
  const [deploymentStatus, setDeploymentStatus] = useState<DeploymentStatus>({ state: 'idle' });
  const { setNodeExecutionStateByType, resetAllExecutionStates } = useWorkflowStore();
  const [testInput, setTestInput] = useState('');
  const [, setTestResult] = useState<TestResult | null>(null);
  const [isTesting, setIsTesting] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [activeTab, setActiveTab] = useState<'deploy' | 'chat' | 'versions' | 'evals' | 'cost' | 'observability' | 'triggers'>('deploy');
  // Phase 1 Gap 1A — increment to force VersionsList reload after a new deploy.
  const [versionsRefreshKey, setVersionsRefreshKey] = useState(0);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [conversationHistory, setConversationHistory] = useState<Array<{role: string, content: string}>>([]);
  const [chatMessages, setChatMessages] = useState<Array<{
    id: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    timestamp: Date;
    latencyMs?: number;
  }>>([]);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const chatInputRef = useRef<HTMLTextAreaElement>(null);

  // Look up template info for tool display
  const activeTemplate = useMemo(() => {
    if (!templateId) return null;
    return WORKFLOW_TEMPLATES.find((t) => t.id === templateId) || null;
  }, [templateId]);

  // ============================================================
  // useEffect Chain
  // (Audit #14: scroll-to-bottom, focus chat, hydrate from restored deployment)
  // ============================================================

  // Auto-scroll chat to bottom when messages change
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [chatMessages, isTesting]);

  // Auto-focus chat input when switching to chat tab
  useEffect(() => {
    if (activeTab === 'chat' && deploymentStatus.state === 'deployed') {
      setTimeout(() => chatInputRef.current?.focus(), 100);
    }
  }, [activeTab, deploymentStatus.state]);

  // Fire-and-forget warmup to trigger cold start in background after deploy
  const warmupRuntime = useCallback((runtimeId: string, endpoint?: string) => {
    authFetch('/api/test-runtime', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        endpoint: endpoint || '',
        input: 'ping',
        runtimeId,
      }),
    }).catch(() => {}); // Ignore errors — just a warmup
  }, []);

  // Hydrate panel from a restored deployment (user clicked "Restore" on the banner)
  useEffect(() => {
    if (restoredDeployment && deploymentStatus.state === 'idle') {
      setDeploymentStatus({
        state: 'deployed',
        message: 'Restored from previous deployment',
        runtimeId: restoredDeployment.runtimeId,
        endpoint: restoredDeployment.endpoint,
        gatewayUrl: restoredDeployment.gatewayUrl,
      });
      setActiveTab('chat');
    }
  }, [restoredDeployment]); // eslint-disable-line react-hooks/exhaustive-deps

  // ============================================================
  // Deploy Submission
  // (Audit #14: handleDeploy — POST /api/deploy, poll Step Functions for status)
  // ============================================================

  const handleDeploy = useCallback(async () => {
    if (!config || !nodeId) return;
    setDeploymentStatus({ state: 'deploying', message: 'Starting deployment...' });
    resetAllExecutionStates();

    try {
      // Merge config with backend-required defaults the frontend may not set
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
          // Phase B — forward the authoring/deploy path. "runtime" is the
          // default; "harness" routes the same payload through the additive
          // AgentCore Harness path. Sent on BOTH camelCase + snake_case so it
          // reaches the SFN and direct-deploy branches regardless of which the
          // backend reads (Bug 9 parity).
          deployment_mode: deploymentMode,
          deploymentMode,
          nodeId, config: fullConfig, connectedTools, gatewayConfig, gatewayTools, templateId,
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
          memoryConfig: memoryConfig || undefined,
          evaluationConfig: evaluationConfig || undefined,
          policyConfig: policyConfig || undefined,
          guardrailsConfig: guardrailsConfig || undefined,
          mcpServerConfig: mcpServerConfig || undefined,
          knowledgeBaseConfig: knowledgeBaseConfig || undefined,
          observabilityConfig: observabilityConfig || undefined,
          a2aConfig: a2aConfig || undefined,
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
        setActiveTab('chat');
        if (result.runtimeId && !result.simulated) {
          warmupRuntime(result.runtimeId, result.endpoint);
        }
        return;
      }

      // Handle asynchronous response (AWS Step Functions: 202 with deploymentId)
      const deploymentId = result.deploymentId || result.deployment_id;
      if (!deploymentId) {
        throw new Error('No deployment ID returned from server');
      }

      setDeploymentStatus({ state: 'deploying', message: 'Deployment started. Waiting for completion... (this may take 5-10 minutes)' });

      // Poll for deployment status
      const maxPolls = 120; // 10 minutes at 5s intervals
      for (let i = 0; i < maxPolls; i++) {
        await new Promise((r) => setTimeout(r, 5000)); // Wait 5 seconds

        try {
          const statusResp = await authFetch(`/api/deploy/${deploymentId}`);
          if (!statusResp.ok) continue;

          const statusResult = await statusResp.json();
          const status = statusResult.status;
          const currentStep = statusResult.current_step || statusResult.currentStep;

          // Update progress message with current step
          const stepLabels: Record<string, string> = {
            validate: 'Validating workflow...',
            mcp_server: 'Deploying MCP Server Runtime...',
            codegen: 'Generating agent code...',
            iam: 'Creating IAM roles...',
            gateway: 'Deploying MCP Gateway...',
            knowledge_base: 'Setting up Knowledge Base...',
            memory: 'Creating memory resource...',
            policy: 'Creating policy engine...',
            runtime_configure: 'Configuring runtime...',
            runtime_launch: 'Launching runtime... (this takes a few minutes)',
            evaluation: 'Setting up online evaluation...',
            auth: 'Configuring JWT auth...',
            status_update: 'Finalizing deployment...',
          };
          const stepMsg = currentStep ? stepLabels[currentStep] || `Step: ${currentStep}` : 'Deploying...';
          setDeploymentStatus({ state: 'deploying', message: stepMsg });

          // Update canvas node execution states based on current step
          if (currentStep) {
            const currentIdx = STEP_ORDER.indexOf(currentStep);
            if (currentIdx >= 0) {
              const currentNodeType = STEP_TO_NODE_TYPE[currentStep];
              // Mark prior steps' node types as completed (skip the current node type — it's running)
              const completedTypes = new Set<string>();
              for (let s = 0; s < currentIdx; s++) {
                const nodeType = STEP_TO_NODE_TYPE[STEP_ORDER[s]];
                if (nodeType && nodeType !== currentNodeType && !completedTypes.has(nodeType)) {
                  completedTypes.add(nodeType);
                  setNodeExecutionStateByType(nodeType, 'completed');
                }
              }
              // Always mark current step's node as running
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
            // Phase 1 Gap 1A — bump the refresh key so VersionsList shows
            // the new version row the moment the user clicks the tab.
            setVersionsRefreshKey((k) => k + 1);
            setActiveTab('chat');
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
          // If it's a thrown error (not a network issue), rethrow
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
  }, [config, nodeId, deploymentMode, connectedTools, gatewayConfig, gatewayTools, templateId, identityConfig, customTools, connectors, memoryConfig, evaluationConfig, policyConfig, guardrailsConfig, mcpServerConfig, warmupRuntime, resetAllExecutionStates, setNodeExecutionStateByType]);

  // ============================================================
  // CFN Download UI
  // (Audit #14: handleDownloadCfn — POST /api/generate-cfn-template, trigger
  // browser download of the generated CloudFormation .zip)
  // ============================================================

  const [isDownloadingCfn, setIsDownloadingCfn] = useState(false);
  const [isExportingPython, setIsExportingPython] = useState(false);
  // Registry publish (Gap 2A — connects deploy -> registry so a deployed agent
  // becomes a reusable blueprint others can Browse + Clone-to-canvas).
  const [isPublishing, setIsPublishing] = useState(false);
  const [publishMsg, setPublishMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  // Phase 3 Gap 3G — eject a standalone Python project. Near-copy of
  // handleDownloadCfn but targets /api/export-python.
  const handleExportPython = useCallback(async () => {
    if (!config || !nodeId) return;
    setIsExportingPython(true);

    try {
      const fullConfig = {
        ...config,
        entrypoint: config.entrypoint || 'agent.py',
        deploymentType: config.deploymentType || 'S3_CODE_DEPLOY',
        idleTimeout: config.idleTimeout ?? 900,
        maxLifetime: config.maxLifetime ?? 28800,
        enableOtel: config.enableOtel ?? false,
      };

      const response = await authFetch('/api/export-python', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          nodeId, config: fullConfig, connectedTools, gatewayConfig, gatewayTools, templateId,
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
          memoryConfig: memoryConfig || undefined,
          evaluationConfig: evaluationConfig || undefined,
          policyConfig: policyConfig || undefined,
          guardrailsConfig: guardrailsConfig || undefined,
          mcpServerConfig: mcpServerConfig || undefined,
          knowledgeBaseConfig: knowledgeBaseConfig || undefined,
          observabilityConfig: observabilityConfig || undefined,
          a2aConfig: a2aConfig || undefined,
        }),
      });

      if (!response.ok) {
        throw new Error(`Python export failed (${response.status})`);
      }

      const result = await response.json();

      if (result.download_url) {
        const a = document.createElement('a');
        a.href = result.download_url;
        a.download = result.filename || 'agent-python.zip';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      } else if (result.zip_base64) {
        const bytes = Uint8Array.from(atob(result.zip_base64), c => c.charCodeAt(0));
        const blob = new Blob([bytes], { type: 'application/zip' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = result.filename || 'agent-python.zip';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Python export failed';
      setDeploymentStatus({ state: 'error', message });
    } finally {
      setIsExportingPython(false);
    }
  }, [config, nodeId, connectedTools, gatewayConfig, gatewayTools, templateId, customTools, connectors, memoryConfig, evaluationConfig, policyConfig, guardrailsConfig, mcpServerConfig, knowledgeBaseConfig, observabilityConfig, a2aConfig, identityConfig]);

  const handleDownloadCfn = useCallback(async () => {
    if (!config || !nodeId) return;
    setIsDownloadingCfn(true);

    try {
      const fullConfig = {
        ...config,
        entrypoint: config.entrypoint || 'agent.py',
        deploymentType: config.deploymentType || 'S3_CODE_DEPLOY',
        idleTimeout: config.idleTimeout ?? 900,
        maxLifetime: config.maxLifetime ?? 28800,
        enableOtel: config.enableOtel ?? false,
      };

      const response = await authFetch('/api/generate-cfn-template', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          nodeId, config: fullConfig, connectedTools, gatewayConfig, gatewayTools, templateId,
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
          memoryConfig: memoryConfig || undefined,
          evaluationConfig: evaluationConfig || undefined,
          policyConfig: policyConfig || undefined,
          guardrailsConfig: guardrailsConfig || undefined,
          mcpServerConfig: mcpServerConfig || undefined,
          knowledgeBaseConfig: knowledgeBaseConfig || undefined,
          observabilityConfig: observabilityConfig || undefined,
          a2aConfig: a2aConfig || undefined,
        }),
      });

      if (!response.ok) {
        throw new Error(`Template generation failed (${response.status})`);
      }

      const result = await response.json();

      if (result.download_url) {
        // Presigned S3 URL — trigger download
        const a = document.createElement('a');
        a.href = result.download_url;
        a.download = result.filename || 'agentcore-cfn.zip';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      } else if (result.zip_base64) {
        // Base64 fallback
        const bytes = Uint8Array.from(atob(result.zip_base64), c => c.charCodeAt(0));
        const blob = new Blob([bytes], { type: 'application/zip' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = result.filename || 'agentcore-cfn.zip';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Template generation failed';
      setDeploymentStatus({ state: 'error', message });
    } finally {
      setIsDownloadingCfn(false);
    }
  }, [config, nodeId, connectedTools, gatewayConfig, gatewayTools, templateId, customTools, connectors, memoryConfig, evaluationConfig, policyConfig, guardrailsConfig, mcpServerConfig, knowledgeBaseConfig, identityConfig]);

  // Gap 2A — publish the deployed agent's canvas as a reusable registry blueprint.
  // This closes the deploy -> registry -> Browse -> Clone-to-canvas loop: the
  // exact nodes/edges on the canvas become the snapshot that RegistryModal's
  // "Clone to Canvas" later rehydrates for another user.
  const handlePublishToRegistry = useCallback(async () => {
    if (!config) return;
    setIsPublishing(true);
    setPublishMsg(null);
    try {
      const { nodes, edges } = useWorkflowStore.getState();
      const display = config.name || 'Untitled Agent';
      await publishToRegistryApi({
        display_name: display,
        description: config.systemPrompt?.slice(0, 280) || `Deployed agent ${display}`,
        visibility: 'org',
        canvas_snapshot: { name: display, nodes, edges },
        source_runtime_name: config.name || undefined,
      });
      setPublishMsg({ kind: 'ok', text: `Published "${display}" to the registry.` });
    } catch (error) {
      const text = error instanceof Error ? error.message : 'Publish failed';
      setPublishMsg({ kind: 'err', text });
    } finally {
      setIsPublishing(false);
    }
  }, [config]);

  // ============================================================
  // Streaming Chat
  // (Audit #14: handleTest — invoke runtime via /api/test-runtime, consume SSE
  // stream into chatMessages; handleNewSession; handleKeyDown; handleDelete)
  // ============================================================

  const handleTest = useCallback(async () => {
    if (!deploymentStatus.endpoint && !deploymentStatus.runtimeId) return;
    setIsTesting(true);
    setTestResult(null);
    const startTime = Date.now();
    const MAX_RETRIES = 5;
    const streamingMsgId = `assistant-streaming-${Date.now()}`;

    // Add user message to chat
    setChatMessages(prev => [...prev, {
      id: `user-${Date.now()}`,
      role: 'user',
      content: testInput,
      timestamp: new Date(),
    }]);

    const requestBody = {
      endpoint: deploymentStatus.endpoint,
      input: testInput,
      simulated: deploymentStatus.simulated,
      runtimeId: deploymentStatus.runtimeId,
      sessionId: sessionId,
      history: conversationHistory,
    };

    // Try streaming first
    const tryStreaming = async (): Promise<boolean> => {
      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 120000);
        const response = await fetch('/api/test-runtime-stream', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(requestBody),
          signal: controller.signal,
        });
        clearTimeout(timeoutId);

        if (!response.ok || !response.body) return false;
        const contentType = response.headers.get('content-type') || '';
        if (!contentType.includes('text/event-stream')) return false;

        // Add empty streaming assistant message
        setChatMessages(prev => {
          const filtered = prev.filter(m => m.id !== 'warming-up');
          return [...filtered, {
            id: streamingMsgId,
            role: 'assistant' as const,
            content: '',
            timestamp: new Date(),
          }];
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let fullText = '';
        let receivedSessionId: string | null = null;
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const lines = buffer.split('\n');
          buffer = lines.pop() || ''; // Keep incomplete line in buffer

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const evt = JSON.parse(line.slice(6));
              if (evt.type === 'token' && evt.token) {
                fullText += evt.token;
                const captured = fullText;
                setChatMessages(prev => prev.map(m =>
                  m.id === streamingMsgId ? { ...m, content: captured } : m
                ));
              } else if (evt.type === 'done') {
                receivedSessionId = evt.session_id || null;
                if (evt.full_response) fullText = evt.full_response;
              } else if (evt.type === 'error') {
                throw new Error(evt.error || 'Stream error');
              }
            } catch (parseErr) {
              if (parseErr instanceof Error && parseErr.message !== 'Stream error') continue;
              throw parseErr;
            }
          }
        }

        if (!fullText) return false;

        // Finalize the streaming message
        const latency = Date.now() - startTime;
        setChatMessages(prev => prev.map(m =>
          m.id === streamingMsgId ? { ...m, content: fullText, latencyMs: latency } : m
        ));

        if (receivedSessionId) setSessionId(receivedSessionId);
        setConversationHistory(prev => [
          ...prev,
          { role: 'user', content: testInput },
          { role: 'assistant', content: fullText },
        ]);
        setTestResult({ success: true, response: fullText, latencyMs: latency, sessionId: receivedSessionId || undefined });
        setTestInput('');
        return true;
      } catch {
        // Clean up streaming message on failure
        setChatMessages(prev => prev.filter(m => m.id !== streamingMsgId));
        return false;
      }
    };

    try {
      // Attempt streaming — fall back to sync on failure
      if (await tryStreaming()) return;

      // Fallback: synchronous endpoint with retry logic
      for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
        try {
          if (attempt > 1) {
            setChatMessages(prev => {
              const filtered = prev.filter(m => m.id !== 'warming-up');
              return [...filtered, {
                id: 'warming-up',
                role: 'system' as const,
                content: `Runtime warming up... Retry ${attempt}/${MAX_RETRIES} (cold start is normal)`,
                timestamp: new Date(),
              }];
            });
            await new Promise(r => setTimeout(r, 5000 + (attempt - 2) * 5000));
          }

          const controller = new AbortController();
          const timeoutId = setTimeout(() => controller.abort(), 120000);

          const response = await authFetch('/api/test-runtime', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody),
            signal: controller.signal,
          });

          clearTimeout(timeoutId);

          const responseText = await response.text();
          let result;
          try {
            result = JSON.parse(responseText);
          } catch {
            if (attempt < MAX_RETRIES) continue;
            const nonJsonErr = `Runtime did not respond after ${MAX_RETRIES} attempts. The S3 code-deploy cold start may be too slow. Try again in a minute.`;
            setTestResult({ success: false, error: nonJsonErr, latencyMs: Date.now() - startTime });
            setChatMessages(prev => [...prev.filter(m => m.id !== 'warming-up'), { id: `error-${Date.now()}`, role: 'system' as const, content: nonJsonErr, timestamp: new Date() }]);
            return;
          }

          if (result.message === 'Service Unavailable' || response.status === 503 || response.status === 504) {
            if (attempt < MAX_RETRIES) continue;
            const gwErr = `API Gateway timed out (29s limit). The runtime cold start takes longer. Try again — the runtime may have warmed up.`;
            setTestResult({ success: false, error: gwErr, latencyMs: Date.now() - startTime });
            setChatMessages(prev => [...prev.filter(m => m.id !== 'warming-up'), { id: `error-${Date.now()}`, role: 'system' as const, content: gwErr, timestamp: new Date() }]);
            return;
          }

          if (result.success === undefined && !result.error && !result.response) {
            if (attempt < MAX_RETRIES) continue;
            const unexpErr = `Unexpected response: ${responseText.slice(0, 200)}`;
            setTestResult({ success: false, error: unexpErr, latencyMs: Date.now() - startTime });
            setChatMessages(prev => [...prev.filter(m => m.id !== 'warming-up'), { id: `error-${Date.now()}`, role: 'system' as const, content: unexpErr, timestamp: new Date() }]);
            return;
          }

          const isColdStartError = result.error && (
            result.error.includes('initialization time exceeded') ||
            result.error.includes('Runtime initialization') ||
            result.error.includes('cold start') ||
            result.error.includes('Read timeout') ||
            result.error.includes('read timeout') ||
            result.error.includes('timed out') ||
            result.error.includes('RuntimeClientError') ||
            result.error.includes('error (500) from runtime')
          );
          if (!result.success && isColdStartError && attempt < MAX_RETRIES) {
            continue;
          }

          if (result.sessionId) {
            setSessionId(result.sessionId);
          }

          if (result.success && result.response) {
            setConversationHistory(prev => [
              ...prev,
              { role: 'user', content: testInput },
              { role: 'assistant', content: result.response }
            ]);
            setChatMessages(prev => {
              const filtered = prev.filter(m => m.id !== 'warming-up');
              return [...filtered, {
                id: `assistant-${Date.now()}`,
                role: 'assistant' as const,
                content: result.response,
                timestamp: new Date(),
                latencyMs: Date.now() - startTime,
              }];
            });
            setTestInput('');
          }

          setTestResult({
            success: result.success,
            response: result.response,
            error: result.error,
            latencyMs: Date.now() - startTime,
            sessionId: result.sessionId,
            requestId: result.requestId,
            arn: result.arn,
            logs: result.logs,
          });
          if (!result.success && result.error) {
            setChatMessages(prev => [...prev.filter(m => m.id !== 'warming-up'), { id: `error-${Date.now()}`, role: 'system' as const, content: result.error, timestamp: new Date() }]);
          }
          return;
        } catch (error) {
          const msg = error instanceof Error ? error.message : 'Test failed';
          if (msg.includes('aborted') && attempt < MAX_RETRIES) continue;
          const catchErr = msg.includes('aborted')
            ? `Request timed out after ${MAX_RETRIES} attempts. The runtime cold start may need more time.`
            : msg;
          setTestResult({ success: false, error: catchErr, latencyMs: Date.now() - startTime });
          setChatMessages(prev => [...prev.filter(m => m.id !== 'warming-up'), { id: `error-${Date.now()}`, role: 'system' as const, content: catchErr, timestamp: new Date() }]);
          return;
        }
      }

      const exhaustErr = `Runtime did not respond after ${MAX_RETRIES} attempts. Cold start initialization is taking too long. Try again in a minute — the runtime may have warmed up.`;
      setTestResult({ success: false, error: exhaustErr, latencyMs: Date.now() - startTime });
      setChatMessages(prev => [...prev.filter(m => m.id !== 'warming-up'), { id: `error-${Date.now()}`, role: 'system' as const, content: exhaustErr, timestamp: new Date() }]);
    } finally {
      setIsTesting(false);
    }
  }, [deploymentStatus.endpoint, deploymentStatus.simulated, deploymentStatus.runtimeId, testInput, sessionId, conversationHistory]);

  const handleNewSession = useCallback(() => {
    setSessionId(null);
    setTestResult(null);
    setConversationHistory([]);
    setChatMessages([]);
  }, []);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (testInput.trim() && !isTesting) handleTest();
    }
  }, [testInput, isTesting, handleTest]);

  const handleDelete = useCallback(async () => {
    if (!deploymentStatus.runtimeId) return;
    if (!confirm('Are you sure you want to delete this runtime from AWS?')) return;

    setIsDeleting(true);
    try {
      const response = await authFetch(`/api/runtime/${deploymentStatus.runtimeId}`, { method: 'DELETE' });
      const result = await response.json();
      if (result.success) {
        setDeploymentStatus({ state: 'idle' });
        setTestResult(null);
        setActiveTab('deploy');
      }
    } catch (error) {
      console.error('Delete failed:', error);
    } finally {
      setIsDeleting(false);
    }
  }, [deploymentStatus.runtimeId]);

  if (!isVisible) return null;

  // ============================================================
  // Render
  // (Audit #14: header, deploy/chat tabs, deploy form, status/error banners,
  // chat messages list, input box, footer with delete + close)
  // ============================================================

  return (
    <>
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black/20 z-40" onClick={onClose} />

      {/* Panel */}
      <div className="fixed right-0 top-0 bottom-0 w-[420px] bg-white shadow-2xl z-50 flex flex-col overflow-hidden border-l border-[#e9ebed]">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#e9ebed] bg-[#232f3e]">
          <div className="flex items-center gap-3">
            <div className="w-7 h-7 rounded-md bg-[#ff9900] flex items-center justify-center">
              <svg className="w-4 h-4 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7z" />
              </svg>
            </div>
            <div>
              <h3 className="font-semibold text-white text-sm">Deploy & Test</h3>
              <p className="text-[11px] text-white/50">{deploymentMode === 'harness' ? 'AgentCore Harness' : 'AgentCore Runtime'}</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-md hover:bg-white/10 transition-colors"
          >
            <svg className="w-4 h-4 text-white/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-[#e9ebed]">
          <button
            onClick={() => setActiveTab('deploy')}
            className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${
              activeTab === 'deploy'
                ? 'text-[#0972d3]'
                : 'text-[#5f6b7a] hover:text-[#16191f]'
            }`}
          >
            Deploy
            {activeTab === 'deploy' && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />
            )}
          </button>
          <button
            onClick={() => setActiveTab('chat')}
            disabled={deploymentStatus.state !== 'deployed'}
            className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${
              activeTab === 'chat'
                ? 'text-[#0972d3]'
                : deploymentStatus.state === 'deployed'
                  ? 'text-[#5f6b7a] hover:text-[#16191f]'
                  : 'text-[#d1d5db] cursor-not-allowed'
            }`}
          >
            Chat
            {deploymentStatus.state === 'deployed' && (
              <span className="ml-1.5 w-1.5 h-1.5 bg-emerald-500 rounded-full inline-block" />
            )}
            {activeTab === 'chat' && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />
            )}
          </button>
          {/* Phase 1 Gap 1A — Versions tab. Always available so the user can
              inspect history even before deploying for the first time. */}
          <button
            onClick={() => setActiveTab('versions')}
            className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${
              activeTab === 'versions'
                ? 'text-[#0972d3]'
                : 'text-[#5f6b7a] hover:text-[#16191f]'
            }`}
          >
            Versions
            {activeTab === 'versions' && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />
            )}
          </button>
          {/* Phase 1 Gap 1C — Eval results tab. */}
          <button
            onClick={() => setActiveTab('evals')}
            className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${
              activeTab === 'evals'
                ? 'text-[#0972d3]'
                : 'text-[#5f6b7a] hover:text-[#16191f]'
            }`}
          >
            Eval
            {activeTab === 'evals' && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />
            )}
          </button>
          {/* Phase 2 Gap 2B — Cost tab. */}
          <button
            onClick={() => setActiveTab('cost')}
            className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${
              activeTab === 'cost'
                ? 'text-[#0972d3]'
                : 'text-[#5f6b7a] hover:text-[#16191f]'
            }`}
          >
            Cost
            {activeTab === 'cost' && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />
            )}
          </button>
          {/* Phase 1 Gap 1D — Observability tab. */}
          <button
            onClick={() => setActiveTab('observability')}
            className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${
              activeTab === 'observability'
                ? 'text-[#0972d3]'
                : 'text-[#5f6b7a] hover:text-[#16191f]'
            }`}
          >
            Observe
            {activeTab === 'observability' && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />
            )}
          </button>
          {/* Phase 3 Gap 3F — Triggers tab. */}
          <button
            onClick={() => setActiveTab('triggers')}
            className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${
              activeTab === 'triggers'
                ? 'text-[#0972d3]'
                : 'text-[#5f6b7a] hover:text-[#16191f]'
            }`}
          >
            Triggers
            {activeTab === 'triggers' && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />
            )}
          </button>
        </div>

        {/* Content */}
        <div className={`flex-1 min-h-0 ${activeTab === 'chat' ? 'flex flex-col' : 'overflow-y-auto'}`}>
          {activeTab === 'deploy' && (
            <div className="p-5 space-y-5">
              {/* Config Summary Card */}
              {config && (
                <div className="rounded-xl border border-gray-200 overflow-hidden">
                  <div className="px-4 py-3 bg-gray-50 border-b border-gray-200">
                    <h4 className="text-sm font-medium text-gray-700">Configuration</h4>
                  </div>
                  <div className="p-4 space-y-3">
                    <div className="flex items-center gap-3">
                      <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center text-white text-lg">
                        🤖
                      </div>
                      <div>
                        <div className="font-medium text-gray-900">{config.name || 'Unnamed Agent'}</div>
                        <div className="text-xs text-gray-500 capitalize">{config.framework.replace(/_/g, ' ')}</div>
                      </div>
                    </div>
                    <div className="grid grid-cols-2 gap-3 pt-2">
                      <div className="bg-gray-50 rounded-lg p-2.5">
                        <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-0.5">Model</div>
                        <div className="text-xs font-medium text-gray-700 truncate">{config.model.modelId}</div>
                      </div>
                      <div className="bg-gray-50 rounded-lg p-2.5">
                        <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-0.5">Protocol</div>
                        <div className="text-xs font-medium text-gray-700">{config.protocol}</div>
                      </div>
                      <div className="bg-gray-50 rounded-lg p-2.5">
                        <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-0.5">Runtime</div>
                        <div className="text-xs font-medium text-gray-700">{config.pythonRuntime.replace('PYTHON_', 'Python ')}</div>
                      </div>
                      <div className="bg-gray-50 rounded-lg p-2.5">
                        <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-0.5">Memory</div>
                        <div className="text-xs font-medium text-gray-700">{connectedTools.includes('memory') ? 'Enabled' : 'Disabled'}</div>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {/* MCP Server Runtime */}
              {mcpServerConfig && (
                <div className="rounded-xl border border-purple-200 bg-purple-50 p-4">
                  <div className="text-xs font-medium text-purple-700 mb-2 flex items-center gap-1.5">
                    <span>🛠️</span> MCP Server Runtime Target
                  </div>
                  <div className="text-xs text-purple-600">
                    A FastMCP server <strong>{(mcpServerConfig as Record<string, string>).name || 'MCP Server'}</strong> will be deployed as an AgentCore Runtime and connected as a Gateway target.
                  </div>
                </div>
              )}

              {/* Connectors (Phase A — SaaS) */}
              {connectors.length > 0 && (
                <div className="rounded-xl border border-indigo-200 bg-indigo-50 p-4">
                  <div className="text-xs font-medium text-indigo-700 mb-2">Connectors</div>
                  <div className="flex flex-wrap gap-2">
                    {connectors.map((c) => (
                      <span key={c.connector_id} className="px-2.5 py-1 bg-indigo-100 text-indigo-700 rounded-full text-xs font-medium flex items-center gap-1">
                        🧩 {c.connector_id} · {c.auth_method === 'oauth2_cc' ? 'OAuth' : 'API key'}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Connected Tools */}
              {(connectedTools.length > 0 || gatewayTools.length > 0) && (
                <div className="rounded-xl border border-blue-200 bg-blue-50 p-4">
                  <div className="text-xs font-medium text-blue-700 mb-2">Connected Tools</div>
                  <div className="flex flex-wrap gap-2">
                    {connectedTools.map(tool => (
                      <span key={tool} className="px-2.5 py-1 bg-blue-100 text-blue-700 rounded-full text-xs font-medium flex items-center gap-1">
                        {tool === 'browser' && '🌐'}
                        {tool === 'code_interpreter' && '💻'}
                        {tool === 'memory' && '🧠'}
                        {tool === 'gateway' && '🔌'}
                        {tool === 'identity' && '🔐'}
                        {tool === 'observability' && '📊'}
                        {tool === 'policy' && '🛡️'}
                        {tool.replace(/_/g, ' ')}
                      </span>
                    ))}
                    {gatewayTools.map(toolId => (
                      <span key={toolId} className="px-2.5 py-1 bg-yellow-100 text-yellow-700 rounded-full text-xs font-medium flex items-center gap-1">
                        {toolId === 'duckduckgo_search' && '🦆'}
                        {toolId === 'web_page_fetcher' && '📄'}
                        {toolId === 'wikipedia_search' && '📚'}
                        {toolId === 'weather_api' && '🌤️'}
                        {toolId === 'get_order' && '📦'}
                        {toolId === 'get_customer' && '👤'}
                        {toolId === 'list_orders' && '📋'}
                        {toolId === 'process_refund' && '💰'}
                        {toolId.replace(/_/g, ' ')}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Template Tools Configuration */}
              {activeTemplate && activeTemplate.builtInTools.length > 0 && (
                <div className="rounded-xl border border-gray-200 overflow-hidden">
                  <div className="px-4 py-3 bg-gradient-to-r from-slate-50 to-slate-100 border-b border-gray-200 flex items-center gap-2">
                    <span className="text-sm">🧰</span>
                    <h4 className="text-sm font-medium text-gray-700">Template Tools Configuration</h4>
                    <span className="ml-auto text-[10px] px-2 py-0.5 bg-[#0972d3]/10 text-[#0972d3] rounded font-medium">
                      {activeTemplate.name}
                    </span>
                  </div>
                  <div className="p-4 space-y-2.5">
                    {activeTemplate.builtInTools.map((tool) => (
                      <div key={tool.name} className="flex items-start gap-3 p-2.5 bg-gray-50 rounded-lg">
                        <span className="text-lg flex-shrink-0 mt-0.5">{tool.icon}</span>
                        <div className="flex-1 min-w-0">
                          <div className="text-xs font-semibold text-gray-800">{tool.name}</div>
                          <div className="text-[11px] text-gray-500 mt-0.5">{tool.description}</div>
                        </div>
                        <div className="flex-shrink-0">
                          <span className="px-1.5 py-0.5 bg-green-100 text-green-700 rounded text-[9px] font-semibold uppercase">Active</span>
                        </div>
                      </div>
                    ))}
                    <div className="text-[10px] text-gray-400 pt-1">
                      These tools are auto-configured in the generated agent code and included in requirements.txt
                    </div>
                  </div>
                </div>
              )}

              {/* Deploy + Download Buttons (inside scroll area) */}
              {deploymentStatus.state === 'idle' && (
                <div className="space-y-2">
                  <button
                    onClick={handleDeploy}
                    disabled={!config}
                    className="w-full py-3 px-4 bg-[#ff9900] text-[#232f3e] rounded-md font-semibold hover:bg-[#ec7211] disabled:bg-[#e9ebed] disabled:text-[#8d99a8] disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2 text-sm"
                  >
                    <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7z" />
                    </svg>
                    Deploy to AgentCore
                  </button>
                </div>
              )}

              {/* Download CF button — visible in idle and deployed states */}
              {(deploymentStatus.state === 'idle' || deploymentStatus.state === 'deployed') && (
                <div>
                  <button
                    onClick={handleDownloadCfn}
                    disabled={!config || isDownloadingCfn}
                    className="w-full py-2.5 px-4 bg-white text-[#0972d3] border border-[#0972d3] rounded-md font-medium hover:bg-[#f2f8fd] disabled:bg-[#e9ebed] disabled:text-[#8d99a8] disabled:border-[#d1d5db] disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2 text-sm"
                  >
                    {isDownloadingCfn ? (
                      <>
                        <div className="w-4 h-4 border-2 border-[#0972d3] border-t-transparent rounded-full animate-spin" />
                        Generating Template...
                      </>
                    ) : (
                      <>
                        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
                        </svg>
                        Download CloudFormation Template
                      </>
                    )}
                  </button>
                </div>
              )}

              {/* Export as Python button — visible in idle and deployed states (Gap 3G) */}
              {(deploymentStatus.state === 'idle' || deploymentStatus.state === 'deployed') && (
                <div>
                  <button
                    onClick={handleExportPython}
                    disabled={!config || isExportingPython}
                    className="w-full py-2.5 px-4 bg-white text-[#0972d3] border border-[#0972d3] rounded-md font-medium hover:bg-[#f2f8fd] disabled:bg-[#e9ebed] disabled:text-[#8d99a8] disabled:border-[#d1d5db] disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2 text-sm"
                  >
                    {isExportingPython ? (
                      <>
                        <div className="w-4 h-4 border-2 border-[#0972d3] border-t-transparent rounded-full animate-spin" />
                        Exporting...
                      </>
                    ) : (
                      <>
                        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
                        </svg>
                        Export as Python
                      </>
                    )}
                  </button>
                </div>
              )}

              {/* Publish to Registry — only once deployed (Gap 2A: deploy -> registry loop) */}
              {deploymentStatus.state === 'deployed' && (
                <div>
                  <button
                    onClick={handlePublishToRegistry}
                    disabled={!config || isPublishing}
                    className="w-full py-2.5 px-4 bg-white text-[#0972d3] border border-[#0972d3] rounded-md font-medium hover:bg-[#f2f8fd] disabled:bg-[#e9ebed] disabled:text-[#8d99a8] disabled:border-[#d1d5db] disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2 text-sm"
                    title="Publish this agent's canvas as a reusable blueprint others can browse and clone"
                  >
                    {isPublishing ? (
                      <>
                        <div className="w-4 h-4 border-2 border-[#0972d3] border-t-transparent rounded-full animate-spin" />
                        Publishing...
                      </>
                    ) : (
                      <>
                        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M12 19V5" /><polyline points="5 12 12 5 19 12" />
                        </svg>
                        Publish to Registry
                      </>
                    )}
                  </button>
                  {publishMsg && (
                    <p className={`mt-2 text-xs ${publishMsg.kind === 'ok' ? 'text-green-700' : 'text-red-600'}`}>
                      {publishMsg.text}
                    </p>
                  )}
                </div>
              )}

              {/* Deploy Status */}
              {deploymentStatus.state === 'deploying' && (
                <div className="flex items-center gap-3 p-3.5 bg-[#ff9900]/5 rounded-lg border border-[#ff9900]/20">
                  <div className="w-5 h-5 border-2 border-[#d45b07] border-t-transparent rounded-full animate-spin flex-shrink-0" />
                  <span className="text-[#16191f] text-sm font-medium">{deploymentStatus.message}</span>
                </div>
              )}

              {deploymentStatus.state === 'deployed' && (
                <div className="space-y-4">
                  <div className="flex items-center gap-2 p-4 bg-green-50 rounded-xl border border-green-100">
                    <div className="w-6 h-6 rounded-full bg-green-500 flex items-center justify-center">
                      <svg className="w-3.5 h-3.5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                      </svg>
                    </div>
                    <span className="text-green-700 font-medium">{deploymentStatus.message}</span>
                  </div>

                  {deploymentStatus.simulated && (
                    <div className="flex items-start gap-2 p-3 bg-amber-50 rounded-xl border border-amber-200">
                      <span className="text-amber-600">⚠️</span>
                      <div className="text-xs text-amber-700">
                        <strong>Simulated Mode:</strong> agentcore CLI not installed. Install with: <code className="bg-amber-100 px-1 rounded">pip install bedrock-agentcore-starter-toolkit</code>
                      </div>
                    </div>
                  )}

                  {/* Endpoint Info */}
                  <div className="rounded-xl border border-gray-200 overflow-hidden">
                    <div className="px-4 py-3 bg-gray-50 border-b border-gray-200 flex items-center justify-between">
                      <h4 className="text-sm font-medium text-gray-700">Endpoint Details</h4>
                      <button
                        onClick={() => navigator.clipboard.writeText(deploymentStatus.endpoint || '')}
                        className="text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1"
                      >
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                        </svg>
                        Copy
                      </button>
                    </div>
                    <div className="p-4 space-y-3">
                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-1">Runtime ID</div>
                        <code className="text-sm font-mono text-gray-800 bg-gray-100 px-2 py-1 rounded">{deploymentStatus.runtimeId}</code>
                      </div>
                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-1">Endpoint URL</div>
                        <code className="text-xs font-mono text-gray-600 break-all block bg-gray-100 p-2 rounded">{deploymentStatus.endpoint}</code>
                      </div>
                      {deploymentStatus.gatewayUrl && (
                        <div>
                          <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-1">Gateway URL (MCP)</div>
                          <code className="text-xs font-mono text-blue-600 break-all block bg-blue-50 p-2 rounded">{deploymentStatus.gatewayUrl}</code>
                        </div>
                      )}
                    </div>
                  </div>

                  {/* CLI Command */}
                  <div className="rounded-xl border border-slate-200 overflow-hidden bg-slate-900">
                    <div className="px-4 py-2.5 border-b border-slate-700 flex items-center gap-2">
                      <div className="flex gap-1.5">
                        <div className="w-3 h-3 rounded-full bg-red-500" />
                        <div className="w-3 h-3 rounded-full bg-yellow-500" />
                        <div className="w-3 h-3 rounded-full bg-green-500" />
                      </div>
                      <span className="text-xs text-slate-400 ml-2">AWS CLI</span>
                    </div>
                    <pre className="p-4 text-xs text-green-400 font-mono overflow-x-auto">
{`aws bedrock-agent-runtime invoke-agent \\
  --agent-id ${deploymentStatus.runtimeId} \\
  --agent-alias-id TSTALIASID \\
  --session-id test-session \\
  --input-text "Hello"`}</pre>
                  </div>

                  <button
                    onClick={() => setDeploymentStatus({ state: 'idle' })}
                    className="w-full py-2.5 px-4 border border-gray-300 rounded-xl text-gray-700 hover:bg-gray-50 transition-colors text-sm"
                  >
                    Redeploy
                  </button>
                  <button
                    onClick={handleDelete}
                    disabled={isDeleting}
                    className="w-full py-2.5 px-4 border border-red-300 rounded-xl text-red-600 hover:bg-red-50 transition-colors text-sm flex items-center justify-center gap-2"
                  >
                    {isDeleting ? (
                      <>
                        <div className="w-4 h-4 border-2 border-red-400 border-t-transparent rounded-full animate-spin" />
                        Deleting...
                      </>
                    ) : (
                      <>🗑️ Delete from AWS</>
                    )}
                  </button>
                </div>
              )}

              {deploymentStatus.state === 'error' && (
                <div className="space-y-3">
                  <div className="flex items-start gap-3 p-4 bg-red-50 rounded-xl border border-red-100">
                    <div className="w-6 h-6 rounded-full bg-red-500 flex items-center justify-center flex-shrink-0 mt-0.5">
                      <svg className="w-3.5 h-3.5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </div>
                    <span className="text-red-700 text-sm">{deploymentStatus.message}</span>
                  </div>
                  <button
                    onClick={handleDeploy}
                    className="w-full py-2.5 px-4 bg-[#ff9900] text-[#232f3e] rounded-md font-semibold hover:bg-[#ec7211] transition-colors text-sm"
                  >
                    Retry Deployment
                  </button>
                </div>
              )}
            </div>
          )}

          {activeTab === 'chat' && deploymentStatus.state === 'deployed' && (
            <div className="flex flex-col flex-1 min-h-0">
              {/* Session Header Bar */}
              <div className="flex items-center justify-between px-4 py-2.5 border-b border-[#e9ebed] bg-[#fafafa] flex-shrink-0">
                <div className="flex items-center gap-2">
                  <div className="w-2 h-2 bg-emerald-500 rounded-full animate-pulse" />
                  <span className="text-xs text-[#5f6b7a]">
                    {sessionId ? `Session: ${sessionId.slice(0, 8)}...` : 'New Session'}
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <button
                    onClick={handleNewSession}
                    className="text-xs text-[#0972d3] hover:text-[#0961b9] font-medium"
                  >
                    + New
                  </button>
                  <button
                    onClick={handleDelete}
                    disabled={isDeleting}
                    className="text-xs text-red-500 hover:text-red-700 font-medium"
                  >
                    {isDeleting ? 'Deleting...' : 'Delete'}
                  </button>
                </div>
              </div>

              {/* Chat Messages Area */}
              <div className="flex-1 overflow-y-auto p-4 space-y-3 min-h-0">
                {/* Empty state */}
                {chatMessages.length === 0 && !isTesting && (
                  <div className="text-center py-12">
                    <div className="w-12 h-12 mx-auto mb-3 rounded-xl bg-[#0972d3]/10 flex items-center justify-center">
                      <svg className="w-6 h-6 text-[#0972d3]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                      </svg>
                    </div>
                    <h4 className="text-sm font-medium text-[#16191f] mb-1">Chat with your Agent</h4>
                    <p className="text-xs text-[#5f6b7a]">Send a message to test your deployed agent</p>
                  </div>
                )}

                {/* Message Bubbles */}
                {chatMessages.map((msg) => (
                  <div key={msg.id}>
                    {msg.role === 'system' ? (
                      <div className="flex justify-center">
                        <div className="px-3 py-1.5 bg-amber-50 border border-amber-200 rounded-lg max-w-[90%]">
                          <span className="text-xs text-amber-700">{msg.content}</span>
                        </div>
                      </div>
                    ) : msg.role === 'user' ? (
                      <div className="flex justify-end">
                        <div className="max-w-[85%] px-4 py-2.5 rounded-2xl rounded-br-md bg-[#0972d3] text-white">
                          <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
                        </div>
                      </div>
                    ) : (
                      <div className="flex justify-start gap-2">
                        <div className="w-6 h-6 rounded-full bg-[#232f3e] flex items-center justify-center flex-shrink-0 mt-1">
                          <span className="text-[10px] text-white font-bold">A</span>
                        </div>
                        <div className="max-w-[85%]">
                          <div className="px-4 py-2.5 rounded-2xl rounded-bl-md bg-[#f2f3f3] text-[#16191f] border border-[#e9ebed]">
                            <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
                          </div>
                          {msg.latencyMs && (
                            <span className="text-[10px] text-[#8d99a8] mt-1 ml-2 inline-block">{msg.latencyMs}ms</span>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                ))}

                {/* Typing Indicator */}
                {isTesting && (
                  <div className="flex justify-start gap-2">
                    <div className="w-6 h-6 rounded-full bg-[#232f3e] flex items-center justify-center flex-shrink-0 mt-1">
                      <span className="text-[10px] text-white font-bold">A</span>
                    </div>
                    <div className="px-4 py-3 rounded-2xl rounded-bl-md bg-[#f2f3f3] border border-[#e9ebed]">
                      <div className="flex items-center gap-1.5">
                        <div className="w-2 h-2 bg-[#0972d3] rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                        <div className="w-2 h-2 bg-[#0972d3] rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                        <div className="w-2 h-2 bg-[#0972d3] rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                      </div>
                    </div>
                  </div>
                )}

                <div ref={chatEndRef} />
              </div>

              {/* Input Area */}
              <div className="border-t border-[#e9ebed] bg-[#fafafa] p-3 flex-shrink-0">
                <div className="flex gap-2">
                  <textarea
                    ref={chatInputRef}
                    value={testInput}
                    onChange={(e) => setTestInput(e.target.value)}
                    onKeyDown={handleKeyDown}
                    placeholder="Type a message..."
                    className="flex-1 resize-none rounded-xl border border-[#e9ebed] px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-[#0972d3] focus:border-transparent bg-white"
                    rows={1}
                    disabled={isTesting}
                  />
                  <button
                    onClick={handleTest}
                    disabled={!testInput.trim() || isTesting}
                    className={`self-end p-2.5 rounded-xl transition-all ${
                      testInput.trim() && !isTesting
                        ? 'bg-[#0972d3] text-white hover:bg-[#0961b9]'
                        : 'bg-[#e9ebed] text-[#8d99a8] cursor-not-allowed'
                    }`}
                  >
                    {isTesting ? (
                      <div className="w-5 h-5 border-2 border-current border-t-transparent rounded-full animate-spin" />
                    ) : (
                      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7z" />
                      </svg>
                    )}
                  </button>
                </div>
              </div>
            </div>
          )}
          {activeTab === 'versions' && (
            <VersionsList
              runtimeName={config?.name ?? null}
              refreshKey={versionsRefreshKey}
            />
          )}
          {activeTab === 'evals' && (
            <EvaluationResultsPanel
              runtimeName={config?.name ?? null}
              refreshKey={versionsRefreshKey}
            />
          )}
          {activeTab === 'cost' && (
            <CostPanel
              runtimeName={config?.name ?? null}
              refreshKey={versionsRefreshKey}
            />
          )}
          {activeTab === 'observability' && (
            <ObservabilityPanel
              runtimeName={config?.name ?? null}
              refreshKey={versionsRefreshKey}
            />
          )}
          {activeTab === 'triggers' && (
            <TriggersPanel
              runtimeName={config?.name ?? null}
              refreshKey={versionsRefreshKey}
            />
          )}
        </div>

        {/* Footer with Deploy button — visible on deploy tab */}
        {activeTab !== 'chat' && (
        <div className="border-t border-[#e9ebed] bg-[#fafafa] flex-shrink-0 p-3.5 space-y-2">
          {activeTab === 'deploy' && (deploymentStatus.state === 'idle' || deploymentStatus.state === 'error') && (
            <button
              onClick={handleDeploy}
              disabled={!config}
              className="w-full py-3 px-4 bg-[#ff9900] text-[#232f3e] rounded-md font-semibold hover:bg-[#ec7211] disabled:bg-[#e9ebed] disabled:text-[#8d99a8] disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2 text-sm"
            >
              <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7z" />
              </svg>
              {deploymentStatus.state === 'error' ? 'Retry Deployment' : 'Deploy to AgentCore'}
            </button>
          )}
          {activeTab === 'deploy' && deploymentStatus.state === 'deploying' && (
            <div className="flex items-center justify-center gap-2 py-2 text-[#d45b07] text-sm font-medium">
              <div className="w-4 h-4 border-2 border-[#d45b07] border-t-transparent rounded-full animate-spin" />
              Deploying...
            </div>
          )}
          <div className="flex items-center justify-center gap-1.5 text-[10px] text-[#8d99a8]">
            <svg className="w-3 h-3" viewBox="0 0 24 24" fill="currentColor">
              <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
            </svg>
            Powered by Amazon Bedrock AgentCore
          </div>
        </div>
        )}
      </div>
    </>
  );
}

export default DeployPanel;
