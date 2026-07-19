/**
 * DeployPanel component for deploying and testing AgentCore Runtime.
 */

import { useState, useCallback, useMemo, useEffect } from 'react';
import { m } from 'motion/react';
import { spring, tween } from '../../lib/motion';
import type { RuntimeConfiguration, GatewayConfiguration, IdentityConfiguration } from '../../types/components';
import { authFetch } from '../../auth/authFetch';
import { WORKFLOW_TEMPLATES } from '../../data/templates';
import { useWorkflowStore } from '../../store/workflowStore';
import { publishToRegistryApi } from '../../services/api';
import { VersionsList } from './VersionsList';
import { EvaluationResultsPanel } from './EvaluationResultsPanel';
import { CostPanel } from './CostPanel';
import { ObservabilityPanel } from './ObservabilityPanel';
import { TraceWaterfall } from '../observability/TraceWaterfall';
import { TriggersPanel } from './TriggersPanel';
import { ResourceTagFields, type ResourceTagState } from './ResourceTagFields';
import { ConfigSummary } from './ConfigSummary';
import { DeployProgress } from './DeployProgress';
import { DeployResult } from './DeployResult';
import { DeployActions } from './DeployActions';
import { useDeployment } from './useDeployment';
import { mapGatewayDeployTargets } from '../../utils/gatewayConfig';
import { ConfirmDialog } from '../common/ConfirmDialog';
import { ChatInterface } from './ChatInterface';

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
  deploymentMode?: 'runtime' | 'harness';
  isVisible: boolean;
  onClose: () => void;
  restoredDeployment?: {
    runtimeId: string;
    endpoint: string;
    gatewayUrl?: string;
  } | null;
}

