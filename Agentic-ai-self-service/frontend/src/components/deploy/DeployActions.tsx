/**
 * DeployActions - action buttons for deploy/download/export/publish.
 */

interface DeployActionsProps {
  canDeploy: boolean;
  state: 'idle' | 'deploying' | 'deployed' | 'error';
  isDownloadingCfn: boolean;
  isExportingPython: boolean;
  isPublishing: boolean;
  publishMsg: { kind: 'ok' | 'err'; text: string } | null;
  onDeploy: () => void;
  onDownloadCfn: () => void;
  onExportPython: () => void;
  onPublish: () => void;
}

export function DeployActions({
  canDeploy,
  state,
  isDownloadingCfn,
  isExportingPython,
  isPublishing,
  publishMsg,
  onDeploy,
  onDownloadCfn,
  onExportPython,
  onPublish,
}: DeployActionsProps) {
  return (
    <div className="space-y-2">
      {/* Deploy Button */}
      {state === 'idle' && (
        <button
          onClick={onDeploy}
          disabled={!canDeploy}
          className="w-full py-3 px-4 bg-[#ff9900] text-[#232f3e] rounded-md font-semibold hover:bg-[#ec7211] disabled:bg-[#e9ebed] disabled:text-[#8d99a8] disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2 text-sm"
        >
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7z" />
          </svg>
          Deploy to AgentCore
        </button>
      )}

      {/* Download CloudFormation Template */}
      {(state === 'idle' || state === 'deployed') && (
        <button
          onClick={onDownloadCfn}
          disabled={!canDeploy || isDownloadingCfn}
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
      )}

      {/* Export as Python */}
      {(state === 'idle' || state === 'deployed') && (
        <button
          onClick={onExportPython}
          disabled={!canDeploy || isExportingPython}
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
      )}

      {/* Publish to Registry */}
      {state === 'deployed' && (
        <>
          <button
            onClick={onPublish}
            disabled={!canDeploy || isPublishing}
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
        </>
      )}
    </div>
  );
}
