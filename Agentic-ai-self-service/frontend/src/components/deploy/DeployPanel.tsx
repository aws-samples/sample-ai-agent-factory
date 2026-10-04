/**
 * DeployPanel component for deploying and testing AgentCore Runtime.
 *
 * Like `TemplateGallery`, this drawer hand-rolls its scrim and panel rather than using
 * `ModalShell`, and so had none of ModalShell's dialog affordances. Measured against
 * the deployed bundle: Escape did not dismiss it, nothing carried `role="dialog"` or
 * `aria-modal`, and the header's close button had no accessible name at all. The only
 * exits were a mouse click on the scrim or on an unnamed icon button.
 *
 * Escape closes, deliberately doing exactly what the scrim and the X already do — it
 * is not suppressed mid-deployment, because a deployment that is still running is
 * recoverable through `ActiveDeploymentBanner`, and an Escape that sometimes works is
 * worse than one that always does. The shared focus trap keeps both keyboard and
 * programmatic focus inside the topmost dialog.
 */

import { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import { m } from 'motion/react';
import { spring, tween } from '../../lib/motion';
import type { RuntimeConfiguration, GatewayConfiguration, IdentityConfiguration } from '../../types/components';
import { authFetch } from '../../auth/authFetch';
import { useScopes } from '../../auth/scopes';
import { WORKFLOW_TEMPLATES } from '../../data/templates';
import { useWorkflowStore } from '../../store/workflowStore';
import {
  createRegistryCanvasSnapshot,
  publishToRegistryApi,
} from '../../services/api';
import { VersionsList } from './VersionsList';
import { EvaluationResultsPanel } from './EvaluationResultsPanel';
import { CostPanel } from './CostPanel';
import { ObservabilityPanel } from './ObservabilityPanel';
import { TraceWaterfall } from '../observability/TraceWaterfall';
import { TriggersPanel } from './TriggersPanel';
import { ResourceTagFields } from './ResourceTagFields';
import {
  governanceTagsFromResourceState,
  resourceTagStateFromGovernance,
  type ResourceTagState,
  type TagGovernanceStatus,
} from './resourceTagState';
import {
  ResourceNamingFields,
  resourceNamingStateFromProfile,
  type ResourceNamingState,
} from './ResourceNamingFields';
import { ConfigSummary } from './ConfigSummary';
import { DeployProgress } from './DeployProgress';
import { DeployResult } from './DeployResult';
import { DeployActions } from './DeployActions';
import {
  DeploymentTargetFields,
  type DeploymentTargetSelection,
} from './DeploymentTargetFields';
import { useDeployment } from './useDeployment';
import { mapGatewayDeployTargets } from '../../utils/gatewayConfig';
import { ConfirmDialog } from '../common/ConfirmDialog';
import { ChatInterface } from './ChatInterface';
import { McpToolsPanel } from './McpToolsPanel';
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap';
import { runtimeConfigForRequest } from '../../utils/runtimeRequestConfig';

const PYTHON_EXPORT_GOVERNANCE_MESSAGE =
  'Standalone Python export contains no AWS resources, so tags and tag profiles cannot be applied. Clear the governance settings, or use the CloudFormation export or platform deploy instead.';

/** The backend's FastAPI ``detail`` as text: a string, or a validation list of ``{msg}``. */
async function responseDetail(response: Response, fallback: string): Promise<string> {
  if (response.status >= 500) return fallback;
  const body = await response.json().catch(() => null);
  const detail = body?.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => (typeof d === 'string' ? d : typeof d?.msg === 'string' ? d.msg : ''))
      .filter(Boolean);
    if (parts.length) return parts.join('; ');
  }
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message;
  return fallback;
}

type DeployPanelTab =
  | 'deploy'
  | 'chat'
  | 'tools'
  | 'versions'
  | 'evals'
  | 'cost'
  | 'observability'
  | 'triggers';

type CfnDataRetentionPolicy = 'Retain' | 'Delete';

