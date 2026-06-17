/**
 * Custom node component for AgentCore components.
 * Renders different visual styles based on component type.
 * Displays validation indicators with tooltips.
 * Runtime nodes have input, output, and tool handles.
 * Requirements: 8.1, 8.2
 */

import { memo, useState } from 'react';
import { Handle, Position, type NodeProps, type Node } from '@xyflow/react';
import type { AgentCoreNodeData } from '../../store/workflowStore';
import type { RuntimeConfiguration, ToolConfiguration } from '../../types/components';

// ============================================================================
// Component Type Colors
// ============================================================================

const COMPONENT_COLORS: Record<string, { bg: string; border: string; accent: string; icon: string }> = {
  runtime: { bg: 'bg-white', border: 'border-[#0972d3]', accent: 'bg-[#0972d3]', icon: '🤖' },
  gateway: { bg: 'bg-white', border: 'border-[#037f0c]', accent: 'bg-[#037f0c]', icon: '🔌' },
  memory: { bg: 'bg-white', border: 'border-[#0972d3]', accent: 'bg-[#0972d3]', icon: '🧠' },
  code_interpreter: { bg: 'bg-white', border: 'border-[#d45b07]', accent: 'bg-[#d45b07]', icon: '💻' },
  browser: { bg: 'bg-white', border: 'border-[#5b48d3]', accent: 'bg-[#5b48d3]', icon: '🌐' },
  observability: { bg: 'bg-white', border: 'border-[#c41367]', accent: 'bg-[#c41367]', icon: '📊' },
  identity: { bg: 'bg-white', border: 'border-[#7d2bd0]', accent: 'bg-[#7d2bd0]', icon: '🔑' },
  evaluation: { bg: 'bg-white', border: 'border-[#037f0c]', accent: 'bg-[#037f0c]', icon: '✅' },
  policy: { bg: 'bg-white', border: 'border-[#d91515]', accent: 'bg-[#d91515]', icon: '🛡️' },
  guardrails: { bg: 'bg-white', border: 'border-[#d91515]', accent: 'bg-[#d91515]', icon: '🚧' },
  a2a: { bg: 'bg-white', border: 'border-[#067a6e]', accent: 'bg-[#067a6e]', icon: '🔄' },
  tool: { bg: 'bg-white', border: 'border-[#d45b07]', accent: 'bg-[#d45b07]', icon: '🔧' },
};

// ============================================================================
// Validation Status Indicators
// ============================================================================

const VALIDATION_INDICATORS: Record<string, { color: string; bgColor: string; symbol: string }> = {
  valid: { color: 'text-green-600', bgColor: 'bg-green-100', symbol: '✓' },
  warning: { color: 'text-yellow-600', bgColor: 'bg-yellow-100', symbol: '⚠' },
  error: { color: 'text-red-600', bgColor: 'bg-red-100', symbol: '✗' },
  pending: { color: 'text-gray-400', bgColor: 'bg-gray-100', symbol: '○' },
};

// ============================================================================
// Validation Tooltip Component
// ============================================================================

interface ValidationTooltipProps {
  errors?: Array<{ message: string }>;
  warnings?: Array<{ message: string }>;
  status: string;
}

