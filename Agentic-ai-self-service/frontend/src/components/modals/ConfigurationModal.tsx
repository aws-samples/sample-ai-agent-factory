/**
 * Base ConfigurationModal component with tabbed interface.
 * Requirements: 3.1
 */

import { useState, useCallback, useId, type ReactNode } from 'react';
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
  isSaveDisabled?: boolean;
  notice?: ReactNode;
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
  isSaveDisabled = false,
  notice,
}: ConfigurationModalProps) {
  const [activeTabId, setActiveTabId] = useState<string>(() => tabs[0]?.id ?? '');
  const tabIdPrefix = useId();

  // Reset to first tab when modal opens (adjust state during render pattern)
  const [lastIsOpen, setLastIsOpen] = useState(isOpen);
  if (isOpen !== lastIsOpen) {
    setLastIsOpen(isOpen);
    if (isOpen) {
      setActiveTabId(tabs[0]?.id ?? '');
    }
  }

  const saveDisabled =
    validationErrors.length > 0 || isSaving || isSaveDisabled;

  const handleSave = useCallback(() => {
    if (!saveDisabled) {
      onSave();
    }
  }, [onSave, saveDisabled]);

  const handleTabKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLButtonElement>, currentTabId: string) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;

      const currentIndex = Math.max(
        0,
        tabs.findIndex((tab) => tab.id === currentTabId),
      );
      let nextIndex = currentIndex;
      if (event.key === 'Home') nextIndex = 0;
      if (event.key === 'End') nextIndex = tabs.length - 1;
      if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % tabs.length;
      if (event.key === 'ArrowLeft') {
        nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
      }

      const nextTab = tabs[nextIndex];
      if (!nextTab) return;
      event.preventDefault();
      setActiveTabId(nextTab.id);
      document.getElementById(`${tabIdPrefix}-tab-${nextTab.id}`)?.focus();
    },
    [tabIdPrefix, tabs],
  );

  const activeTab = tabs.find((tab) => tab.id === activeTabId);
  const hasErrors = validationErrors.length > 0;

  const footer = (
    <>
      <button
        type="button"
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
        type="button"
        onClick={handleSave}
        disabled={saveDisabled}
        className={`px-4 py-2 text-sm font-medium transition-colors ${
          saveDisabled ? 'cursor-not-allowed' : ''
        }`}
        style={{
          color: 'var(--accent-foreground)',
          backgroundColor: saveDisabled ? '#93c5fd' : 'var(--color-aws-blue)',
          borderRadius: 'var(--radius-control)',
        }}
        data-testid="modal-save-button"
      >
        {isSaving ? (
          <span className="flex items-center gap-2">
            <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24" aria-hidden="true">
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
        <div
          className="flex border-b px-4 overflow-x-auto"
          style={{ borderColor: 'var(--color-border)' }}
          role="tablist"
          aria-label={`${title} sections`}
        >
          {tabs.map((tab) => (
            <button
              key={tab.id}
              id={`${tabIdPrefix}-tab-${tab.id}`}
              type="button"
              onClick={() => setActiveTabId(tab.id)}
              onKeyDown={(event) => handleTabKeyDown(event, tab.id)}
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
              aria-controls={`${tabIdPrefix}-tabpanel-${tab.id}`}
              tabIndex={activeTabId === tab.id ? 0 : -1}
              data-testid={`tab-${tab.id}`}
            >
              {tab.label}
              {tab.hasError && (
                <>
                  <span
                    className="absolute -top-1 -right-1 w-2 h-2 bg-red-500 rounded-full"
                    aria-hidden="true"
                  />
                  <span className="sr-only"> — contains validation errors</span>
                </>
              )}
            </button>
          ))}
        </div>
      )}

      {notice && <div className="px-5 pt-4">{notice}</div>}

      {/* Content */}
      <div
        className="overflow-y-auto p-5"
        style={{ height: 'var(--modal-content-height, 360px)' }}
        role={tabs.length > 1 ? 'tabpanel' : undefined}
        id={tabs.length > 1 ? `${tabIdPrefix}-tabpanel-${activeTabId}` : undefined}
        aria-labelledby={
          tabs.length > 1 ? `${tabIdPrefix}-tab-${activeTabId}` : undefined
        }
        tabIndex={tabs.length > 1 ? 0 : undefined}
      >
        {activeTab?.content}
      </div>

      {/* Validation Errors Summary */}
      {hasErrors && (
        <div className="px-5 py-3 bg-red-50 border-t border-red-200" role="alert">
          <div className="flex items-start gap-2">
            <svg className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
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
