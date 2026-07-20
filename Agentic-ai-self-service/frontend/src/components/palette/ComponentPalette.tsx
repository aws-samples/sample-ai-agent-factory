/**
 * ComponentPalette - Sidebar containing draggable AgentCore component types.
 * Requirements: 12.1, 12.4
 */

import { useState, useCallback, useMemo } from 'react';
import type { AgentCoreComponentType } from '../../types/workflow';
import { FlowSidebar } from '../flow-sidebar';
import { COMPONENT_ICONS } from '../icons/componentIcons';
import { accentFor } from '../nodes/nodeColors';

// ============================================================================
// Palette Item Definition
// ============================================================================

export interface PaletteItem {
  type: AgentCoreComponentType;
  label: string;
  description: string;
  category: 'compute' | 'integration' | 'security' | 'tools' | 'connectors';
  toolId?: string;
  customIcon?: string; // For tool-specific icons like 🦆, 📄, etc.
}

// ============================================================================
// Component Definitions
// ============================================================================

// eslint-disable-next-line react-refresh/only-export-components
export const PALETTE_ITEMS: PaletteItem[] = [
  {
    type: 'runtime',
    label: 'AgentCore Runtime',
    description: 'Deploy and execute AI agents in serverless environments',
    category: 'compute',
  },
  {
    type: 'gateway',
    label: 'AgentCore Gateway',
    description: 'Convert APIs and services into MCP-compatible tools',
    category: 'integration',
  },
  {
    type: 'memory',
    label: 'AgentCore Memory',
    description: 'Persistent memory for agent conversations',
    category: 'compute',
  },
  {
    type: 'code_interpreter',
    label: 'Code Interpreter',
    description: 'Secure code execution sandbox for agents',
    category: 'compute',
  },
  {
    type: 'browser',
    label: 'Browser Tool',
    description: 'Cloud-based browser for web interactions',
    category: 'integration',
  },
  {
    type: 'observability',
    label: 'Observability',
    description: 'OpenTelemetry tracing and monitoring',
    category: 'integration',
  },
  {
    type: 'identity',
    label: 'AgentCore Identity',
    description: 'Manage agent credentials for external resources',
    category: 'security',
  },
  {
    type: 'evaluation',
    label: 'AgentCore Evaluations',
    description: 'Quality assessment with built-in and custom evaluators',
    category: 'compute',
  },
  {
    type: 'policy',
    label: 'AgentCore Policy',
    description: 'Cedar-based fine-grained access control for tool invocations',
    category: 'security',
  },
  {
    type: 'guardrails',
    label: 'Bedrock Guardrails',
    description: 'Content filtering, PII detection, topic blocking, and prompt attack defense',
    category: 'security',
  },
  {
    type: 'a2a',
    label: 'Agent-to-Agent (A2A)',
    description: 'Multi-agent orchestration and communication patterns',
    category: 'integration',
  },
  // ── Tools ──────────────────────────────────────────────────────────────
  {
    type: 'tool',
    label: 'DuckDuckGo Search',
    description: 'Web search via DuckDuckGo API - returns top results',
    customIcon: '🦆',
    category: 'tools',
    toolId: 'duckduckgo_search',
  },
  {
    type: 'tool',
    label: 'Web Page Fetcher',
    description: 'Fetch and extract content from web pages by URL',
    customIcon: '📄',
    category: 'tools',
    toolId: 'web_page_fetcher',
  },
  {
    type: 'tool',
    label: 'Wikipedia Search',
    description: 'Search and retrieve Wikipedia article summaries',
    customIcon: '📚',
    category: 'tools',
    toolId: 'wikipedia_search',
  },
  {
    type: 'tool',
    label: 'Weather API',
    description: 'Get current weather data for any location',
    customIcon: '🌤️',
    category: 'tools',
    toolId: 'weather_api',
  },
  // Customer Support Tools (from 05-blueprints/customer-support-agent-with-agentcore)
  {
    type: 'tool',
    label: 'Get Order',
    description: 'Look up order details by order ID - items, status, dates, total',
    customIcon: '📦',
    category: 'tools',
    toolId: 'get_order',
  },
  {
    type: 'tool',
    label: 'Get Customer',
    description: 'Look up customer info and order summary by customer ID',
    customIcon: '👤',
    category: 'tools',
    toolId: 'get_customer',
  },
  {
    type: 'tool',
    label: 'List Orders',
    description: 'List orders for a customer sorted by date',
    customIcon: '📋',
    category: 'tools',
    toolId: 'list_orders',
  },
  {
    type: 'tool',
    label: 'Process Refund',
    description: 'Process a refund for an order with amount validation',
    customIcon: '💰',
    category: 'tools',
    toolId: 'process_refund',
  },
  // Knowledge Base Tool (RAG)
  {
    type: 'tool',
    label: 'Knowledge Base',
    description: 'RAG-powered Q&A using Amazon Bedrock Knowledge Bases',
    customIcon: '📖',
    category: 'tools',
    toolId: 'knowledge_base',
  },
  // ── Connectors (Phase A — SaaS) ─────────────────────────────────────────
  // Connector nodes reuse the `tool` component type. Their toolId is prefixed
  // with "connector:" so App.tsx dispatches the ConnectorConfigModal and the
  // deploy extraction routes them into the `connectors` payload (not gatewayTools).
  {
    type: 'tool',
    label: 'Jira',
    description: 'Atlassian Jira — create and search issues via the gateway',
    customIcon: '🟦',
    category: 'connectors',
    toolId: 'connector:jira',
  },
  {
    type: 'tool',
    label: 'Asana',
    description: 'Asana tasks and projects (API key only)',
    customIcon: '🅰️',
    category: 'connectors',
    toolId: 'connector:asana',
  },
  {
    type: 'tool',
    label: 'Slack',
    description: 'Post messages and read channels via Slack',
    customIcon: '💬',
    category: 'connectors',
    toolId: 'connector:slack',
  },
  {
    type: 'tool',
    label: 'GitHub',
    description: 'Issues, pull requests, and repos via GitHub',
    customIcon: '🐙',
    category: 'connectors',
    toolId: 'connector:github',
  },
  {
    type: 'tool',
    label: 'Salesforce',
    description: 'Leads, accounts, and SOQL via Salesforce',
    customIcon: '☁️',
    category: 'connectors',
    toolId: 'connector:salesforce',
  },
  {
    type: 'tool',
    label: 'OpenAPI / MCP Connector',
    description: 'Bring any OpenAPI spec or MCP server as a gateway target',
    customIcon: '🧩',
    category: 'connectors',
    toolId: 'connector:generic_openapi',
  },
];

