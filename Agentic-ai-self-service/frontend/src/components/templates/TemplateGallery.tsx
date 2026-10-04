/**
 * TemplateGallery - Modal displaying prebuilt workflow templates.
 *
 * This modal hand-rolls its own scrim and panel rather than using `ModalShell`, whose
 * docstring says it "Enforces consistent modal UX across the app". Measured against
 * the deployed bundle, it therefore had none of what ModalShell supplies: Escape did
 * not dismiss it, nothing carried `role="dialog"` or `aria-modal`, and the close
 * button had no text, no `aria-label` and no `title`, so its accessible name was the
 * empty string. A keyboard-only user who opened the gallery had no way out, and a
 * screen-reader user was offered an unnamed button.
 *
 * Dismissal, naming, focus containment, and the replace-workflow confirmation all use
 * the shared accessible dialog primitives below.
 */

import { useCallback, useRef, useState } from 'react';
import { m } from 'motion/react';
import { popIn, tween } from '../../lib/motion';
import type { WorkflowTemplate, TemplateDifficulty } from '../../types/templates';
import { WORKFLOW_TEMPLATES } from '../../data/templates';
import { ConfirmDialog } from '../common/ConfirmDialog';
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap';

// ============================================================================
// Props
// ============================================================================

export interface TemplateGalleryProps {
  isOpen: boolean;
  onClose: () => void;
  onSelectTemplate: (template: WorkflowTemplate) => void;
  hasExistingNodes: boolean;
}

// ============================================================================
// Difficulty Badge
// ============================================================================

const DIFFICULTY_STYLES: Record<TemplateDifficulty, { bg: string; text: string; label: string }> = {
  beginner: { bg: 'bg-green-100', text: 'text-green-700', label: 'Beginner' },
  intermediate: { bg: 'bg-yellow-100', text: 'text-yellow-700', label: 'Intermediate' },
  advanced: { bg: 'bg-red-100', text: 'text-red-700', label: 'Advanced' },
};