const DEPLOY_PANEL_TABS: ReadonlyArray<{ id: DeployPanelTab; label: string }> = [
  { id: 'deploy', label: 'Deploy' },
  { id: 'chat', label: 'Chat' },
  { id: 'tools', label: 'MCP Tools' },
  { id: 'versions', label: 'Versions' },
  { id: 'evals', label: 'Eval' },
  { id: 'cost', label: 'Cost' },
  { id: 'observability', label: 'Observe' },
  { id: 'triggers', label: 'Triggers' },
];

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
  /** Saved flow containing the node, if this is a visual-canvas deploy. */
  flowId?: string | null;
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
    deploymentId?: string;
    runtimeId: string;
    endpoint: string;
    runtimeProtocol?: 'HTTP' | 'MCP' | 'A2A';
    gatewayUrl?: string;
  } | null;
}

export function DeployPanel({
  config,
  nodeId,
  flowId,
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
  const { hasScope } = useScopes();
  const governance = useWorkflowStore((state) => state.governance);
  const setGovernance = useWorkflowStore((state) => state.setGovernance);
  // The canvas validator's verdict gates every side-effecting action here. Live (2026-09-28) a
  // gateway with no target read "Ready to deploy", deployed, and was refused by the deployer
  // minutes later; the "1 Error" badge sat behind this very panel. Deploy, the CloudFormation
  // export and the Python export all send the canvas, so all three wait for a valid one.
  // Keyed on a verdict that EXISTS: every store mutation that changes the canvas runs
  // validation (see validation-on-canvas-change), so a null verdict means an empty, never
  // hydrated canvas, which has no config to deploy anyway.
  const canvasIsValid = useWorkflowStore((state) => state.validationState?.isValid ?? null);
  const firstValidationError = useWorkflowStore(
    (state) => state.validationState?.errors[0]?.message ?? null,
  );
  const validationBlockedReason = canvasIsValid === null || canvasIsValid
    ? null
    : firstValidationError
      ? `Fix the canvas first: ${firstValidationError}`
      : 'Fix the canvas validation errors first';
  const [testInput, setTestInput] = useState('');
  const [, setTestResult] = useState<TestResult | null>(null);
  const [isTesting, setIsTesting] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [activeTab, setActiveTab] = useState<DeployPanelTab>('deploy');
  const [versionsRefreshKey, setVersionsRefreshKey] = useState(0);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const resourceTagState = useMemo(
    () => resourceTagStateFromGovernance(governance.tags),
    [governance.tags],
  );
  const setResourceTagState = useCallback((next: ResourceTagState) => {
    setGovernance((current) => ({
      ...current,
      tags: governanceTagsFromResourceState(next),
    }));
  }, [setGovernance]);
  const [tagGovernanceStatus, setTagGovernanceStatus] = useState<TagGovernanceStatus>({
    state: 'loading',
    message: 'Loading tag governance…',
    missingRequired: false,
  });
  const [resourceNamingState, setResourceNamingState] = useState<ResourceNamingState>(
    () => resourceNamingStateFromProfile(governance.namingProfile),
  );
  const namingProfileKey = JSON.stringify(governance.namingProfile);
  const lastNamingProfileKeyRef = useRef(namingProfileKey);
  useEffect(() => {
    if (lastNamingProfileKeyRef.current !== namingProfileKey) {
      lastNamingProfileKeyRef.current = namingProfileKey;
      setResourceNamingState(resourceNamingStateFromProfile(governance.namingProfile));
    }
  }, [governance.namingProfile, namingProfileKey]);
  const handleResourceNamingChange = useCallback((next: ResourceNamingState) => {
    setResourceNamingState(next);
    if (!next.error) {
      setGovernance((current) => ({
        ...current,
        namingProfile: next.profile,
      }));
    }
  }, [setGovernance]);
  const [deploymentTarget, setDeploymentTarget] = useState<DeploymentTargetSelection>({});
  const [conversationHistory, setConversationHistory] = useState<Array<{role: string, content: string}>>([]);
  const [chatMessages, setChatMessages] = useState<Array<{
    id: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    timestamp: Date;
    latencyMs?: number;
  }>>([]);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  useDialogFocusTrap(isVisible, panelRef, undefined, onClose);

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
  const [cfnDataRetentionPolicy, setCfnDataRetentionPolicy] =
    useState<CfnDataRetentionPolicy>('Retain');
  // An export failure belongs to the button that failed, NOT to the deployment:
  // writing it into deploymentStatus hid a live runtime's Chat/Delete, and from idle
  // it hid the very settings to correct and turned the CTA into "Retry Deployment".
  const [exportError, setExportError] = useState<{ kind: 'cfn' | 'python'; message: string } | null>(null);
  useEffect(() => {
    setExportError(null);
  }, [cfnDataRetentionPolicy, resourceTagState, resourceNamingState]);
  const [isPublishing, setIsPublishing] = useState(false);
  const [publishMsg, setPublishMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const governanceReady = (
    tagGovernanceStatus.state === 'ready'
    && !tagGovernanceStatus.missingRequired
  );

  const warmupRuntime = useCallback((runtimeId: string, endpoint?: string) => {
    authFetch('/api/test-runtime', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        endpoint: endpoint || '',
        input: 'ping',
        runtimeId,
        // Tells a Memory agent this ping is not a conversation turn, so it is not stored.
        warmup: true,
      }),
    }).catch(() => {});
  }, []);

  const { deploymentStatus, setDeploymentStatus, handleDeploy } = useDeployment({
    config,
    nodeId,
    flowId,
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
    targetAccountId: deploymentTarget.targetAccountId,
    targetRegion: deploymentTarget.targetRegion,
    warmupRuntime,
    onVersionsRefresh: () => setVersionsRefreshKey((k) => k + 1),
    onTabChange: (tab) => setActiveTab(tab),
  });
  const isMcpDeployment = (
    deploymentStatus.state === 'deployed'
    && deploymentStatus.runtimeProtocol === 'MCP'
  );
  const isMcpProtocol = (
    isMcpDeployment
    || config?.protocol === 'MCP'
  );
  const visibleTabs = useMemo(
    () => DEPLOY_PANEL_TABS.filter((tab) => {
      if (tab.id === 'chat') return !isMcpProtocol;
      if (tab.id === 'tools') return isMcpProtocol;
      if (tab.id === 'triggers') return !isMcpProtocol;
      return true;
    }),
    [isMcpProtocol],
  );

  useEffect(() => {
    if (visibleTabs.some((tab) => tab.id === activeTab)) return;
    setActiveTab(isMcpDeployment ? 'tools' : 'deploy');
  }, [activeTab, isMcpDeployment, visibleTabs]);

  const handleTabKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLButtonElement>, currentTab: DeployPanelTab) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;

      const enabledTabs = visibleTabs.filter(
        (tab) => (
          (tab.id !== 'chat' && tab.id !== 'tools')
          || deploymentStatus.state === 'deployed'
        ),
      );
      const currentIndex = Math.max(
        0,
        enabledTabs.findIndex((tab) => tab.id === currentTab),
      );
      let nextIndex = currentIndex;
      if (event.key === 'Home') nextIndex = 0;
      if (event.key === 'End') nextIndex = enabledTabs.length - 1;
      if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % enabledTabs.length;
      if (event.key === 'ArrowLeft') {
        nextIndex = (currentIndex - 1 + enabledTabs.length) % enabledTabs.length;
      }

      event.preventDefault();
      const nextTab = enabledTabs[nextIndex].id;
      setActiveTab(nextTab);
      document.getElementById(`deploy-panel-tab-${nextTab}`)?.focus();
    },
    [deploymentStatus.state, visibleTabs],
  );


  useEffect(() => {
    if (restoredDeployment && deploymentStatus.state === 'idle') {
      setDeploymentStatus({
        state: 'deployed',
        deploymentId: restoredDeployment.deploymentId,
        message: 'Restored from previous deployment',
        runtimeId: restoredDeployment.runtimeId,
        endpoint: restoredDeployment.endpoint,
        runtimeProtocol: restoredDeployment.runtimeProtocol || 'HTTP',
        gatewayUrl: restoredDeployment.gatewayUrl,
      });
      setActiveTab(restoredDeployment.runtimeProtocol === 'MCP' ? 'tools' : 'chat');
    }
  }, [restoredDeployment]); // eslint-disable-line react-hooks/exhaustive-deps

  const pythonExportBlockedReason =
    Object.keys(resourceTagState.tags).length || resourceTagState.profileName
      ? PYTHON_EXPORT_GOVERNANCE_MESSAGE
      : null;

  const handleExportPython = useCallback(async () => {
    if (!config || !nodeId || !governanceReady || validationBlockedReason) return;
    // Refused here as well as by the disabled button: nothing is sent, and the
    // reason is rendered next to the button rather than the settings vanishing.
    if (pythonExportBlockedReason) return;
    setExportError(null);
    setIsExportingPython(true);
    try {
      const fullConfig = runtimeConfigForRequest(config, templateId);
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
          // Sent, not dropped: a standalone project creates no AWS resources, so the
          // route refuses governance tags with a 400 whose detail is shown below.
          resourceTags: Object.keys(resourceTagState.tags).length ? resourceTagState.tags : undefined,
          tagProfile: resourceTagState.profileName || undefined,
        }),
      });
      if (!response.ok) {
        throw new Error(await responseDetail(response, `Python export failed (${response.status})`));
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
      setExportError({ kind: 'python', message });
    } finally {
      setIsExportingPython(false);
    }
  }, [config, nodeId, governanceReady, validationBlockedReason, connectedTools, gatewayConfigForDeploy, externalMcpServers, gatewayTools, templateId, customTools, connectors, memoryConfig, evaluationConfig, policyConfig, guardrailsConfig, mcpServerConfig, knowledgeBaseConfig, observabilityConfig, a2aConfig, identityConfig, resourceTagState, pythonExportBlockedReason]);

  const handleDownloadCfn = useCallback(async () => {
    if (!config || !nodeId || resourceNamingState.error || !governanceReady || validationBlockedReason) return;
    setExportError(null);
    setIsDownloadingCfn(true);
    try {
      const fullConfig = runtimeConfigForRequest(config, templateId);
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
          policyRevision: resourceTagState.policyRevision || undefined,
          tagProfileUpdatedAt: resourceTagState.profileName
            ? resourceTagState.profileUpdatedAt || undefined
            : undefined,
          namingProfile: resourceNamingState.profile || undefined,
          dataRetentionPolicy: cfnDataRetentionPolicy,
        }),
      });
      if (!response.ok) {
        // A 4xx here is the generator refusing a canvas it cannot export
        // faithfully (e.g. a LiteLLM gateway), and its `detail` names the
        // workaround. Surface it verbatim — the bare status code this used to
        // throw told the user nothing about how to proceed. Shared with the
        // Python export so a 422 validation list reads the same on both.
        throw new Error(await responseDetail(response, `Template generation failed (${response.status})`));
      }
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
      setExportError({ kind: 'cfn', message });
    } finally {
      setIsDownloadingCfn(false);
    }
  }, [config, nodeId, governanceReady, validationBlockedReason, connectedTools, gatewayConfigForDeploy, externalMcpServers, gatewayTools, templateId, customTools, connectors, memoryConfig, evaluationConfig, policyConfig, guardrailsConfig, mcpServerConfig, knowledgeBaseConfig, identityConfig, a2aConfig, observabilityConfig, resourceTagState, resourceNamingState, cfnDataRetentionPolicy]);

  const handlePublishToRegistry = useCallback(async () => {
    if (!config || !hasScope('registry:write') || !governanceReady) return;
    setIsPublishing(true);
    setPublishMsg(null);
    try {
      const {
        nodes,
        edges,
        viewport,
        governance,
      } = useWorkflowStore.getState();
      const display = config.name || 'Untitled Agent';
      await publishToRegistryApi({
        display_name: display,
        description: config.systemPrompt?.slice(0, 280) || `Deployed agent ${display}`,
        visibility: 'org',
        canvas_snapshot: createRegistryCanvasSnapshot(
          display,
          nodes,
          edges,
          viewport,
          governance,
        ),
        source_runtime_name: config.name || undefined,
      });
      setPublishMsg({ kind: 'ok', text: `Published "${display}" to the registry.` });
    } catch (error) {
      const text = error instanceof Error ? error.message : 'Publish failed';
      setPublishMsg({ kind: 'err', text });
    } finally {
      setIsPublishing(false);
    }
  }, [config, governanceReady, hasScope]);

  const handleTest = useCallback(async () => {
    if (deploymentStatus.runtimeProtocol === 'MCP') return;
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
        // authFetch, NOT bare fetch: the endpoint requires a bearer token like
        // every other one, and this was the only call in the file that skipped it.
        // The failure was invisible — a 401 returns false below, which silently
        // falls through to the non-streaming path, so chat kept working and
        // streaming was simply never reachable in a deployed UI. Measured live:
        // POST /api/test-runtime-stream -> 401 on every message.
        const response = await authFetch('/api/test-runtime-stream', {
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
  }, [deploymentStatus.endpoint, deploymentStatus.simulated, deploymentStatus.runtimeId, deploymentStatus.runtimeProtocol, testInput, sessionId, conversationHistory]);

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
    const deleteTargetId = deploymentStatus.runtimeId || deploymentStatus.deploymentId;
    if (!deleteTargetId) return;
    const isPartialDeployment = !deploymentStatus.runtimeId;
    setShowDeleteConfirm(false);
    setDeleteError(null);
    setIsDeleting(true);
    try {
      const response = await authFetch(
        `/api/runtime/${encodeURIComponent(deleteTargetId)}`,
        { method: 'DELETE' },
      );
      const result = await response.json().catch(() => ({})) as {
        success?: boolean;
        message?: string;
        detail?: string;
      };
      if (!response.ok || !result.success) {
        throw new Error(
          result.message
          || result.detail
          || `${isPartialDeployment ? 'Deployment cleanup' : 'Runtime deletion'} failed (${response.status})`,
        );
      }
      if (result.success) {
        setDeploymentStatus({ state: 'idle' });
        setTestResult(null);
        setActiveTab('deploy');
      }
    } catch (error) {
      setDeleteError(
        error instanceof Error
          ? error.message
          : `${isPartialDeployment ? 'Deployment cleanup' : 'Runtime deletion'} failed`,
      );
    } finally {
      setIsDeleting(false);
    }
  }, [
    deploymentStatus.deploymentId,
    deploymentStatus.runtimeId,
    setDeploymentStatus,
  ]);

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
        ref={panelRef}
        className="fixed right-0 top-0 bottom-0 w-[420px] bg-white z-50 flex flex-col overflow-hidden border-l border-[#e9ebed]"
        style={{ boxShadow: 'var(--elevation-4)' }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="deploy-panel-title"
        tabIndex={-1}
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
              <h2 id="deploy-panel-title" className="font-semibold text-white text-sm">Deploy &amp; Test</h2>
              <p className="text-[11px] text-white/50">{deploymentMode === 'harness' ? 'AgentCore Harness' : 'AgentCore Runtime'}</p>
            </div>
          </div>
          <button type="button" onClick={onClose} aria-label="Close the deploy and test panel" className="p-1.5 rounded-md hover:bg-white/10 transition-colors">
            <svg className="w-4 h-4 text-white/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div
          className="flex border-b border-[#e9ebed]"
          role="tablist"
          aria-label="Deploy and test views"
        >
          {visibleTabs.map((tab) => {
            const isActive = activeTab === tab.id;
            const isDisabled = (
              (tab.id === 'chat' || tab.id === 'tools')
              && deploymentStatus.state !== 'deployed'
            );
            return (
              <button
                key={tab.id}
                id={`deploy-panel-tab-${tab.id}`}
                type="button"
                role="tab"
                aria-selected={isActive}
                aria-controls={`deploy-panel-tabpanel-${tab.id}`}
                tabIndex={isActive ? 0 : -1}
                disabled={isDisabled}
                onClick={() => setActiveTab(tab.id)}
                onKeyDown={(event) => handleTabKeyDown(event, tab.id)}
                className={`flex-1 py-2.5 text-sm font-medium transition-colors relative ${
                  isDisabled ? 'cursor-not-allowed' : ''
                }`}
                style={{
                  color: isActive
                    ? 'var(--color-aws-blue)'
                    : isDisabled
                      ? 'var(--color-text-placeholder)'
                      : 'var(--color-text-secondary)',
                }}
              >
                {tab.label}
                {(tab.id === 'chat' || tab.id === 'tools') && deploymentStatus.state === 'deployed' && (
                  <span
                    className="ml-1.5 w-1.5 h-1.5 bg-emerald-500 rounded-full inline-block"
                    aria-hidden="true"
                  />
                )}
                {isActive && (
                  <span
                    className="absolute bottom-0 left-0 right-0 h-0.5"
                    style={{ background: 'var(--color-aws-blue)' }}
                    aria-hidden="true"
                  />
                )}
              </button>
            );
          })}
        </div>

        {deleteError && (
          <div
            role="alert"
            className="mx-4 mt-3 flex-shrink-0 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
          >
            {deploymentStatus.runtimeId ? 'Runtime deletion' : 'Deployment cleanup'} failed: {deleteError}
          </div>
        )}

        <div
          className={`flex-1 min-h-0 ${
            activeTab === 'chat' || activeTab === 'tools'
              ? 'flex flex-col'
              : 'overflow-y-auto'
          }`}
          role="tabpanel"
          id={`deploy-panel-tabpanel-${activeTab}`}
          aria-labelledby={`deploy-panel-tab-${activeTab}`}
          tabIndex={0}
        >
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

              {(deploymentStatus.state === 'idle' || deploymentStatus.state === 'deployed') && (
                <DeploymentTargetFields
                  value={deploymentTarget}
                  onChange={setDeploymentTarget}
                />
              )}

              {(deploymentStatus.state === 'idle' || deploymentStatus.state === 'deployed') && (
                <ResourceTagFields
                  value={resourceTagState}
                  onChange={setResourceTagState}
                  onStatusChange={setTagGovernanceStatus}
                />
              )}

              {(deploymentStatus.state === 'idle' || deploymentStatus.state === 'deployed') && (
                <ResourceNamingFields
                  value={resourceNamingState}
                  onChange={handleResourceNamingChange}
                />
              )}

              {deploymentStatus.state === 'deploying' && (
                <DeployProgress message={deploymentStatus.message || 'Deploying...'} />
              )}

              {deploymentStatus.state === 'deployed' && (
                <DeployResult
                  message={deploymentStatus.message || 'Deployed successfully!'}
                  simulated={deploymentStatus.simulated}
                  runtimeId={deploymentStatus.runtimeId}
                  runtimeProtocol={deploymentStatus.runtimeProtocol}
                  endpoint={deploymentStatus.endpoint}
                  gatewayUrl={deploymentStatus.gatewayUrl}
                  onRedeploy={() => setDeploymentStatus({ state: 'idle' })}
                  onDelete={() => {
                    setDeleteError(null);
                    setShowDeleteConfirm(true);
                  }}
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
                  {deploymentStatus.deploymentId && !deploymentStatus.runtimeId && (
                    <button
                      type="button"
                      onClick={() => {
                        setDeleteError(null);
                        setShowDeleteConfirm(true);
                      }}
                      disabled={isDeleting}
                      className="w-full py-2.5 px-4 border border-red-300 rounded-xl text-red-600 hover:bg-red-50 transition-colors text-sm flex items-center justify-center gap-2 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {isDeleting ? 'Cleaning up...' : 'Clean up partial deployment'}
                    </button>
                  )}
                </div>
              )}

              {(deploymentStatus.state === 'idle' || deploymentStatus.state === 'deployed') && (
                <fieldset
                  className="rounded-lg border border-[#d5dbdb] bg-[#fafafa] p-3.5"
                  aria-describedby="cfn-data-retention-help"
                >
                  <legend className="px-1 text-sm font-semibold text-[#232f3e]">
                    CloudFormation data retention
                  </legend>
                  <p id="cfn-data-retention-help" className="mb-3 text-xs text-[#5f6b7a]">
                    Applies only to the downloaded CloudFormation bundle when its stack is deleted.
                  </p>
                  <div className="space-y-2">
                    <label className="flex cursor-pointer items-start gap-2.5 rounded-md p-2 hover:bg-white">
                      <input
                        type="radio"
                        name="cfn-data-retention-policy"
                        value="Retain"
                        checked={cfnDataRetentionPolicy === 'Retain'}
                        onChange={() => setCfnDataRetentionPolicy('Retain')}
                        aria-describedby="cfn-data-retention-help"
                        className="mt-0.5"
                      />
                      <span>
                        <span className="block text-sm font-medium text-[#232f3e]">
                          Retain (recommended)
                        </span>
                        <span className="block text-xs text-[#5f6b7a]">
                          Preserve stateful resources when the exported stack is deleted.
                        </span>
                      </span>
                    </label>
                    <label className="flex cursor-pointer items-start gap-2.5 rounded-md p-2 hover:bg-white">
                      <input
                        type="radio"
                        name="cfn-data-retention-policy"
                        value="Delete"
                        checked={cfnDataRetentionPolicy === 'Delete'}
                        onChange={() => setCfnDataRetentionPolicy('Delete')}
                        aria-describedby={
                          cfnDataRetentionPolicy === 'Delete'
                            ? 'cfn-data-retention-help cfn-delete-retention-warning'
                            : 'cfn-data-retention-help'
                        }
                        className="mt-0.5"
                      />
                      <span>
                        <span className="block text-sm font-medium text-[#232f3e]">
                          Delete with stack
                        </span>
                        <span className="block text-xs text-[#5f6b7a]">
                          Request deletion of stack-owned data when the exported stack is deleted.
                        </span>
                      </span>
                    </label>
                  </div>
                  {cfnDataRetentionPolicy === 'Delete' && (
                    <p
                      id="cfn-delete-retention-warning"
                      role="status"
                      className="mt-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900"
                    >
                      Use Delete only for ephemeral or test environments. Stack deletion can
                      permanently remove stack-owned data.
                    </p>
                  )}
                </fieldset>
              )}

              <DeployActions
                canDeploy={!!config && governanceReady && !validationBlockedReason}
                canDownloadCfn={!!config && governanceReady && !resourceNamingState.error && !validationBlockedReason}
                cfnDownloadBlockedReason={
                  !config
                    ? 'Add a deployable runtime to the canvas before downloading'
                    : validationBlockedReason
                      ? validationBlockedReason
                    : !governanceReady
                      ? (tagGovernanceStatus.state === 'ready'
                        ? 'Supply every required tag before downloading'
                        : tagGovernanceStatus.message)
                      : resourceNamingState.error
                        ? 'Fix the CloudFormation naming profile before downloading'
                        : null
                }
                canPublish={hasScope('registry:write') && governanceReady}
                state={deploymentStatus.state}
                isDownloadingCfn={isDownloadingCfn}
                isExportingPython={isExportingPython}
                isPublishing={isPublishing}
                publishMsg={publishMsg}
                onDownloadCfn={handleDownloadCfn}
                onExportPython={handleExportPython}
                onPublish={handlePublishToRegistry}
                pythonExportBlockedReason={pythonExportBlockedReason}
                cfnExportError={exportError?.kind === 'cfn' ? exportError.message : null}
                pythonExportError={exportError?.kind === 'python' ? exportError.message : null}
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
                  <button onClick={handleNewSession} className="text-xs text-blue-700 hover:text-blue-800 font-medium">
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
          {activeTab === 'tools' && deploymentStatus.state === 'deployed' && (
            <div className="flex min-h-0 flex-1 flex-col">
              <div className="flex flex-shrink-0 items-center justify-between border-b border-[#e9ebed] bg-[#fafafa] px-4 py-2.5">
                <span className="text-xs text-[#5f6b7a]">MCP protocol runtime</span>
                <button
                  type="button"
                  onClick={() => setShowDeleteConfirm(true)}
                  disabled={isDeleting}
                  className="text-xs font-medium text-red-500 hover:text-red-700 disabled:opacity-60"
                >
                  {isDeleting ? 'Deleting...' : 'Delete'}
                </button>
              </div>
              <McpToolsPanel deploymentId={deploymentStatus.deploymentId} />
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
          {activeTab === 'triggers' && !isMcpProtocol && (
            <TriggersPanel runtimeName={config?.name ?? null} refreshKey={versionsRefreshKey} />
          )}
        </div>

        {activeTab !== 'chat' && activeTab !== 'tools' && (
          <div className="border-t border-[#e9ebed] bg-[#fafafa] flex-shrink-0 p-3.5 space-y-2">
            {activeTab === 'deploy' && (deploymentStatus.state === 'idle' || deploymentStatus.state === 'error') && (
              <button
                onClick={handleDeploy}
                disabled={!config || !governanceReady || !!validationBlockedReason}
                title={validationBlockedReason ?? undefined}
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
            <div className="flex items-center justify-center gap-1.5 text-[10px]" style={{ color: 'var(--color-text-secondary)' }}>
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
        title={deploymentStatus.runtimeId ? 'Delete Runtime' : 'Clean Up Partial Deployment'}
        message={
          deploymentStatus.runtimeId
            ? 'Are you sure you want to delete this runtime from AWS? This action cannot be undone.'
            : 'Clean up the AWS resources created before this deployment failed? Only resources whose ownership can be verified will be removed.'
        }
        confirmLabel={deploymentStatus.runtimeId ? 'Delete' : 'Clean up'}
        cancelLabel="Cancel"
        variant="danger"
        onConfirm={handleDelete}
        onCancel={() => setShowDeleteConfirm(false)}
      />
    </>
  );
}

export default DeployPanel;
