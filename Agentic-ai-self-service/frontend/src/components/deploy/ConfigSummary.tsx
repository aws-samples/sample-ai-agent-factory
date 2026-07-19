/**
 * ConfigSummary - displays the runtime configuration card.
 */

import type { RuntimeConfiguration } from '../../types/components';
import type { WorkflowTemplate } from '../../types/templates';

interface ConfigSummaryProps {
  config: RuntimeConfiguration;
  connectedTools: string[];
  mcpServerConfig: Record<string, unknown> | null;
  connectors: Array<{ connector_id: string; auth_method: string }>;
  gatewayTools: string[];
  activeTemplate: WorkflowTemplate | null;
}

export function ConfigSummary({
  config,
  connectedTools,
  mcpServerConfig,
  connectors,
  gatewayTools,
  activeTemplate,
}: ConfigSummaryProps) {
  return (
    <div className="space-y-5">
      {/* Runtime Configuration Card */}
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

      {/* Connectors */}
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
          <div className="px-4 py-3 border-b border-gray-200 flex items-center gap-2" style={{ background: 'var(--color-bg-subtle)' }}>
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
    </div>
  );
}