// ============================================================================
// Category Definitions
// ============================================================================

const CATEGORIES = [
  { id: 'compute', label: 'Compute', icon: '⚡' },
  { id: 'integration', label: 'Integration', icon: '🔗' },
  { id: 'security', label: 'Security', icon: '🔒' },
  { id: 'tools', label: 'Tools', icon: '🧰' },
  { id: 'connectors', label: 'Connectors', icon: '🧩' },
] as const;

// ============================================================================
// Props Interface
// ============================================================================

export interface ComponentPaletteProps {
  onDragStart?: (componentType: AgentCoreComponentType, event: React.DragEvent) => void;
  onDragEnd?: () => void;
  collapsed?: boolean;
  onToggleCollapse?: () => void;
  searchQuery?: string;
  onSearchChange?: (query: string) => void;
  onOpenTemplates?: () => void;
  onOpenToolGenerator?: () => void;
  onOpenAgentGenerator?: () => void;
  onOpenRegistry?: () => void;
}

// ============================================================================
// PaletteItemComponent
// ============================================================================

interface PaletteItemComponentProps {
  item: PaletteItem;
  onDragStart?: (componentType: AgentCoreComponentType, event: React.DragEvent) => void;
  onDragEnd?: () => void;
}

function PaletteItemComponent({ item, onDragStart, onDragEnd }: PaletteItemComponentProps) {
  const handleDragStart = useCallback(
    (event: React.DragEvent) => {
      event.dataTransfer.setData('application/agentcore-component', item.type);
      if (item.toolId) {
        event.dataTransfer.setData('application/agentcore-tool-id', item.toolId);
      }
      event.dataTransfer.effectAllowed = 'copy';
      onDragStart?.(item.type, event);
    },
    [item.type, item.toolId, onDragStart]
  );

  const accent = accentFor(item.type);

  // Use custom emoji icon for tools/connectors, or component icon for core types
  const iconDisplay = item.customIcon ? (
    <span className="text-base">{item.customIcon}</span>
  ) : (
    <div style={{ color: accent }}>
      {COMPONENT_ICONS[item.type]}
    </div>
  );

  // NOTE: this element uses the NATIVE HTML5 drag API (dataTransfer) to drop
  // nodes on the canvas. framer-motion's <m.div> overrides onDragStart/onDragEnd
  // with its OWN pointer-gesture system (incompatible signature), so the
  // draggable root MUST stay a plain <div>. Hover-lift is done via CSS transform
  // (the -translate-y on hover) to avoid the gesture collision entirely.
  return (
    <div
      draggable
      onDragStart={handleDragStart}
      onDragEnd={onDragEnd}
      className="no-darkmap flex items-start gap-2.5 p-2.5 rounded-lg cursor-grab active:cursor-grabbing transition-all duration-150 group hover:-translate-y-0.5 active:translate-y-0"
      style={{
        background: 'var(--color-surface)',
        border: '1px solid var(--color-border)',
        transitionTimingFunction: 'var(--ease-out-quint)',
        // @ts-expect-error CSS custom prop for hover glow
        '--pi-accent': accent,
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = `color-mix(in srgb, ${accent} 55%, transparent)`;
        e.currentTarget.style.boxShadow = `0 0 18px -6px ${accent}, var(--elevation-2)`;
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = 'var(--color-border)';
        e.currentTarget.style.boxShadow = 'none';
      }}
      data-testid={`palette-item-${item.type}`}
      data-component-type={item.type}
    >
      <div
        className="no-darkmap w-8 h-8 rounded-md flex items-center justify-center flex-shrink-0 transition-colors"
        style={{ background: `color-mix(in srgb, ${accent} 14%, transparent)`, boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${accent} 30%, transparent)` }}
      >
        {iconDisplay}
      </div>
      <div className="flex-1 min-w-0">
        <div className="no-darkmap font-medium text-[13px] transition-colors" style={{ color: 'var(--color-text-primary)' }}>{item.label}</div>
        <div className="no-darkmap text-[11px] mt-0.5 line-clamp-2 leading-relaxed" style={{ color: 'var(--color-text-tertiary)' }}>{item.description}</div>
      </div>
    </div>
  );
}

// ============================================================================
// ComponentPalette Component
// ============================================================================

export function ComponentPalette({
  onDragStart,
  onDragEnd,
  collapsed = false,
  onToggleCollapse,
  searchQuery = '',
  onSearchChange,
  onOpenTemplates,
  onOpenToolGenerator,
  onOpenAgentGenerator,
  onOpenRegistry,
}: ComponentPaletteProps) {
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(
    new Set(['compute', 'integration', 'security', 'tools', 'connectors'])
  );

  // Filter items based on search query. Trim BEFORE matching — an untrimmed
  // query like "a " excludes items that contain "a" but not "a " (trailing
  // whitespace should never change search results); Property 41 checks the
  // trimmed semantics.
  const filteredItems = useMemo(() => {
    if (!searchQuery.trim()) return PALETTE_ITEMS;

    const query = searchQuery.trim().toLowerCase();
    return PALETTE_ITEMS.filter(
      (item) =>
        item.label.toLowerCase().includes(query) ||
        item.description.toLowerCase().includes(query)
    );
  }, [searchQuery]);

  // Group items by category
  const itemsByCategory = useMemo(() => {
    const grouped: Record<string, PaletteItem[]> = {
      compute: [],
      integration: [],
      security: [],
      tools: [],
      connectors: [],
    };

    for (const item of filteredItems) {
      grouped[item.category].push(item);
    }

    return grouped;
  }, [filteredItems]);

  const toggleCategory = useCallback((categoryId: string) => {
    setExpandedCategories((prev) => {
      const next = new Set(prev);
      if (next.has(categoryId)) {
        next.delete(categoryId);
      } else {
        next.add(categoryId);
      }
      return next;
    });
  }, []);

  if (collapsed) {
    return (
      <div
        className="w-12 h-full bg-white border-r border-[#e9ebed] flex flex-col items-center py-3"
        data-testid="component-palette-collapsed"
      >
        <button
          onClick={onToggleCollapse}
          className="p-1.5 rounded-md hover:bg-[#f2f3f3] transition-colors mb-3"
          title="Expand palette"
          aria-label="Expand component palette"
        >
          <svg className="w-4 h-4 text-[#5f6b7a]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
        </button>
        <div className="space-y-1.5">
          {PALETTE_ITEMS.map((item) => {
            const collapsedIcon = item.customIcon ? (
              <span className="text-base">{item.customIcon}</span>
            ) : (
              <div className="text-[#0972d3]">
                {COMPONENT_ICONS[item.type]}
              </div>
            );

            return (
              <div
                key={item.toolId || item.type}
                draggable
                onDragStart={(e) => {
                  e.dataTransfer.setData('application/agentcore-component', item.type);
                  if (item.toolId) {
                    e.dataTransfer.setData('application/agentcore-tool-id', item.toolId);
                  }
                  e.dataTransfer.effectAllowed = 'copy';
                  onDragStart?.(item.type, e);
                }}
                onDragEnd={onDragEnd}
                className="w-9 h-9 rounded-md bg-[#f2f3f3] hover:bg-[#0972d3]/10 flex items-center justify-center cursor-grab active:cursor-grabbing transition-colors"
                title={item.label}
              >
                {collapsedIcon}
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  return (
    <div
      className="w-[268px] h-full bg-white border-r border-[#e9ebed] flex flex-col"
      data-testid="component-palette"
    >
      {/* Header */}
      <div className="h-12 flex items-center justify-between px-3.5 border-b border-[#e9ebed] bg-[#fafafa]">
        <div className="flex items-center gap-2">
          <svg className="w-4 h-4 text-[#5f6b7a]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /><rect x="3" y="14" width="7" height="7" />
          </svg>
          <h2 className="font-semibold text-[#16191f] text-sm">Components</h2>
        </div>
        <button
          onClick={onToggleCollapse}
          className="p-1 rounded-md hover:bg-[#e9ebed] transition-colors"
          title="Collapse palette"
          aria-label="Collapse component palette"
        >
          <svg className="w-4 h-4 text-[#5f6b7a]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
        </button>
      </div>

      {/* Flow Sidebar */}
      <FlowSidebar />

      {/* Components Section Header */}
      <div className="flex items-center gap-2 px-3.5 pt-2.5 pb-1">
        <span className="font-medium text-[#16191f] text-[13px]">Components</span>
      </div>

      {/* Search Input */}
      <div className="px-2.5 pb-2 border-b border-[#e9ebed]">
        <div className="relative">
          <svg
            className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[#8d99a8]"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
            />
          </svg>
          <input
            type="text"
            placeholder="Filter components..."
            value={searchQuery}
            onChange={(e) => onSearchChange?.(e.target.value)}
            className="w-full pl-8 pr-3 py-2 text-sm bg-[#f2f3f3] border border-[#e9ebed] rounded-md focus:outline-none focus:ring-2 focus:ring-[#0972d3] focus:border-transparent focus:bg-white transition-all placeholder:text-[#8d99a8]"
            data-testid="palette-search-input"
          />
        </div>
      </div>

      {/* Component Categories */}
      <div className="flex-1 overflow-y-auto p-2.5 space-y-2">
        {CATEGORIES.map((category) => {
          const items = itemsByCategory[category.id];
          const isExpanded = expandedCategories.has(category.id);

          if (items.length === 0) return null;

          return (
            <div key={category.id} className="rounded-lg border border-[#e9ebed] overflow-hidden">
              <button
                onClick={() => toggleCategory(category.id)}
                className="w-full flex items-center justify-between px-3 py-2.5 bg-[#fafafa] hover:bg-[#f2f3f3] transition-colors"
                data-testid={`category-${category.id}`}
              >
                <div className="flex items-center gap-2">
                  <span className="text-sm">{category.icon}</span>
                  <span className="font-medium text-[#16191f] text-[13px]">{category.label}</span>
                  <span className="text-[11px] text-[#5f6b7a] bg-[#e9ebed] px-1.5 py-px rounded">{items.length}</span>
                </div>
                <svg
                  className={`w-3.5 h-3.5 text-[#8d99a8] transition-transform duration-200 ${isExpanded ? 'rotate-180' : ''}`}
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </button>

              {isExpanded && (
                <div className="p-1.5 space-y-1.5 bg-white" data-testid={`category-${category.id}-items`}>
                  {items.map((item) => (
                    <PaletteItemComponent
                      key={item.toolId || item.type}
                      item={item}
                      onDragStart={onDragStart}
                      onDragEnd={onDragEnd}
                    />
                  ))}
                </div>
              )}
            </div>
          );
        })}

        {filteredItems.length === 0 && (
          <div className="text-center py-8 text-[#8d99a8] text-sm">
            No components match your search
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="p-2.5 border-t border-[#e9ebed] space-y-1.5">
        {/* Primary action group */}
        <div className="flex gap-1.5">
          {onOpenTemplates && (
            <button
              onClick={onOpenTemplates}
              className="flex-1 py-2 px-2.5 bg-[#0972d3] hover:bg-[#0961b9] text-white rounded-md text-xs font-semibold transition-all duration-150 flex items-center justify-center gap-1.5"
              title="Browse pre-built templates"
              aria-label="Browse templates"
            >
              <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18" /><path d="M9 21V9" />
              </svg>
              Templates
            </button>
          )}
          {onOpenRegistry && (
            <button
              onClick={onOpenRegistry}
              className="flex-1 py-2 px-2.5 bg-white hover:bg-gray-50 text-[#0972d3] rounded-md text-xs font-semibold transition-all duration-150 flex items-center justify-center gap-1.5 border border-[#0972d3]/30 hover:border-[#0972d3]/50"
              title="Browse published agents from the registry"
              aria-label="Browse agent registry"
            >
              <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
              </svg>
              Registry
            </button>
          )}
        </div>
        {onOpenAgentGenerator && (
          <button
            onClick={onOpenAgentGenerator}
            className="w-full py-2 px-3 bg-gradient-to-r from-[#9d7eff] to-[#0972d3] hover:opacity-90 text-white rounded-md text-xs font-medium transition-opacity flex items-center justify-center gap-1.5"
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
            </svg>
            Generate Agent (AI)
          </button>
        )}
        {onOpenToolGenerator && (
          <button
            onClick={onOpenToolGenerator}
            className="w-full py-2 px-3 bg-[#f2f3f3] hover:bg-[#e9ebed] text-[#16191f] rounded-md text-xs font-medium transition-colors flex items-center justify-center gap-1.5 border border-[#e9ebed]"
          >
            <svg className="w-3.5 h-3.5 text-[#0972d3]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2a4 4 0 0 1 4 4c0 1.95-1.4 3.57-3.25 3.92L12 22" /><path d="M12 2a4 4 0 0 0-4 4c0 1.95 1.4 3.57 3.25 3.92" />
            </svg>
            AI Tool Generator
          </button>
        )}
        <div className="text-[10px] text-[#8d99a8] text-center pt-0.5">
          Drag components to canvas
        </div>
      </div>
    </div>
  );
}

export default ComponentPalette;
