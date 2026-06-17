/**
 * Base ConfigurationModal component with tabbed interface.
 * Requirements: 3.1
 */

import { useState, useCallback, useEffect, type ReactNode } from 'react';

// ============================================================================
// Types
// ============================================================================

export interface ModalTab {
  id: string;
  label: string;
  content: ReactNode;
  hasError?: boolean;
}

export interface ValidationError {
  field: string;
  message: string;
}

export interface ConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: () => void;
  title: string;
  tabs: ModalTab[];
  validationErrors?: ValidationError[];
  isSaving?: boolean;
}

// ============================================================================
// ConfigurationModal Component
// ============================================================================

export function ConfigurationModal({
  isOpen,
  onClose,
  onSave,
  title,
  tabs,
  validationErrors = [],
  isSaving = false,
}: ConfigurationModalProps) {
  const [activeTabId, setActiveTabId] = useState<string>(tabs[0]?.id ?? '');
  const [hasInitialized, setHasInitialized] = useState(false);

  // Reset to first tab only when modal first opens (not on every render)
  useEffect(() => {
    if (isOpen && !hasInitialized) {
      setActiveTabId(tabs[0]?.id ?? '');
      setHasInitialized(true);
    } else if (!isOpen) {
      setHasInitialized(false);
    }
  }, [isOpen, hasInitialized, tabs]);

  // Handle escape key to close modal
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) {
        onClose();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  const handleSave = useCallback(() => {
    if (validationErrors.length === 0) {
      onSave();
    }
  }, [onSave, validationErrors]);

  const handleBackdropClick = useCallback(
    (e: React.MouseEvent) => {
      if (e.target === e.currentTarget) {
        onClose();
      }
    },
    [onClose]
  );

  if (!isOpen) return null;

  const activeTab = tabs.find((tab) => tab.id === activeTabId);
  const hasErrors = validationErrors.length > 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={handleBackdropClick}
      data-testid="configuration-modal-backdrop"
    >
      <div
        className="bg-white rounded-xl shadow-2xl max-h-[90vh] flex flex-col"
        style={{ width: 'var(--modal-width, 540px)' }}
        data-testid="configuration-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-title"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-200">
          <h2 id="modal-title" className="text-base font-semibold text-gray-800 truncate pr-2">
            {title}
          </h2>
          <button
            onClick={onClose}
            className="p-2 rounded-lg hover:bg-gray-100 transition-colors"
            aria-label="Close modal"
            data-testid="modal-close-button"
          >
            <svg className="w-5 h-5 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Tabs */}
        {tabs.length > 1 && (
          <div className="flex border-b border-gray-200 px-4 overflow-x-auto" role="tablist">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTabId(tab.id)}
                className={`
                  relative px-3 py-2.5 text-xs font-medium transition-colors whitespace-nowrap
                  ${activeTabId === tab.id
                    ? 'text-blue-600 border-b-2 border-blue-600 -mb-px'
                    : 'text-gray-500 hover:text-gray-700'
                  }
                `}
                role="tab"
                aria-selected={activeTabId === tab.id}
                aria-controls={`tabpanel-${tab.id}`}
                data-testid={`tab-${tab.id}`}
              >
                {tab.label}
                {tab.hasError && (
                  <span className="absolute -top-1 -right-1 w-2 h-2 bg-red-500 rounded-full" />
                )}
              </button>
            ))}
          </div>
        )}

        {/* Content */}
        <div
          className="overflow-y-auto p-5"
          style={{ height: 'var(--modal-content-height, 360px)' }}
          role="tabpanel"
          id={`tabpanel-${activeTabId}`}
          aria-labelledby={`tab-${activeTabId}`}
        >
          {activeTab?.content}
        </div>

        {/* Validation Errors Summary */}
        {hasErrors && (
          <div className="px-5 py-3 bg-red-50 border-t border-red-200">
            <div className="flex items-start gap-2">
              <svg className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <div>
                <p className="text-sm font-medium text-red-800">
                  Please fix the following errors:
                </p>
                <ul className="mt-1 text-sm text-red-700 list-disc list-inside">
                  {validationErrors.slice(0, 3).map((error, index) => (
                    <li key={index}>{error.message}</li>
                  ))}
                  {validationErrors.length > 3 && (
                    <li>...and {validationErrors.length - 3} more</li>
                  )}
                </ul>
              </div>
            </div>
          </div>
        )}

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 px-5 py-3 border-t border-gray-200 bg-gray-50 rounded-b-xl">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
            data-testid="modal-cancel-button"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={hasErrors || isSaving}
            className={`
              px-4 py-2 text-sm font-medium text-white rounded-lg transition-colors
              ${hasErrors || isSaving
                ? 'bg-blue-300 cursor-not-allowed'
                : 'bg-blue-600 hover:bg-blue-700'
              }
            `}
            data-testid="modal-save-button"
          >
            {isSaving ? (
              <span className="flex items-center gap-2">
                <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                </svg>
                Saving...
              </span>
            ) : (
              'Save'
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

export default ConfigurationModal;