function DifficultyBadge({ difficulty }: { difficulty: TemplateDifficulty }) {
  const style = DIFFICULTY_STYLES[difficulty];
  return (
    <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold uppercase tracking-wide ${style.bg} ${style.text}`}>
      {style.label}
    </span>
  );
}

// ============================================================================
// Component Type Pills
// ============================================================================

const COMPONENT_LABELS: Record<string, string> = {
  runtime: 'Runtime',
  gateway: 'Gateway',
  memory: 'Memory',
  identity: 'Identity',
  observability: 'Observability',
};

// ============================================================================
// TemplateGallery Component
// ============================================================================

export function TemplateGallery({ isOpen, onClose, onSelectTemplate, hasExistingNodes }: TemplateGalleryProps) {
  const [pendingTemplate, setPendingTemplate] = useState<WorkflowTemplate | null>(null);
  const [lastIsOpen, setLastIsOpen] = useState(isOpen);
  const dialogRef = useRef<HTMLDivElement>(null);

  if (isOpen !== lastIsOpen) {
    setLastIsOpen(isOpen);
    if (!isOpen && pendingTemplate) setPendingTemplate(null);
  }

  const closeGallery = useCallback(() => {
    setPendingTemplate(null);
    onClose();
  }, [onClose]);
  useDialogFocusTrap(isOpen, dialogRef, undefined, closeGallery);

  const selectAndClose = useCallback(
    (template: WorkflowTemplate) => {
      setPendingTemplate(null);
      onSelectTemplate(template);
      onClose();
    },
    [onSelectTemplate, onClose],
  );

  const handleSelect = useCallback(
    (template: WorkflowTemplate) => {
      if (hasExistingNodes) {
        setPendingTemplate(template);
        return;
      }
      selectAndClose(template);
    },
    [hasExistingNodes, selectAndClose],
  );

  if (!isOpen) return null;

  return (
    <>
      {/* Backdrop */}
      <m.div
        className="fixed inset-0 z-40"
        style={{ background: 'rgba(11, 18, 32, 0.44)', backdropFilter: 'blur(3px)' }}
        onClick={closeGallery}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={tween.base}
      />

      {/* Modal Panel */}
      <div className="fixed inset-0 z-50 flex items-center justify-center p-8 pointer-events-none">
        <m.div
          className="pointer-events-auto bg-white rounded-xl w-full max-w-3xl max-h-[80vh] flex flex-col overflow-hidden border border-[#e9ebed]"
          style={{ boxShadow: 'var(--elevation-4)' }}
          role="dialog"
          aria-modal="true"
          aria-labelledby="template-gallery-title"
          ref={dialogRef}
          tabIndex={-1}
          variants={popIn}
          initial="hidden"
          animate="visible"
        >
          {/* Header */}
          <div className="flex items-center justify-between px-6 py-3.5 border-b border-[#e9ebed] bg-[#232f3e]">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-md bg-[#ff9900] flex items-center justify-center">
                <svg className="w-4 h-4 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18" /><path d="M9 21V9" />
                </svg>
              </div>
              <div>
                <h2 id="template-gallery-title" className="font-semibold text-white text-sm">Workflow Templates</h2>
                <p className="text-[11px] text-white/50">Start with a pre-configured workflow</p>
              </div>
            </div>
            <button
              type="button"
              onClick={closeGallery}
              aria-label="Close the workflow templates gallery"
              className="p-1.5 rounded-md hover:bg-white/10 transition-colors"
            >
              <svg className="w-4 h-4 text-white/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* Template Cards */}
          <div className="flex-1 overflow-y-auto p-6 space-y-4">
            {WORKFLOW_TEMPLATES.map((template) => (
              <div
                key={template.id}
                className="rounded-lg border border-[#e9ebed] hover:border-[#0972d3]/40 hover:shadow-md transition-all overflow-hidden"
              >
                <div className="p-5">
                  <div className="flex items-start justify-between gap-4">
                    {/* Left: Info */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-xl">{template.icon}</span>
                        <h3 className="font-semibold text-gray-900">{template.name}</h3>
                        <DifficultyBadge difficulty={template.difficulty} />
                      </div>
                      <p className="text-sm text-gray-600 mb-3">{template.longDescription}</p>

                      {/* Built-in Tools */}
                      {template.builtInTools.length > 0 && (
                        <div className="mb-3 p-2.5 bg-slate-50 rounded-lg border border-slate-200">
                          <div
                            className="text-[10px] uppercase tracking-wide font-semibold mb-1.5"
                            style={{ color: 'var(--color-text-secondary)' }}
                          >
                            Built-in Tools
                          </div>
                          <div className="flex flex-wrap gap-1.5">
                            {template.builtInTools.map((tool) => (
                              <span
                                key={tool.name}
                                className="inline-flex items-center gap-1 px-2 py-1 border rounded-md text-[11px] font-medium"
                                style={{
                                  color: 'var(--color-text-primary)',
                                  background: 'var(--color-surface)',
                                  borderColor: 'var(--color-border)',
                                }}
                                title={tool.description}
                              >
                                <span>{tool.icon}</span>
                                {tool.name}
                              </span>
                            ))}
                          </div>
                        </div>
                      )}

                      {/* Component type pills */}
                      <div className="flex flex-wrap gap-1.5">
                        {template.componentTypes.map((type) => (
                          <span
                            key={type}
                            className="px-2 py-0.5 bg-gray-100 text-gray-600 rounded-md text-[11px] font-medium"
                          >
                            {COMPONENT_LABELS[type] || type}
                          </span>
                        ))}
                        <span className="px-2 py-0.5 bg-blue-50 text-blue-600 rounded-md text-[11px] font-medium">
                          {template.nodes.length} node{template.nodes.length !== 1 ? 's' : ''}
                        </span>
                        {template.edges.length > 0 && (
                          <span className="px-2 py-0.5 bg-blue-50 text-blue-600 rounded-md text-[11px] font-medium">
                            {template.edges.length} connection{template.edges.length !== 1 ? 's' : ''}
                          </span>
                        )}
                      </div>
                    </div>

                    {/* Right: Action */}
                    <button
                      onClick={() => handleSelect(template)}
                      className="flex-shrink-0 px-4 py-2 bg-[#0972d3] text-white rounded-md font-medium text-sm hover:bg-[#0961b9] transition-colors"
                    >
                      Use Template
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Footer */}
          <div className="px-6 py-2.5 border-t border-[#e9ebed] bg-[#fafafa]">
            <p className="text-[10px] text-center" style={{ color: 'var(--color-text-secondary)' }}>
              Templates are fully customizable — double-click any node to edit its configuration
            </p>
          </div>
        </m.div>
      </div>

      <ConfirmDialog
        isOpen={pendingTemplate !== null}
        title="Replace current workflow?"
        message="This will replace every node and connection in your current workflow."
        confirmLabel="Replace workflow"
        cancelLabel="Keep current workflow"
        variant="danger"
        onConfirm={() => {
          if (pendingTemplate) selectAndClose(pendingTemplate);
        }}
        onCancel={() => setPendingTemplate(null)}
      />
    </>
  );
}

export default TemplateGallery;
