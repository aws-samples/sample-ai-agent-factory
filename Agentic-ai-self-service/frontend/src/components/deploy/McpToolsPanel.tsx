import { useCallback, useEffect, useMemo, useState } from 'react';
import { getErrorMessage } from '../../services/api/client';
import {
  callMcpTool,
  discoverMcpTools,
  type McpToolCallResult,
  type McpToolDescriptor,
  type McpToolsResult,
} from '../../services/api/runtimeMcp';

interface McpToolsPanelProps {
  deploymentId?: string;
}

function placeholderForSchema(schema: unknown): unknown {
  if (!schema || typeof schema !== 'object') return '';
  const value = schema as Record<string, unknown>;
  if (value.default !== undefined) return value.default;
  if (Array.isArray(value.enum) && value.enum.length > 0) return value.enum[0];
  switch (value.type) {
    case 'boolean':
      return false;
    case 'integer':
    case 'number':
      return 0;
    case 'array':
      return [];
    case 'object':
      return {};
    default:
      return '';
  }
}

function initialArguments(tool: McpToolDescriptor | undefined): string {
  if (!tool) return '{}';
  const properties = tool.inputSchema.properties;
  const required = tool.inputSchema.required;
  if (
    !properties
    || typeof properties !== 'object'
    || Array.isArray(properties)
    || !Array.isArray(required)
  ) {
    return '{}';
  }
  const result: Record<string, unknown> = {};
  for (const key of required) {
    if (typeof key === 'string' && key in properties) {
      result[key] = placeholderForSchema(
        (properties as Record<string, unknown>)[key],
      );
    }
  }
  return JSON.stringify(result, null, 2);
}

function renderCallResult(result: McpToolCallResult): string {
  const value = result.structuredContent ?? result.content;
  return JSON.stringify(value, null, 2);
}

