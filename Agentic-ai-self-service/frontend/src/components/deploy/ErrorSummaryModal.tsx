/**
 * Error summary modal component.
 * Requirements: 8.6 - Show error summary modal when deployment is blocked
 */

import { useCallback } from 'react';
import type { ValidationError } from '../../types/validation';

export interface ErrorSummaryModalProps {
  isOpen: boolean;
  onClose: () => void;
  errors: ValidationError[];
  warnings: ValidationError[];
}

/**
 * Modal displaying a summary of all validation errors and warnings.
 * Property 26: Deployment Blocked on Validation Errors
 */
export function ErrorSummaryModal({
  isOpen,
  onClose,
  errors,
  warnings,
}: ErrorSummaryModalProps) {
  const handleBackdropClick = useCallback(
    (e: React.MouseEvent) => {
      if (e.target === e.currentTarget) {
        onClose();
      }
    },
    [onClose]
  );

  if (!isOpen) {
    return null;
  }

  // Group errors by component
  const errorsByComponent = groupByComponent(errors);
  const warningsByComponent = groupByComponent(warnings);

  return (
    <div
      className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50"
      onClick={handleBackdropClick}
      data-testid="error-summary-modal"
    >
      <div className="bg-white rounded-lg shadow-xl mx-4 max-h-[80vh] flex flex-col" style={{ width: 'var(--modal-width, 540px)' }}>
        {/* Header */}
        <div className="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="text-2xl">⚠️</span>
            <div>
              <h2 className="text-lg font-semibold text-gray-900">
                Deployment Blocked
              </h2>
              <p className="text-sm text-gray-500">
                Please fix the following issues before deploying
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 transition-colors"
            aria-label="Close"
          >
            <svg
              className="w-6 h-6"
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

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {/* Errors Section */}
          {errors.length > 0 && (
            <div className="mb-6">
              <h3 className="text-sm font-semibold text-red-700 uppercase tracking-wide mb-3 flex items-center gap-2">
                <span className="w-2 h-2 bg-red-500 rounded-full"></span>
                Errors ({errors.length})
              </h3>
              <div className="space-y-3">
                {Object.entries(errorsByComponent).map(([componentId, componentErrors]) => (
                  <div
                    key={componentId}
                    className="bg-red-50 border border-red-200 rounded-lg p-3"
                  >
                    <div className="font-medium text-red-800 text-sm mb-2">
                      {componentId || 'General'}
                    </div>
                    <ul className="space-y-1">
                      {componentErrors.map((error, index) => (
                        <li
                          key={index}
                          className="text-sm text-red-700 flex items-start gap-2"
                        >
                          <span className="text-red-400 mt-0.5">•</span>
                          <span>
                            <span className="font-medium">{error.field}:</span>{' '}
                            {error.message}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Warnings Section */}
          {warnings.length > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-yellow-700 uppercase tracking-wide mb-3 flex items-center gap-2">
                <span className="w-2 h-2 bg-yellow-500 rounded-full"></span>
                Warnings ({warnings.length})
              </h3>
              <div className="space-y-3">
                {Object.entries(warningsByComponent).map(([componentId, componentWarnings]) => (
                  <div
                    key={componentId}
                    className="bg-yellow-50 border border-yellow-200 rounded-lg p-3"
                  >
                    <div className="font-medium text-yellow-800 text-sm mb-2">
                      {componentId || 'General'}
                    </div>
                    <ul className="space-y-1">
                      {componentWarnings.map((warning, index) => (
                        <li
                          key={index}
                          className="text-sm text-yellow-700 flex items-start gap-2"
                        >
                          <span className="text-yellow-400 mt-0.5">•</span>
                          <span>
                            <span className="font-medium">{warning.field}:</span>{' '}
                            {warning.message}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* No Issues */}
          {errors.length === 0 && warnings.length === 0 && (
            <div className="text-center py-8 text-gray-500">
              <span className="text-4xl mb-2 block">✓</span>
              <p>No validation issues found</p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-gray-200 flex justify-end gap-3">
          <button
            onClick={onClose}
            className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors font-medium"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * Group validation errors by component ID.
 */
function groupByComponent(
  items: ValidationError[]
): Record<string, ValidationError[]> {
  return items.reduce(
    (acc, item) => {
      const key = item.componentId || 'general';
      if (!acc[key]) {
        acc[key] = [];
      }
      acc[key].push(item);
      return acc;
    },
    {} as Record<string, ValidationError[]>
  );
}

export default ErrorSummaryModal;
