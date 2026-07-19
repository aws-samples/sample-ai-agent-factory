/**
 * Deploy pipeline step mappings.
 *
 * CRITICAL: These constants mirror the AWS Step Functions state machine
 * deployed by backend/infra/lib/backend-infra-stack.ts (the DeploymentPipeline
 * construct). If the backend's SFN step names change, these MUST be updated
 * in lock-step or the UI's live deploy progress visualization will break.
 *
 * The backend pipeline order is defined in:
 *   backend/infra/lib/backend-infra-stack.ts
 *   → DeploymentPipeline construct
 *   → Step Functions states
 *
 * When the backend adds/removes/renames steps, search for STEP_ORDER and
 * STEP_TO_NODE_TYPE in this file and update both.
 */

import type { AgentCoreComponentType } from '../../types/workflow';

/**
 * Maps Step Functions step names to canvas node types.
 * Used to highlight which canvas node is currently being deployed.
 *
 * null = UI-only step (status_update) with no canvas representation
 */
export const STEP_TO_NODE_TYPE: Record<string, AgentCoreComponentType | null> = {
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
  status_update: null, // UI-only, no canvas node
};

/**
 * Ordered list of all Step Functions states in deployment order.
 * Used to compute completion percentage and mark prior steps as complete.
 */
export const STEP_ORDER = [
  'validate',
  'guardrails',
  'mcp_server',
  'knowledge_base',
  'gateway',
  'memory',
  'policy',
  'codegen',
  'iam',
  'runtime_configure',
  'runtime_launch',
  'evaluation',
  'auth',
  'status_update',
];

/**
 * Human-readable labels for each step (shown in the deploying progress UI).
 */
export const STEP_LABELS: Record<string, string> = {
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
  guardrails: 'Deploying guardrails...',
};
