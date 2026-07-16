/**
 * Base ConfigurationModal component with tabbed interface.
 * Requirements: 3.1
 */

import { useState, useCallback, type ReactNode } from 'react';
import { ModalShell } from './ModalShell';

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
  const [activeTabId, setActiveTabId] = useState<string>(() => tabs[0]?.id ?? '');

  // Reset to first tab when modal opens (adjust state during render pattern)
  const [lastIsOpen, setLastIsOpen] = useState(isOpen);
  if (isOpen !== lastIsOpen) {
    setLastIsOpen(isOpen);
    if (isOpen) {
      setActiveTabId(tabs[0]?.id ?? '');
    }
  }

  const handleSave = useCallback(() => {
    if (validationErrors.length === 0) {
      onSave();
    }
  }, [onSave, validationErrors]);

  const activeTab = tabs.find((tab) => tab.id === activeTabId);
  const hasErrors = validationErrors.length > 0;

  const footer = (
    <>
      <button
        onClick={onClose}
        className="px-4 py-2 text-sm font-medium border transition-colors"
        style={{
          color: 'var(--color-text-secondary)',
          backgroundColor: 'var(--color-surface)',
          borderColor: 'var(--color-border)',
          borderRadius: 'var(--radius-control)',
        }}
        data-testid="modal-cancel-button"
      >
        Cancel
      </button>
      <button
        onClick={handleSave}
        disabled={hasErrors || isSaving}
        className={`px-4 py-2 text-sm font-medium text-white transition-colors ${
          hasErrors || isSaving ? 'cursor-not-allowed' : ''
        }`}
        style={{
          backgroundColor: hasErrors || isSaving ? '#93c5fd' : 'var(--color-aws-blue)',
          borderRadius: 'var(--radius-control)',
        }}
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
    </>
  );

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      title={title}
      footer={footer}
      data-testid="configuration-modal"
    >
      {/* Tabs */}
      {tabs.length > 1 && (
        <div className="flex border-b px-4 overflow-x-auto" style={{ borderColor: 'var(--color-border)' }} role="tablist">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTabId(tab.id)}
              className={`
                relative px-3 py-2.5 text-xs font-medium transition-colors whitespace-nowrap
                ${activeTabId === tab.id
                  ? 'border-b-2 -mb-px'
                  : 'hover:text-gray-700'
                }
              `}
              style={{
                color: activeTabId === tab.id ? 'var(--color-aws-blue)' : 'var(--color-text-secondary)',
                borderColor: activeTabId === tab.id ? 'var(--color-aws-blue)' : 'transparent',
              }}
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
    </ModalShell>
  );
}

export default ConfigurationModal;
