/**
 * Compact row component for a single flow in the sidebar.
 * Displays flow name with active highlighting, edit and delete actions.
 * Uses inline-edit for rename instead of window.prompt().
 * Requirements: 4.1, 4.4, 5.1, 5.2, 6.1, 6.2
 */

import { useState, useRef, useEffect, useCallback } from 'react';
import type { FlowSummary } from '../../types/flow';
import { DeleteConfirmDialog } from './DeleteConfirmDialog';

// ============================================================================
// Props
// ============================================================================

export interface FlowSidebarItemProps {
  flow: FlowSummary;
  isActive: boolean;
  onOpen: (id: string) => void;
  onRename: (id: string, currentName: string, newName: string) => void;
  onDelete: (id: string) => void;
}

// ============================================================================
// Component
// ============================================================================

export function FlowSidebarItem({
  flow,
  isActive,
  onOpen,
  onRename,
  onDelete,
}: FlowSidebarItemProps) {
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [editName, setEditName] = useState(flow.name);
  const inputRef = useRef<HTMLInputElement>(null);

  // Focus input when entering edit mode
  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

  const handleRowClick = () => {
    if (!isEditing) {
      onOpen(flow.id);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (isEditing) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onOpen(flow.id);
    }
  };

  const handleRenameClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    setEditName(flow.name);
    setIsEditing(true);
  };

  const handleRenameConfirm = useCallback(() => {
    const trimmed = editName.trim();
    if (trimmed && trimmed !== flow.name) {
      onRename(flow.id, flow.name, trimmed);
    }
    setIsEditing(false);
  }, [editName, flow.id, flow.name, onRename]);

  const handleRenameCancel = useCallback(() => {
    setEditName(flow.name);
    setIsEditing(false);
  }, [flow.name]);

  const handleDeleteClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    setShowDeleteDialog(true);
  };

  const handleConfirmDelete = () => {
    setShowDeleteDialog(false);
    onDelete(flow.id);
  };

  const handleCancelDelete = () => {
    setShowDeleteDialog(false);
  };

  return (
    <>
      <div
        data-testid="flow-sidebar-item"
        role="button"
        tabIndex={0}
        className={`flex cursor-pointer items-center justify-between rounded-md border px-2 py-1.5 transition-colors ${
          isActive
            ? 'bg-[#0972d3]/10 border-[#0972d3]/30'
            : 'border-transparent hover:bg-[#f2f3f3]'
        }`}
        onClick={handleRowClick}
        onKeyDown={handleKeyDown}
      >
        {isEditing ? (
          <input
            ref={inputRef}
            type="text"
            value={editName}
            onChange={(e) => setEditName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleRenameConfirm();
              if (e.key === 'Escape') handleRenameCancel();
            }}
            onBlur={handleRenameConfirm}
            onClick={(e) => e.stopPropagation()}
            className="flex-1 text-[13px] px-1.5 py-0.5 rounded border border-[#0972d3] focus:outline-none focus:ring-1 focus:ring-[#0972d3] bg-white min-w-0"
            data-testid="flow-sidebar-item-rename-input"
          />
        ) : (
          <span
            data-testid="flow-sidebar-item-name"
            className="truncate text-[13px] text-[#16191f]"
          >
            {flow.name}
          </span>
        )}

        <div className="ml-2 flex shrink-0 items-center gap-0.5">
          {/* Edit / Rename button */}
          <button
            type="button"
            data-testid="flow-sidebar-item-edit"
            className="flex h-6 w-6 items-center justify-center rounded text-[#8d99a8] hover:bg-[#e9ebed] hover:text-[#16191f]"
            onClick={handleRenameClick}
            aria-label={`Rename ${flow.name}`}
          >
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10" />
            </svg>
          </button>

          {/* Delete button */}
          <button
            type="button"
            data-testid="flow-sidebar-item-delete"
            className="flex h-6 w-6 items-center justify-center rounded text-[#8d99a8] hover:bg-red-50 hover:text-red-600"
            onClick={handleDeleteClick}
            aria-label={`Delete ${flow.name}`}
          >
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
            </svg>
          </button>
        </div>
      </div>

      <DeleteConfirmDialog
        isOpen={showDeleteDialog}
        flowName={flow.name}
        onConfirm={handleConfirmDelete}
        onCancel={handleCancelDelete}
      />
    </>
  );
}