function ValidationTooltip({ errors = [], warnings = [], status }: ValidationTooltipProps) {
  if (errors.length === 0 && warnings.length === 0) {
    return <div className="text-xs text-gray-600">{status === 'valid' ? 'Configuration is valid' : 'Validation pending'}</div>;
  }

  return (
    <div className="text-xs max-w-xs">
      {errors.length > 0 && (
        <div className="mb-1">
          <div className="font-semibold text-red-600 mb-0.5">Errors:</div>
          <ul className="list-disc list-inside text-red-600">
            {errors.slice(0, 3).map((err, i) => <li key={i} className="truncate">{err.message}</li>)}
            {errors.length > 3 && <li className="text-gray-500">...and {errors.length - 3} more</li>}
          </ul>
        </div>
      )}
      {warnings.length > 0 && (
        <div>
          <div className="font-semibold text-yellow-600 mb-0.5">Warnings:</div>
          <ul className="list-disc list-inside text-yellow-600">
            {warnings.slice(0, 3).map((warn, i) => <li key={i} className="truncate">{warn.message}</li>)}
            {warnings.length > 3 && <li className="text-gray-500">...and {warnings.length - 3} more</li>}
          </ul>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// AgentCoreNode Component
// ============================================================================

type AgentCoreNodeProps = NodeProps<Node<AgentCoreNodeData>>;

function AgentCoreNode({ data, selected }: AgentCoreNodeProps) {
  const [showTooltip, setShowTooltip] = useState(false);

  // Defensive: ensure componentType is always a string
  if (!data?.componentType) {
    console.error('[AgentCoreNode] Missing componentType in node data:', JSON.stringify(data));
    return <div className="p-2 text-xs text-red-500 bg-red-50 rounded border border-red-200">Invalid node</div>;
  }

  const colors = COMPONENT_COLORS[data.componentType] || COMPONENT_COLORS.runtime;
  const validation = VALIDATION_INDICATORS[data.validationStatus] || VALIDATION_INDICATORS.pending;
  const hasValidationIssues = data.validationStatus === 'error' || data.validationStatus === 'warning';
  const isRuntime = data.componentType === 'runtime';
  const isTool = data.componentType === 'tool';
  const runtimeConfig = data.configuration as RuntimeConfiguration | undefined;
  const toolConfig = data.configuration as ToolConfiguration | undefined;
  const execState = data.executionState as string | undefined;

  return (
    <div className="relative min-w-[180px] group">
      {/* Execution state badge — outside overflow-hidden container so it's visible */}
      {execState === 'completed' && (
        <div className="absolute -top-1.5 -right-1.5 w-5 h-5 bg-green-500 rounded-full flex items-center justify-center shadow-sm z-10">
          <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={3}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
          </svg>
        </div>
      )}
      {execState === 'failed' && (
        <div className="absolute -top-1.5 -right-1.5 w-5 h-5 bg-red-500 rounded-full flex items-center justify-center shadow-sm z-10">
          <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={3}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </div>
      )}
      {execState === 'running' && (
        <div className="absolute -top-1.5 -right-1.5 w-5 h-5 bg-blue-500 rounded-full flex items-center justify-center shadow-sm z-10">
          <svg className="w-3 h-3 text-white animate-spin" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
        </div>
      )}

      <div
        className={`
          rounded-xl border cursor-pointer overflow-hidden
          ${colors.bg} ${colors.border}
          ${selected ? 'ring-2 ring-[#0972d3] ring-offset-1 shadow-md' : 'shadow-sm hover:shadow-md'}
          ${data.validationStatus === 'error' ? 'border-red-500' : ''}
          ${data.validationStatus === 'warning' ? 'border-amber-500' : ''}
          ${execState === 'running' ? 'execution-running' : ''}
          transition-all duration-200
        `}
        style={{
          boxShadow: selected ? 'var(--shadow-md)' : 'var(--shadow-sm)',
          transitionTimingFunction: 'var(--ease-out-quint)',
        }}
        data-testid={`node-${data.componentType}`}
      >
      {/* Color accent bar at top */}
      <div className={`h-1 ${colors.accent}`} />

      {/* Input Handle - Left side */}
      <Handle
        type="target"
        position={Position.Left}
        id="input"
        className="w-2.5 h-2.5 !bg-[#0972d3] border-2 border-white"
        style={{ top: '50%' }}
      />

      {/* Tool Handles - Top (for Runtime nodes) */}
      {isRuntime && (
        <Handle
          type="target"
          position={Position.Top}
          id="tools"
          className="w-2.5 h-2.5 !bg-[#037f0c] border-2 border-white"
          style={{ left: '50%' }}
        />
      )}

      {/* Node Content */}
      <div className="px-3.5 py-2.5">
        <div className="flex items-center gap-2.5">
          <span className="text-base leading-none">{colors.icon}</span>
          <div className="flex-1 min-w-0">
            <div className="font-semibold text-[#16191f] text-[13px] truncate tracking-tight leading-tight">
              {data.label || data.componentType}
            </div>
            <div className="text-[11px] text-[#5f6b7a] leading-tight mt-0.5">
              {isRuntime && runtimeConfig?.framework ? (
                <span className="capitalize">{runtimeConfig.framework.replace(/_/g, ' ')}</span>
              ) : isTool && toolConfig?.toolId ? (
                <span className="capitalize">{toolConfig.toolId.replace(/_/g, ' ')}</span>
              ) : (
                <span className="capitalize">{data.componentType.replace(/_/g, ' ')}</span>
              )}
            </div>
          </div>

          {/* Validation Indicator */}
          <div
            className="relative"
            onMouseEnter={() => setShowTooltip(true)}
            onMouseLeave={() => setShowTooltip(false)}
          >
            <span
              className={`text-xs cursor-help px-1.5 py-0.5 rounded ${validation.color} ${hasValidationIssues ? validation.bgColor : ''}`}
              data-testid={`validation-indicator-${data.validationStatus}`}
            >
              {validation.symbol}
            </span>

            {showTooltip && (
              <div className="absolute z-50 right-0 top-full mt-1 p-2 bg-white rounded-md shadow-lg border border-[#e9ebed] min-w-[200px]">
                <ValidationTooltip errors={data.validationErrors} warnings={data.validationWarnings} status={data.validationStatus} />
              </div>
            )}
          </div>
        </div>

        {/* Framework badge for Runtime */}
        {isRuntime && runtimeConfig?.framework && (
          <div className="mt-1.5 text-[10px] bg-[#0972d3]/10 text-[#0972d3] px-2 py-0.5 rounded font-medium inline-block">
            {runtimeConfig.framework.replace(/_/g, ' ')}
          </div>
        )}

        {/* Tool type badge */}
        {isTool && toolConfig?.toolId && (
          <div className="mt-1.5 text-[10px] bg-[#d45b07]/10 text-[#d45b07] px-2 py-0.5 rounded font-medium inline-block">
            gateway tool
          </div>
        )}
      </div>

      {/* Output Handle - Right side */}
      <Handle
        type="source"
        position={Position.Right}
        id="output"
        className="w-2.5 h-2.5 !bg-[#ff9900] border-2 border-white"
        style={{ top: '50%' }}
      />

      {/* Double-click hint — more discoverable */}
      <div className="absolute -bottom-6 left-0 right-0 text-center text-[10px] text-[#8d99a8] opacity-0 group-hover:opacity-100 transition-opacity duration-200 pointer-events-none">
        Double-click to configure
      </div>
      </div>
    </div>
  );
}

export default memo(AgentCoreNode);
