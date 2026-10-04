/**
 * Accessible confirm dialog component.
 * Replaces native confirm() with proper modal + focus trap + Escape handling.
 */

import { useId, useRef } from 'react';
import { m } from 'motion/react';
import { spring } from '../../lib/motion';
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap';

interface ConfirmDialogProps {
  isOpen: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'danger' | 'default';
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  isOpen,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'default',
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const cancelButtonRef = useRef<HTMLButtonElement>(null);
  const confirmButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const messageId = useId();
  useDialogFocusTrap(
    isOpen,
    dialogRef,
    variant === 'danger' ? cancelButtonRef : confirmButtonRef,
    onCancel,
  );

  if (!isOpen) return null;

  return (
    <>
      {/* Backdrop */}
      <m.div
        className="fixed inset-0 bg-black/40 z-50"
        style={{ backdropFilter: 'blur(4px)' }}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        onPointerDown={onCancel}
        aria-hidden="true"
      />

      {/* Dialog */}
      <div
        ref={dialogRef}
        className="fixed inset-0 flex items-center justify-center z-50 px-4"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={messageId}
        tabIndex={-1}
        onPointerDown={(event) => {
          if (event.target === event.currentTarget) {
            onCancel();
          }
        }}
      >
        <m.div
          className="bg-white rounded-xl border border-gray-200 shadow-xl max-w-md w-full p-6"
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={spring.gentle}
        >
          <h3
            id={titleId}
            className="text-lg font-semibold text-gray-900 mb-2"
          >
            {title}
          </h3>
          <p
            id={messageId}
            className="text-sm text-gray-600 mb-6"
          >
            {message}
          </p>
          <div className="flex items-center justify-end gap-3">
            <button
              ref={cancelButtonRef}
              type="button"
              onClick={onCancel}
              className="px-4 py-2 rounded-md text-sm font-medium text-gray-700 bg-white border border-gray-300 hover:bg-gray-50 transition-colors"
            >
              {cancelLabel}
            </button>
            <button
              ref={confirmButtonRef}
              type="button"
              onClick={onConfirm}
              className={`px-4 py-2 rounded-md text-sm font-medium text-white transition-colors ${
                variant === 'danger'
                  ? 'bg-red-600 hover:bg-red-700'
                  : 'bg-[#0972d3] hover:bg-[#0961b9]'
              }`}
            >
              {confirmLabel}
            </button>
          </div>
        </m.div>
      </div>
    </>
  );
}
