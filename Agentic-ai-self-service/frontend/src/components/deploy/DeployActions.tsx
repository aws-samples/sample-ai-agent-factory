/**
 * DeployActions - action buttons for deploy/download/export/publish.
 */

interface DeployActionsProps {
  canDeploy: boolean;
  canDownloadCfn: boolean;
  canPublish: boolean;
  state: 'idle' | 'deploying' | 'deployed' | 'error';
  isDownloadingCfn: boolean;
  isExportingPython: boolean;
  isPublishing: boolean;
  publishMsg: { kind: 'ok' | 'err'; text: string } | null;
  onDownloadCfn: () => void;
  onExportPython: () => void;
  onPublish: () => void;
  /** Why a standalone Python export cannot honour the current settings, or null. */
  pythonExportBlockedReason?: string | null;
  /**
   * Why the CloudFormation download is unavailable, or null. The button used to blame the
   * naming profile for every disabled state; live, the real blocker was a tag-governance
   * review, and the message sent the user to the wrong section.
   */
  cfnDownloadBlockedReason?: string | null;
  /** The last failure of each export, shown beside its own button. */
  cfnExportError?: string | null;
  pythonExportError?: string | null;
}

export function DeployActions({
  canDeploy,
  canDownloadCfn,
  canPublish,
  state,
  isDownloadingCfn,
  isExportingPython,
  isPublishing,
  publishMsg,
  onDownloadCfn,
  onExportPython,
  onPublish,
  pythonExportBlockedReason = null,
  cfnDownloadBlockedReason = null,
  cfnExportError = null,
  pythonExportError = null,
}: DeployActionsProps) {
  return (
    <div className="space-y-2">
      {/* Download CloudFormation Template */}
      {(state === 'idle' || state === 'deployed') && (
        <button
          onClick={onDownloadCfn}
          disabled={!canDownloadCfn || isDownloadingCfn}
          title={
            !canDownloadCfn
              ? (cfnDownloadBlockedReason || 'The CloudFormation download is not available yet')
              : undefined
          }
          className="w-full py-2.5 px-4 bg-white text-blue-700 border border-blue-500 rounded-md font-medium hover:bg-blue-50 hover:text-blue-800 disabled:bg-[#e9ebed] disabled:text-[#8d99a8] disabled:border-[#d1d5db] disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2 text-sm"
        >
          {isDownloadingCfn ? (
            <>
              <div className="w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
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
      {(state === 'idle' || state === 'deployed') && cfnExportError && (
        <p role="alert" className="text-xs text-red-600">
          {cfnExportError}
        </p>
      )}

      {/* Export as Python */}
      {(state === 'idle' || state === 'deployed') && (
        <>
        <button
          onClick={onExportPython}
          disabled={!canDeploy || isExportingPython || !!pythonExportBlockedReason}
          aria-describedby={pythonExportBlockedReason ? 'python-export-blocked-reason' : undefined}
          className="w-full py-2.5 px-4 bg-white text-blue-700 border border-blue-500 rounded-md font-medium hover:bg-blue-50 hover:text-blue-800 disabled:bg-[#e9ebed] disabled:text-[#8d99a8] disabled:border-[#d1d5db] disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2 text-sm"
        >
          {isExportingPython ? (
            <>
              <div className="w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
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
        {pythonExportError && (
          <p role="alert" className="text-xs text-red-600">
            {pythonExportError}
          </p>
        )}
        {pythonExportBlockedReason && (
          <p id="python-export-blocked-reason" role="status" className="text-xs text-[#5f6b7a]">
            {pythonExportBlockedReason}
          </p>
        )}
        </>
      )}

      {/* Publish to Registry */}
      {state === 'deployed' && canPublish && (
        <>
          <button
            onClick={onPublish}
            disabled={!canDeploy || isPublishing}
            className="w-full py-2.5 px-4 bg-white text-blue-700 border border-blue-500 rounded-md font-medium hover:bg-blue-50 hover:text-blue-800 disabled:bg-[#e9ebed] disabled:text-[#8d99a8] disabled:border-[#d1d5db] disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2 text-sm"
            title="Publish this agent's canvas as a reusable blueprint others can browse and clone"
          >
            {isPublishing ? (
              <>
                <div className="w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
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