export function McpToolsPanel({ deploymentId }: McpToolsPanelProps) {
  const [catalog, setCatalog] = useState<McpToolsResult | null>(null);
  const [selectedName, setSelectedName] = useState('');
  const [argumentsText, setArgumentsText] = useState('{}');
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);
  const [callError, setCallError] = useState<string | null>(null);
  const [callResult, setCallResult] = useState<McpToolCallResult | null>(null);
  const [isDiscovering, setIsDiscovering] = useState(false);
  const [isCalling, setIsCalling] = useState(false);

  const selectedTool = useMemo(
    () => catalog?.tools.find((tool) => tool.name === selectedName),
    [catalog, selectedName],
  );

  const discover = useCallback(async (signal?: AbortSignal) => {
    if (!deploymentId) {
      setDiscoveryError(
        'This deployment has no product deployment identifier, so its MCP tools cannot be verified.',
      );
      setCatalog(null);
      return;
    }
    setIsDiscovering(true);
    setDiscoveryError(null);
    setCallError(null);
    setCallResult(null);
    try {
      const result = await discoverMcpTools(deploymentId, signal);
      if (signal?.aborted) return;
      setCatalog(result);
      const first = result.tools[0];
      setSelectedName(first?.name ?? '');
      setArgumentsText(initialArguments(first));
    } catch (error) {
      if (signal?.aborted) return;
      setCatalog(null);
      setDiscoveryError(getErrorMessage(error));
    } finally {
      if (!signal?.aborted) setIsDiscovering(false);
    }
  }, [deploymentId]);

  useEffect(() => {
    const controller = new AbortController();
    void discover(controller.signal);
    return () => controller.abort();
  }, [discover]);

  const selectTool = useCallback((tool: McpToolDescriptor) => {
    setSelectedName(tool.name);
    setArgumentsText(initialArguments(tool));
    setCallError(null);
    setCallResult(null);
  }, []);

  const invoke = useCallback(async () => {
    if (!deploymentId || !selectedTool) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(argumentsText);
    } catch {
      setCallError('Arguments must be valid JSON.');
      return;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      setCallError('Arguments must be a JSON object.');
      return;
    }

    setIsCalling(true);
    setCallError(null);
    setCallResult(null);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 30_000);
    try {
      const result = await callMcpTool(
        deploymentId,
        selectedTool.name,
        parsed as Record<string, unknown>,
        catalog?.sessionId,
        controller.signal,
      );
      setCatalog((current) => (
        current
          ? { ...current, sessionId: result.sessionId ?? current.sessionId }
          : current
      ));
      setCallResult(result);
    } catch (error) {
      setCallError(
        error instanceof DOMException && error.name === 'AbortError'
          ? 'The MCP tool call timed out. Try again shortly.'
          : getErrorMessage(error),
      );
    } finally {
      window.clearTimeout(timeout);
      setIsCalling(false);
    }
  }, [argumentsText, catalog?.sessionId, deploymentId, selectedTool]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="border-b border-[#e9ebed] bg-[#fafafa] px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-[#232f3e]">MCP Tools</h3>
            <p className="text-xs text-[#5f6b7a]">
              {catalog
                ? `${catalog.serverInfo.name} ${catalog.serverInfo.version} · ${catalog.protocolVersion}`
                : 'Discovering the tools exposed by this runtime'}
            </p>
          </div>
          <button
            type="button"
            onClick={() => void discover()}
            disabled={isDiscovering}
            className="rounded-md border border-[#aab7b8] px-2.5 py-1.5 text-xs font-medium text-[#232f3e] disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isDiscovering ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {isDiscovering && !catalog && (
          <div role="status" className="rounded-lg border border-blue-100 bg-blue-50 p-3 text-sm text-blue-800">
            Initializing MCP and loading tools…
          </div>
        )}

        {discoveryError && (
          <div role="alert" className="space-y-3 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
            <p>{discoveryError}</p>
            <button
              type="button"
              onClick={() => void discover()}
              className="rounded-md border border-red-300 px-2.5 py-1.5 text-xs font-medium"
            >
              Retry discovery
            </button>
          </div>
        )}

        {catalog && catalog.tools.length === 0 && (
          <div role="status" className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
            The runtime completed the MCP handshake but advertised no tools.
          </div>
        )}

        {catalog && catalog.tools.length > 0 && (
          <div className="space-y-4">
            <fieldset>
              <legend className="mb-2 text-xs font-semibold uppercase tracking-wide text-[#5f6b7a]">
                Available tools
              </legend>
              <div className="grid gap-2">
                {catalog.tools.map((tool) => {
                  const selected = tool.name === selectedName;
                  return (
                    <button
                      key={tool.name}
                      type="button"
                      aria-pressed={selected}
                      onClick={() => selectTool(tool)}
                      className={`rounded-lg border p-3 text-left transition-colors ${
                        selected
                          ? 'border-[#0073bb] bg-blue-50'
                          : 'border-[#d5dbdb] bg-white hover:border-[#879596]'
                      }`}
                    >
                      <span className="block font-mono text-sm font-semibold text-[#232f3e]">
                        {tool.title || tool.name}
                      </span>
                      {tool.title && (
                        <span className="block font-mono text-[11px] text-[#5f6b7a]">
                          {tool.name}
                        </span>
                      )}
                      {tool.description && (
                        <span className="mt-1 block text-xs text-[#5f6b7a]">
                          {tool.description}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            </fieldset>

            {selectedTool && (
              <div className="space-y-3 rounded-lg border border-[#d5dbdb] p-3">
                <div>
                  <label
                    htmlFor="mcp-tool-arguments"
                    className="mb-1 block text-xs font-semibold text-[#232f3e]"
                  >
                    Arguments for {selectedTool.name}
                  </label>
                  <textarea
                    id="mcp-tool-arguments"
                    value={argumentsText}
                    onChange={(event) => setArgumentsText(event.target.value)}
                    rows={7}
                    spellCheck={false}
                    className="w-full rounded-md border border-[#aab7b8] bg-white p-2 font-mono text-xs text-[#232f3e] focus:border-[#0073bb] focus:outline-none"
                  />
                </div>
                <details>
                  <summary className="cursor-pointer text-xs font-medium text-[#0073bb]">
                    Input schema
                  </summary>
                  <pre className="mt-2 overflow-x-auto rounded bg-[#f2f3f3] p-2 text-[11px] text-[#232f3e]">
                    {JSON.stringify(selectedTool.inputSchema, null, 2)}
                  </pre>
                </details>
                <button
                  type="button"
                  onClick={() => void invoke()}
                  disabled={isCalling}
                  className="w-full rounded-md bg-[#ff9900] px-3 py-2 text-sm font-semibold text-[#232f3e] disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {isCalling ? 'Calling tool…' : `Call ${selectedTool.name}`}
                </button>
              </div>
            )}

            {callError && (
              <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
                {callError}
              </div>
            )}
            {callResult && (
              <div
                role="status"
                className={`rounded-lg border p-3 ${
                  callResult.isError
                    ? 'border-red-200 bg-red-50 text-red-800'
                    : 'border-emerald-200 bg-emerald-50 text-emerald-900'
                }`}
              >
                <div className="mb-2 text-xs font-semibold uppercase tracking-wide">
                  {callResult.isError ? 'Tool returned an error' : 'Tool result'}
                </div>
                <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs">
                  {renderCallResult(callResult)}
                </pre>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
