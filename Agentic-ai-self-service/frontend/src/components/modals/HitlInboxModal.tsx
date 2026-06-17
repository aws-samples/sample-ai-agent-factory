/**
 * HitlInboxModal — Phase 2 Gap 2D.
 *
 * Global human-in-the-loop approval inbox. Lists all pending approval requests
 * across every runtime owned by the caller. Each request displays the action,
 * reason, runtime_id, timestamp, and Approve/Reject decision buttons.
 *
 * Matches modal shell pattern from EvaluationConfigurationModal and API/loading
 * pattern from VersionsList.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  getApiClient,
  getErrorMessage,
  type HitlRequestSummary,
} from '../../services/api';

export interface HitlInboxModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export function HitlInboxModal({ isOpen, onClose }: HitlInboxModalProps) {
  const [requests, setRequests] = useState<HitlRequestSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null); // request_id being acted upon
  const [comments, setComments] = useState<Record<string, string>>({}); // Per-request comment text

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const api = getApiClient();
      const pending = await api.listHitlPending();
      setRequests(pending);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isOpen) {
      void reload();
    }
  }, [isOpen, reload]);

  const handleDecision = async (
    requestId: string,
    runtimeId: string,
    decision: 'approve' | 'reject'
  ) => {
    setActing(requestId);
    setError(null);
    try {
      const comment = comments[requestId] || '';
      await getApiClient().decideHitl(requestId, runtimeId, decision, comment);
      // Clear the comment field for this request
      setComments((prev) => {
        const next = { ...prev };
        delete next[requestId];
        return next;
      });
      // Reload to remove the decided request from the queue
      await reload();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setActing(null);
    }
  };

  const updateComment = (requestId: string, value: string) => {
    setComments((prev) => ({ ...prev, [requestId]: value }));
  };

  if (!isOpen) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40">
      <div className="relative w-full max-w-3xl max-h-[90vh] bg-white rounded-xl shadow-xl flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">
              Human-in-the-loop Approvals
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              Pending approval requests across all your runtimes
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void reload()}
              disabled={loading}
              className="text-xs px-2.5 py-1 rounded border border-gray-200 hover:bg-gray-50 disabled:opacity-50 transition-colors"
            >
              {loading ? 'Loading…' : 'Refresh'}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="text-gray-400 hover:text-gray-600 transition-colors"
              aria-label="Close"
            >
              <svg
                className="w-5 h-5"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M6 18L18 6M6 6l12 12"
                />
              </svg>
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {error && (
            <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </div>
          )}

          {loading && requests.length === 0 ? (
            <div className="text-sm text-gray-500">Loading pending requests…</div>
          ) : requests.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <div className="text-4xl mb-3">✓</div>
              <div className="text-sm font-medium text-gray-700">
                No pending approvals
              </div>
              <div className="text-xs text-gray-500 mt-1">
                All requests have been handled
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              {requests.map((req) => {
                const isActing = acting === req.request_id;
                const createdDate = new Date(req.created_at * 1000);
                return (
                  <div
                    key={req.request_id}
                    className="rounded-lg border border-gray-200 bg-white p-4 space-y-3"
                  >
                    <div className="space-y-1">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-semibold text-gray-900">
                          {req.action}
                        </span>
                        <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-100 text-amber-700">
                          {req.status}
                        </span>
                      </div>
                      <div className="text-xs text-gray-600">{req.reason}</div>
                      <div className="flex items-center gap-3 text-[11px] text-gray-500">
                        <span>
                          Runtime:{' '}
                          <code className="font-mono text-[10px] bg-gray-100 px-1 py-0.5 rounded">
                            {req.runtime_id.slice(0, 24)}
                            {req.runtime_id.length > 24 ? '…' : ''}
                          </code>
                        </span>
                        <span>{createdDate.toLocaleString()}</span>
                      </div>
                    </div>

                    {/* Comment input */}
                    <div>
                      <label
                        htmlFor={`comment-${req.request_id}`}
                        className="block text-xs font-medium text-gray-700 mb-1"
                      >
                        Comment (optional)
                      </label>
                      <input
                        id={`comment-${req.request_id}`}
                        type="text"
                        value={comments[req.request_id] || ''}
                        onChange={(e) =>
                          updateComment(req.request_id, e.target.value)
                        }
                        disabled={isActing}
                        placeholder="Add a reason for your decision…"
                        className="w-full px-2.5 py-1.5 text-xs border border-gray-200 rounded focus:outline-none focus:ring-2 focus:ring-emerald-500/20 focus:border-emerald-500 disabled:opacity-50 disabled:cursor-not-allowed"
                      />
                    </div>

                    {/* Action buttons */}
                    <div className="flex gap-2">
                      <button
                        type="button"
                        onClick={() =>
                          void handleDecision(
                            req.request_id,
                            req.runtime_id,
                            'approve'
                          )
                        }
                        disabled={isActing}
                        className="flex-1 px-3 py-1.5 text-xs font-medium rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                      >
                        {isActing ? 'Approving…' : 'Approve'}
                      </button>
                      <button
                        type="button"
                        onClick={() =>
                          void handleDecision(
                            req.request_id,
                            req.runtime_id,
                            'reject'
                          )
                        }
                        disabled={isActing}
                        className="flex-1 px-3 py-1.5 text-xs font-medium rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                      >
                        {isActing ? 'Rejecting…' : 'Reject'}
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
