/**
 * Collapsible flow list section for the ComponentPalette sidebar.
 * Displays flows with create, open, rename, and delete actions.
 * Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 5.3, 6.3, 6.5, 8.1, 8.2, 8.3, 8.4
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { useFlowStore } from '../../store/flowStore';
import { FlowSidebarItem } from './FlowSidebarItem';

// ============================================================================
// Component
// ============================================================================

export function FlowSidebar() {
  const [isExpanded, setIsExpanded] = useState(true);

  const flows = useFlowStore((s) => s.flows);
  const activeFlowId = useFlowStore((s) => s.activeFlowId);
  const isLoading = useFlowStore((s) => s.isLoading);
  const error = useFlowStore((s) => s.error);
  const fetchFlows = useFlowStore((s) => s.fetchFlows);
  const createFlow = useFlowStore((s) => s.createFlow);
  const openFlow = useFlowStore((s) => s.openFlow);
  const renameFlow = useFlowStore((s) => s.renameFlow);
  const deleteFlow = useFlowStore((s) => s.deleteFlow);

  const [hasFetched, setHasFetched] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [createName, setCreateName] = useState('');
  const createInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    fetchFlows().then(() => setHasFetched(true));
  }, [fetchFlows]);

  // Auto-open the most recent flow, or create a default one if none exist
  useEffect(() => {
    if (!hasFetched || isLoading || error || activeFlowId) return;

    if (flows.length > 0) {
      // Open the first (most recent) flow automatically
      openFlow(flows[0].id);
    } else {
      // No flows at all — create a default one
      createFlow('Untitled Flow');
    }
  }, [hasFetched, isLoading, error, flows, activeFlowId, openFlow, createFlow]);

  // Focus the create input when it appears
  useEffect(() => {
    if (isCreating && createInputRef.current) {
      createInputRef.current.focus();
    }
  }, [isCreating]);

  const handleToggle = useCallback(() => {
    setIsExpanded((prev) => !prev);
  }, []);

  const handleCreateClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      setIsCreating(true);
      setCreateName('Untitled Flow');
    },
    [],
  );

  const handleCreateConfirm = useCallback(() => {
    const trimmed = createName.trim();
    if (trimmed) {
      createFlow(trimmed);
    }
    setIsCreating(false);
    setCreateName('');
  }, [createName, createFlow]);

  const handleCreateCancel = useCallback(() => {
    setIsCreating(false);
    setCreateName('');
  }, []);

  const handleOpen = useCallback(
    (id: string) => {
      openFlow(id);
    },
    [openFlow],
  );

  const handleRename = useCallback(
    (id: string, _currentName: string, newName: string) => {
      renameFlow(id, newName);
    },
    [renameFlow],
  );

  const handleDelete = useCallback(
    (id: string) => {
      deleteFlow(id);
    },
    [deleteFlow],
  );

  return (
    <div data-testid="flow-sidebar" className="border-b border-[#e9ebed]">
      {/* Header */}
      <div
        data-testid="flow-sidebar-header"
        className="flex cursor-pointer items-center justify-between px-3 py-2.5 bg-[#fafafa] hover:bg-[#f2f3f3]"
        onClick={handleToggle}
      >
        <div className="flex items-center gap-1.5" data-testid="flow-sidebar-toggle">
          {/* Chevron */}
          <svg
            className={`h-3.5 w-3.5 text-[#8d99a8] transition-transform ${isExpanded ? 'rotate-90' : ''}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
          <span className="font-medium text-[#16191f] text-[13px]">Flows</span>
        </div>

        {/* Create button */}
        <button
          type="button"
          data-testid="flow-sidebar-create"
          className="flex h-5 w-5 items-center justify-center rounded text-[#8d99a8] hover:bg-[#e9ebed] hover:text-[#16191f]"
          onClick={handleCreateClick}
          aria-label="Create flow"
        >
          <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
          </svg>
        </button>
      </div>

      {/* Expanded content */}
      {isExpanded && (
        <div data-testid="flow-sidebar-list" className="p-1.5 space-y-0.5">
          {/* Inline create input */}
          {isCreating && (
            <div className="flex items-center gap-1 px-2 py-1">
              <input
                ref={createInputRef}
                type="text"
                value={createName}
                onChange={(e) => setCreateName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleCreateConfirm();
                  if (e.key === 'Escape') handleCreateCancel();
                }}
                onBlur={handleCreateConfirm}
                className="flex-1 text-[13px] px-1.5 py-0.5 rounded border border-[#0972d3] focus:outline-none focus:ring-1 focus:ring-[#0972d3] bg-white"
                data-testid="flow-sidebar-create-input"
              />
            </div>
          )}

          {isLoading && (
            <p data-testid="flow-sidebar-loading" className="px-2 py-1 text-[12px] text-[#8d99a8]">
              Loading...
            </p>
          )}

          {error && (
            <div data-testid="flow-sidebar-error" className="mx-1.5 mb-1 flex items-start gap-1.5 rounded-md bg-red-50 border border-red-200 px-2 py-1.5">
              <svg className="h-3.5 w-3.5 text-red-400 mt-0.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
              </svg>
              <span className="text-[11px] text-red-600 leading-tight">Something went wrong. Please try again.</span>
            </div>
          )}

          {!isLoading && !error && flows.length === 0 && !isCreating && (
            <p data-testid="flow-sidebar-empty" className="px-2 py-1 text-[12px] text-[#8d99a8]">
              No flows yet
            </p>
          )}

          {!isLoading && flows.map((flow) => (
            <FlowSidebarItem
              key={flow.id}
              flow={flow}
              isActive={flow.id === activeFlowId}
              onOpen={handleOpen}
              onRename={handleRename}
              onDelete={handleDelete}
            />
          ))}
        </div>
      )}
    </div>
  );
}
