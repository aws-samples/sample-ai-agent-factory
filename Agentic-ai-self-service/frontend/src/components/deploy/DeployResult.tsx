/**
 * DeployResult - displays successful deployment info and actions.
 */

interface DeployResultProps {
  message: string;
  simulated?: boolean;
  runtimeId?: string;
  endpoint?: string;
  gatewayUrl?: string;
  onRedeploy: () => void;
  onDelete: () => void;
  isDeleting: boolean;
}

export function DeployResult({
  message,
  simulated,
  runtimeId,
  endpoint,
  gatewayUrl,
  onRedeploy,
  onDelete,
  isDeleting,
}: DeployResultProps) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 p-4 bg-green-50 rounded-xl border border-green-100">
        <div className="w-6 h-6 rounded-full bg-green-500 flex items-center justify-center">
          <svg className="w-3.5 h-3.5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
          </svg>
        </div>
        <span className="text-green-700 font-medium">{message}</span>
      </div>

      {simulated && (
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
            onClick={() => endpoint && navigator.clipboard.writeText(endpoint)}
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
            <code className="text-sm font-mono text-gray-800 bg-gray-100 px-2 py-1 rounded">{runtimeId}</code>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-1">Endpoint URL</div>
            <code className="text-xs font-mono text-gray-600 break-all block bg-gray-100 p-2 rounded">{endpoint}</code>
          </div>
          {gatewayUrl && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-1">Gateway URL (MCP)</div>
              <code className="text-xs font-mono text-blue-600 break-all block bg-blue-50 p-2 rounded">{gatewayUrl}</code>
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
  --agent-id ${runtimeId} \\
  --agent-alias-id TSTALIASID \\
  --session-id test-session \\
  --input-text "Hello"`}</pre>
      </div>

      <button
        onClick={onRedeploy}
        className="w-full py-2.5 px-4 border border-gray-300 rounded-xl text-gray-700 hover:bg-gray-50 transition-colors text-sm"
      >
        Redeploy
      </button>
      <button
        onClick={onDelete}
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
  );
}