export function DeployPanel({
  config,
  nodeId,
  connectedTools = [],
  gatewayConfig,
  gatewayTools = [],
  templateId,
  identityConfig,
  customTools = [],
  connectors = [],
  memoryConfig,
  evaluationConfig,
  policyConfig,
  guardrailsConfig,
  mcpServerConfig,
  knowledgeBaseConfig,
  observabilityConfig,
  a2aConfig,
  deploymentMode = 'runtime',
  isVisible,
  onClose,
  restoredDeployment,
}: DeployPanelProps) {
  const [testInput, setTestInput] = useState('');
  const [, setTestResult] = useState<TestResult | null>(null);
  const [isTesting, setIsTesting] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [activeTab, setActiveTab] = useState<'deploy' | 'chat' | 'versions' | 'evals' | 'cost' | 'observability' | 'triggers'>('deploy');
  const [versionsRefreshKey, setVersionsRefreshKey] = useState(0);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [resourceTagState, setResourceTagState] = useState<ResourceTagState>({ tags: {}, profileName: null });
  const [conversationHistory, setConversationHistory] = useState<Array<{role: string, content: string}>>([]);
  const [chatMessages, setChatMessages] = useState<Array<{
    id: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    timestamp: Date;
    latencyMs?: number;
  }>>([]);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

  const activeTemplate = useMemo(() => {
    if (!templateId) return null;
    return WORKFLOW_TEMPLATES.find((t) => t.id === templateId) || null;
  }, [templateId]);

  // Split the gateway's mixed targets[] (falling back to the single legacy
  // target) into the two arrays the deploy path needs: `externalMcpServers`
  // (mcp_server family, secret-carrying) and `gatewayTargets` (openapi / lambda
  // / smithy) which we thread into gatewayConfig.targets for the backend loop.
  const { externalMcpServers, gatewayConfigForDeploy } = useMemo(() => {
    if (!gatewayConfig) return { externalMcpServers: undefined, gatewayConfigForDeploy: gatewayConfig };
    const { externalMcpServers: mcp, gatewayTargets } = mapGatewayDeployTargets(gatewayConfig);
    return {
      externalMcpServers: mcp.length > 0 ? mcp : undefined,
      // Backend deploy loop reads gateway_config.targets for the non-MCP
      // families. Overwrite with just those so mcp_server entries (handled via
      // externalMcpServers) aren't double-deployed.
      gatewayConfigForDeploy: { ...gatewayConfig, targets: gatewayTargets },
    };
  }, [gatewayConfig]);

  const [isDownloadingCfn, setIsDownloadingCfn] = useState(false);
  const [isExportingPython, setIsExportingPython] = useState(false);
  const [isPublishing, setIsPublishing] = useState(false);
  const [publishMsg, setPublishMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const warmupRuntime = useCallback((runtimeId: string, endpoint?: string) => {
    authFetch('/api/test-runtime', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        endpoint: endpoint || '',
        input: 'ping',
        runtimeId,
      }),
    }).catch(() => {});
  }, []);

  const { deploymentStatus, setDeploymentStatus, handleDeploy } = useDeployment({
    config,
    nodeId,
    deploymentMode,
    connectedTools,
    gatewayConfig: gatewayConfigForDeploy || null,
    externalMcpServers,
    gatewayTools,
    templateId: templateId || null,
    identityConfig: identityConfig || null,
    customTools,
    connectors,
    memoryConfig: memoryConfig || null,
    evaluationConfig: evaluationConfig || null,
    policyConfig: policyConfig || null,
    guardrailsConfig: guardrailsConfig || null,
    mcpServerConfig: mcpServerConfig || null,
    knowledgeBaseConfig: knowledgeBaseConfig || null,
    observabilityConfig: observabilityConfig || null,
    a2aConfig: a2aConfig || null,
    resourceTagState,
    warmupRuntime,
    onVersionsRefresh: () => setVersionsRefreshKey((k) => k + 1),
    onTabChange: (tab) => setActiveTab(tab),
  });


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
          nodeId, config: fullConfig, connectedTools, gatewayConfig: gatewayConfigForDeploy, gatewayTools, templateId,
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
      if (!response.ok) throw new Error(`Python export failed (${response.status})`);
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
  }, [config, nodeId, connectedTools, gatewayConfigForDeploy, externalMcpServers, gatewayTools, templateId, customTools, connectors, memoryConfig, evaluationConfig, policyConfig, guardrailsConfig, mcpServerConfig, knowledgeBaseConfig, observabilityConfig, a2aConfig, identityConfig, resourceTagState, setDeploymentStatus]);

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
          nodeId, config: fullConfig, connectedTools, gatewayConfig: gatewayConfigForDeploy, gatewayTools, templateId,
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
      if (!response.ok) throw new Error(`Template generation failed (${response.status})`);
      const result = await response.json();
      if (result.download_url) {
        const a = document.createElement('a');
        a.href = result.download_url;
        a.download = result.filename || 'agentcore-cfn.zip';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      } else if (result.zip_base64) {
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
  }, [config, nodeId, connectedTools, gatewayConfigForDeploy, externalMcpServers, gatewayTools, templateId, customTools, connectors, memoryConfig, evaluationConfig, policyConfig, guardrailsConfig, mcpServerConfig, knowledgeBaseConfig, identityConfig, a2aConfig, observabilityConfig, resourceTagState, setDeploymentStatus]);

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

  const handleTest = useCallback(async () => {
    if (!deploymentStatus.endpoint && !deploymentStatus.runtimeId) return;
    setIsTesting(true);
    setTestResult(null);
    const startTime = Date.now();
    const MAX_RETRIES = 5;
    const streamingMsgId = `assistant-streaming-${Date.now()}`;

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
          buffer = lines.pop() || '';
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
        setChatMessages(prev => prev.filter(m => m.id !== streamingMsgId));
        return false;
      }
    };

    try {
      if (await tryStreaming()) return;

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
    setShowDeleteConfirm(false);
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
  }, [deploymentStatus.runtimeId, setDeploymentStatus]);

  if (!isVisible) return null;

  return (
    <>
      <m.div
        className="fixed inset-0 z-40"
        style={{ background: 'rgba(11, 18, 32, 0.28)', backdropFilter: 'blur(2px)' }}
        onClick={onClose}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={tween.base}
      />

      <m.div
        className="fixed right-0 top-0 bottom-0 w-[420px] bg-white z-50 flex flex-col overflow-hidden border-l border-[#e9ebed]"
        style={{ boxShadow: 'var(--elevation-4)' }}
        initial={{ x: '100%' }}
        animate={{ x: 0 }}
        transition={spring.gentle}
      >
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
          <button onClick={onClose} className="p-1.5 rounded-md hover:bg-white/10 transition-colors">
            <svg className="w-4 h-4 text-white/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="flex border-b border-[#e9ebed]">
          <button onClick={() => setActiveTab('deploy')} className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${activeTab === 'deploy' ? 'text-[#0972d3]' : 'text-[#5f6b7a] hover:text-[#16191f]'}`}>
            Deploy
            {activeTab === 'deploy' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />}
          </button>
          <button onClick={() => setActiveTab('chat')} disabled={deploymentStatus.state !== 'deployed'} className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${activeTab === 'chat' ? 'text-[#0972d3]' : deploymentStatus.state === 'deployed' ? 'text-[#5f6b7a] hover:text-[#16191f]' : 'text-[#d1d5db] cursor-not-allowed'}`}>
            Chat
            {deploymentStatus.state === 'deployed' && <span className="ml-1.5 w-1.5 h-1.5 bg-emerald-500 rounded-full inline-block" />}
            {activeTab === 'chat' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />}
          </button>
          <button onClick={() => setActiveTab('versions')} className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${activeTab === 'versions' ? 'text-[#0972d3]' : 'text-[#5f6b7a] hover:text-[#16191f]'}`}>
            Versions
            {activeTab === 'versions' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />}
          </button>
          <button onClick={() => setActiveTab('evals')} className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${activeTab === 'evals' ? 'text-[#0972d3]' : 'text-[#5f6b7a] hover:text-[#16191f]'}`}>
            Eval
            {activeTab === 'evals' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />}
          </button>
          <button onClick={() => setActiveTab('cost')} className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${activeTab === 'cost' ? 'text-[#0972d3]' : 'text-[#5f6b7a] hover:text-[#16191f]'}`}>
            Cost
            {activeTab === 'cost' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />}
          </button>
          <button onClick={() => setActiveTab('observability')} className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${activeTab === 'observability' ? 'text-[#0972d3]' : 'text-[#5f6b7a] hover:text-[#16191f]'}`}>
            Observe
            {activeTab === 'observability' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />}
          </button>
          <button onClick={() => setActiveTab('triggers')} className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${activeTab === 'triggers' ? 'text-[#0972d3]' : 'text-[#5f6b7a] hover:text-[#16191f]'}`}>
            Triggers
            {activeTab === 'triggers' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#0972d3]" />}
          </button>
        </div>

        <div className={`flex-1 min-h-0 ${activeTab === 'chat' ? 'flex flex-col' : 'overflow-y-auto'}`}>
          {activeTab === 'deploy' && (
            <div className="p-5 space-y-5">
              {config && (
                <ConfigSummary
                  config={config}
                  connectedTools={connectedTools}
                  mcpServerConfig={mcpServerConfig || null}
                  connectors={connectors}
                  gatewayTools={gatewayTools}
                  activeTemplate={activeTemplate}
                />
              )}

              {deploymentStatus.state === 'idle' && (
                <ResourceTagFields onChange={setResourceTagState} />
              )}

              {deploymentStatus.state === 'deploying' && (
                <DeployProgress message={deploymentStatus.message || 'Deploying...'} />
              )}

              {deploymentStatus.state === 'deployed' && (
                <DeployResult
                  message={deploymentStatus.message || 'Deployed successfully!'}
                  simulated={deploymentStatus.simulated}
                  runtimeId={deploymentStatus.runtimeId}
                  endpoint={deploymentStatus.endpoint}
                  gatewayUrl={deploymentStatus.gatewayUrl}
                  onRedeploy={() => setDeploymentStatus({ state: 'idle' })}
                  onDelete={() => setShowDeleteConfirm(true)}
                  isDeleting={isDeleting}
                />
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

              <DeployActions
                canDeploy={!!config}
                state={deploymentStatus.state}
                isDownloadingCfn={isDownloadingCfn}
                isExportingPython={isExportingPython}
                isPublishing={isPublishing}
                publishMsg={publishMsg}
                onDeploy={handleDeploy}
                onDownloadCfn={handleDownloadCfn}
                onExportPython={handleExportPython}
                onPublish={handlePublishToRegistry}
              />
            </div>
          )}

          {activeTab === 'chat' && deploymentStatus.state === 'deployed' && (
            <div className="flex flex-col flex-1 min-h-0">
              <div className="flex items-center justify-between px-4 py-2.5 border-b border-[#e9ebed] bg-[#fafafa] flex-shrink-0">
                <div className="flex items-center gap-2">
                  <div className="w-2 h-2 bg-emerald-500 rounded-full animate-pulse" />
                  <span className="text-xs text-[#5f6b7a]">
                    {sessionId ? `Session: ${sessionId.slice(0, 8)}...` : 'New Session'}
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <button onClick={handleNewSession} className="text-xs text-[#0972d3] hover:text-[#0961b9] font-medium">
                    + New
                  </button>
                  <button onClick={() => setShowDeleteConfirm(true)} disabled={isDeleting} className="text-xs text-red-500 hover:text-red-700 font-medium">
                    {isDeleting ? 'Deleting...' : 'Delete'}
                  </button>
                </div>
              </div>

              {/* ChatInterface is ALWAYS mounted so the message input is
                  available for the first message — it renders the empty-state
                  placeholder itself when there are no messages yet. (Previously
                  the empty state replaced the whole component, hiding the input
                  and making a fresh session un-chattable.) */}
              <ChatInterface
                chatMessages={chatMessages}
                testInput={testInput}
                isTesting={isTesting}
                onTestInputChange={setTestInput}
                onSendMessage={handleTest}
                onKeyDown={handleKeyDown}
              />
            </div>
          )}
          {activeTab === 'versions' && <VersionsList runtimeName={config?.name ?? null} refreshKey={versionsRefreshKey} />}
          {activeTab === 'evals' && <EvaluationResultsPanel runtimeName={config?.name ?? null} refreshKey={versionsRefreshKey} />}
          {activeTab === 'cost' && <CostPanel runtimeName={config?.name ?? null} refreshKey={versionsRefreshKey} />}
          {activeTab === 'observability' && (
            <>
              <ObservabilityPanel runtimeName={config?.name ?? null} refreshKey={versionsRefreshKey} />
              <TraceWaterfall runtimeName={config?.name ?? null} refreshKey={versionsRefreshKey} />
            </>
          )}
          {activeTab === 'triggers' && <TriggersPanel runtimeName={config?.name ?? null} refreshKey={versionsRefreshKey} />}
        </div>

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
      </m.div>

      <ConfirmDialog
        isOpen={showDeleteConfirm}
        title="Delete Runtime"
        message="Are you sure you want to delete this runtime from AWS? This action cannot be undone."
        confirmLabel="Delete"
        cancelLabel="Cancel"
        variant="danger"
        onConfirm={handleDelete}
        onCancel={() => setShowDeleteConfirm(false)}
      />
    </>
  );
}

export default DeployPanel;
