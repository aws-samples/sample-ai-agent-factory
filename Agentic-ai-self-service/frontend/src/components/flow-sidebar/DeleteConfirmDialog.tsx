/**
 * Confirmation dialog for flow deletion.
 * Overlay + centered card pattern with confirm/cancel buttons.
 * Requirements: 4.1
 */

// ============================================================================
// Props
// ============================================================================

export interface DeleteConfirmDialogProps {
  isOpen: boolean;
  flowName: string;
  onConfirm: () => void;
  onCancel: () => void;
}

// ============================================================================
// Component
// ============================================================================

export function DeleteConfirmDialog({
  isOpen,
  flowName,
  onConfirm,
  onCancel,
}: DeleteConfirmDialogProps) {
  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onCancel}
      role="dialog"
      aria-modal="true"
      aria-label="Delete confirmation"
    >
      <div
        className="mx-4 w-full max-w-md rounded-lg bg-white p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold text-gray-900">Delete Flow</h2>
        <p className="mt-2 text-sm text-gray-600">
          Are you sure you want to delete <span className="font-medium">"{flowName}"</span>? This
          action cannot be undone.
        </p>
        <div className="mt-6 flex justify-end gap-3">
          <button
            type="button"
            className="rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            type="button"
            className="rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
            onClick={onConfirm}
          >
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}
