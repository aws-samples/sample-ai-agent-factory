/**
 * Custom node component for AgentCore components.
 * Renders different visual styles based on component type.
 * Displays validation indicators with tooltips.
 * Runtime nodes have input, output, and tool handles.
 * Requirements: 8.1, 8.2
 *
 * Redesign: layered depth (resting → hover-lift → selected-glow) via motion,
 * a tinted icon chip, canonical CSS-var accent colors (single source of truth,
 * shared with the minimap), and a spring "drop-in" enter animation. All node
 * data logic, handle ids, and data-testids are preserved.
 */

import { memo, useState } from 'react';
import { m } from 'motion/react';
import { Handle, Position, type NodeProps, type Node } from '@xyflow/react';
import type { AgentCoreNodeData } from '../../store/workflowStore';
import type { RuntimeConfiguration, ToolConfiguration } from '../../types/components';
import { COMPONENT_ICONS } from '../icons/componentIcons';
import { nodeEnter, spring } from '../../lib/motion';
import { accentFor } from './nodeColors';

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

  const accent = accentFor(data.componentType);
  const validation = VALIDATION_INDICATORS[data.validationStatus] || VALIDATION_INDICATORS.pending;
  const hasValidationIssues = data.validationStatus === 'error' || data.validationStatus === 'warning';
  const isRuntime = data.componentType === 'runtime';
  const isTool = data.componentType === 'tool';
  const runtimeConfig = data.configuration as RuntimeConfiguration | undefined;
  const toolConfig = data.configuration as ToolConfiguration | undefined;
  const execState = data.executionState as string | undefined;

  const isError = data.validationStatus === 'error';
  const isWarning = data.validationStatus === 'warning';

  // Border reflects validation state, else accent (bright when selected).
  const borderColor = isError
    ? 'var(--neon-red)'
    : isWarning
    ? 'var(--neon-amber)'
    : selected
    ? accent
    : `color-mix(in srgb, ${accent} 30%, var(--color-border))`;

  // Neon glow: subtle ambient halo at rest, bright ring + bloom when selected.
  const restGlow = `var(--elevation-2), 0 0 18px -6px color-mix(in srgb, ${accent} 60%, transparent)`;
  const selectedGlow = `var(--elevation-3), 0 0 0 1px ${accent}, 0 0 24px -2px color-mix(in srgb, ${accent} 70%, transparent)`;

  return (
    <m.div
      className="relative min-w-[184px] group"
      variants={nodeEnter}
      initial="hidden"
      animate="visible"
      exit="exit"
      transition={spring.snappy}
    >
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
          no-darkmap relative cursor-pointer overflow-hidden
          ${execState === 'running' ? 'execution-running' : ''}
          transition-[box-shadow,border-color] duration-200
        `}
        style={{
          borderRadius: 'var(--radius-surface)',
          border: `1px solid ${borderColor}`,
          background: 'var(--node-card-bg)',
          boxShadow: selected ? selectedGlow : restGlow,
          transitionTimingFunction: 'var(--ease-out-quint)',
        }}
        data-testid={`node-${data.componentType}`}
      >
      {/* Neon accent bar at top with bloom + shimmer sweep */}
      <div className="relative h-[3px] overflow-hidden" style={{ background: accent, boxShadow: `0 0 12px ${accent}` }}>
        <div
          className="absolute inset-y-0 w-1/3"
          style={{
            background: 'linear-gradient(90deg, transparent, rgba(255,255,255,0.85), transparent)',
            animation: 'u-shimmer 4.5s ease-in-out infinite',
          }}
        />
      </div>
      {/* Accent wash so the card glows in its type color */}
      <div
        className="pointer-events-none absolute inset-0"
        style={{ background: `linear-gradient(180deg, color-mix(in srgb, ${accent} 14%, transparent), transparent 55%)` }}
      />

      {/* Input Handle - Left side */}
      <Handle
        type="target"
        position={Position.Left}
        id="input"
        className="!w-2.5 !h-2.5 !border-2 !border-[#0b1220] !bg-[#0972d3] transition-transform hover:!scale-125"
        style={{ top: '50%' }}
      />

      {/* Tool Handles - Top (for Runtime nodes) */}
      {isRuntime && (
        <Handle
          type="target"
          position={Position.Top}
          id="tools"
          className="!w-2.5 !h-2.5 !border-2 !border-[#0b1220] !bg-[#037f0c] transition-transform hover:!scale-125"
          style={{ left: '50%' }}
        />
      )}

      {/* Node Content */}
      <div className="relative px-3.5 py-2.5">
        <div className="flex items-center gap-2.5">
          {/* Neon icon chip with glow */}
          <div
            className="flex-shrink-0 flex items-center justify-center w-8 h-8 rounded-lg"
            style={{
              color: accent,
              background: `color-mix(in srgb, ${accent} 16%, transparent)`,
              boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${accent} 45%, transparent), 0 0 12px -2px color-mix(in srgb, ${accent} 70%, transparent)`,
            }}
          >
            {COMPONENT_ICONS[data.componentType]}
          </div>
          <div className="flex-1 min-w-0">
            <div className="no-darkmap font-semibold text-[13px] truncate tracking-tight leading-tight" style={{ color: 'var(--color-text-primary)' }}>
              {data.label || data.componentType}
            </div>
            <div className="no-darkmap text-[11px] leading-tight mt-0.5" style={{ color: 'var(--color-text-secondary)' }}>
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
              <div className="absolute z-50 right-0 top-full mt-1 p-2 bg-white rounded-md border border-[#e9ebed] min-w-[200px]" style={{ boxShadow: 'var(--elevation-3)' }}>
                <ValidationTooltip errors={data.validationErrors} warnings={data.validationWarnings} status={data.validationStatus} />
              </div>
            )}
          </div>
        </div>

        {/* Framework badge for Runtime */}
        {isRuntime && runtimeConfig?.framework && (
          <div
            className="mt-1.5 text-[10px] px-2 py-0.5 rounded font-medium inline-block"
            style={{ color: accent, background: `color-mix(in srgb, ${accent} 10%, transparent)` }}
          >
            {runtimeConfig.framework.replace(/_/g, ' ')}
          </div>
        )}

        {/* Tool type badge */}
        {isTool && toolConfig?.toolId && (
          <div
            className="mt-1.5 text-[10px] px-2 py-0.5 rounded font-medium inline-block"
            style={{ color: accent, background: `color-mix(in srgb, ${accent} 10%, transparent)` }}
          >
            gateway tool
          </div>
        )}
      </div>

      {/* Output Handle - Right side */}
      <Handle
        type="source"
        position={Position.Right}
        id="output"
        className="!w-2.5 !h-2.5 !border-2 !border-[#0b1220] !bg-[#ff9900] transition-transform hover:!scale-125"
        style={{ top: '50%' }}
      />

      {/* Double-click hint — more discoverable */}
      <div className="absolute -bottom-6 left-0 right-0 text-center text-[10px] text-[#8d99a8] opacity-0 group-hover:opacity-100 transition-opacity duration-200 pointer-events-none">
        Double-click to configure
      </div>
      </div>
    </m.div>
  );
}

export default memo(AgentCoreNode);
