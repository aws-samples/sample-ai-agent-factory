/**
 * CanvasArea - main canvas view with overlays (empty state, selected node, errors).
 * Extracted from App.tsx for better separation of concerns.
 */

import { m } from 'motion/react';
import { staggerContainer, fadeRise, pressable } from '../../lib/motion';
import WorkflowCanvas from './WorkflowCanvas';
import { ActiveDeploymentBanner } from '../deploy/ActiveDeploymentBanner';
import type { ActiveDeployment } from '../deploy/ActiveDeploymentBanner';
import type { AgentCoreNode } from '../../store/workflowStore';
import type { FlowSaveConflict, SaveConflictResolution } from '../../store/flowStore';
import { configurationTargetForExistingNode } from '../modals/modalRegistry';

interface CanvasAreaProps {
  nodes: AgentCoreNode[];
  selectedNode: AgentCoreNode | null;
  lastSaveError: string | null;
  /**
   * A save was refused because the flow changed elsewhere (another tab or
   * session). Neither side has been lost; the user picks which one wins.
   */
  saveConflict?: FlowSaveConflict | null;
  onResolveSaveConflict?: (choice: SaveConflictResolution) => void;
  onNodeCreate: (node: AgentCoreNode) => void;
  onNodeDoubleClick: (nodeId: string) => void;
  onRestoreDeployment: (deployment: ActiveDeployment) => void;
  onClearSaveError: () => void;
  onOpenTemplateGallery: () => void;
  onOpenAgentGenerator: () => void;
  onOpenConfig: (nodeId: string) => void;
  /**
   * When set, the empty-state actions are disabled and the text is announced as
   * the reason. The App sets it until a flow is active; content placed on a
   * canvas that no flow owns cannot be saved and would be replaced by the
   * sidebar's auto-open.
   */
  authoringDisabledReason?: string | null;
}

export function CanvasArea({
  nodes,
  selectedNode,
  lastSaveError,
  saveConflict = null,
  onResolveSaveConflict,
  onNodeCreate,
  onNodeDoubleClick,
  onRestoreDeployment,
  onClearSaveError,
  onOpenTemplateGallery,
  onOpenAgentGenerator,
  onOpenConfig,
  authoringDisabledReason = null,
}: CanvasAreaProps) {
  const authoringDisabled = authoringDisabledReason !== null;
  const selectedNodeCanConfigure =
    selectedNode !== null &&
    configurationTargetForExistingNode(selectedNode) !== null;

  return (
    <div className="flex-1 relative">
      <WorkflowCanvas
        onNodeCreate={onNodeCreate}
        onNodeDoubleClick={onNodeDoubleClick}
      />

      <ActiveDeploymentBanner onRestore={onRestoreDeployment} />

      {/* Save conflict: the server holds a newer version of this flow (F-15) */}
      {saveConflict && (
        <div
          data-testid="autosave-conflict-toast"
          role="alertdialog"
          aria-labelledby="autosave-conflict-title"
          aria-describedby="autosave-conflict-body"
          className="absolute bottom-4 right-4 z-40 max-w-sm rounded-md border border-amber-300 bg-amber-50 shadow-md"
        >
          <div className="px-3 py-2.5">
            <div id="autosave-conflict-title" className="text-[13px] font-semibold text-amber-900">
              This flow was changed elsewhere
            </div>
            <div id="autosave-conflict-body" className="text-[12px] text-amber-800 mt-0.5 break-words">
              Another tab or session saved a newer version, so your latest changes were not saved.
              Reload to see that version (your unsaved changes here are discarded), or keep yours and
              overwrite it.
            </div>
            <div className="mt-2 flex gap-2">
              <button
                type="button"
                onClick={() => onResolveSaveConflict?.('reload')}
                className="rounded-md border border-amber-400 bg-white px-2.5 py-1 text-[12px] font-medium text-amber-900 hover:bg-amber-100"
              >
                Reload their version
              </button>
              <button
                type="button"
                onClick={() => onResolveSaveConflict?.('keep_mine')}
                className="rounded-md bg-amber-600 px-2.5 py-1 text-[12px] font-medium text-white hover:bg-amber-700"
              >
                Keep mine and overwrite
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Auto-save error toast */}
      {lastSaveError && !saveConflict && (
        <div
          data-testid="autosave-error-toast"
          role="alert"
          className="absolute bottom-4 right-4 z-40 max-w-sm rounded-md border border-red-300 bg-red-50 shadow-md"
        >
          <div className="flex items-start gap-2 px-3 py-2.5">
            <svg
              className="mt-0.5 h-4 w-4 shrink-0 text-red-500"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
              aria-hidden="true"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z"
              />
            </svg>
            <div className="flex-1 min-w-0">
              <div className="text-[13px] font-semibold text-red-800">
                Auto-save failed
              </div>
              <div className="text-[12px] text-red-700 mt-0.5 break-words">
                Your recent changes have not been saved. Check your connection and try again.
              </div>
            </div>
            <button
              type="button"
              onClick={onClearSaveError}
              aria-label="Dismiss auto-save error"
              className="-mr-1 -mt-1 rounded p-1 text-red-500 hover:bg-red-100 hover:text-red-700"
            >
              <svg
                className="h-3.5 w-3.5"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2.5}
                aria-hidden="true"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
      )}

      {/* Selected Node Info Card */}
      {selectedNode && (
        <div
          className="absolute bottom-4 left-4 z-30 bg-white rounded-xl border border-[#e9ebed] p-4 min-w-[240px] transition-transform duration-200"
          style={{
            boxShadow: 'var(--shadow-md)',
            transitionTimingFunction: 'var(--ease-out-quint)',
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.transform = 'scale(1.01)';
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.transform = 'scale(1)';
          }}
        >
          <div className="flex items-start gap-3">
            <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-[#232f3e] to-[#16191f] flex items-center justify-center text-white text-base flex-shrink-0 shadow-sm">
              {selectedNode.data.componentType === 'runtime' ? '🤖' :
               selectedNode.data.componentType === 'gateway' ? '🔌' :
               selectedNode.data.componentType === 'memory' ? '🧠' :
               selectedNode.data.componentType === 'code_interpreter' ? '💻' :
               selectedNode.data.componentType === 'browser' ? '🌐' :
               selectedNode.data.componentType === 'observability' ? '📊' :
               selectedNode.data.componentType === 'tool' ? '🔧' : '🔑'}
            </div>
            <div className="flex-1 min-w-0">
              <div className="font-medium text-[#16191f] text-sm truncate tracking-tight">
                {selectedNode.data.label || selectedNode.data.componentType}
              </div>
              <div className="text-xs text-[#5f6b7a] capitalize mt-1 font-light">
                {selectedNode.data.componentType.replace(/_/g, ' ')}
              </div>
            </div>
          </div>
          {selectedNodeCanConfigure && (
            <button
              onClick={() => onOpenConfig(selectedNode.id)}
            className="mt-3 w-full py-2 px-3 text-sm text-[#0972d3] hover:bg-[#0972d3]/8 active:bg-[#0972d3]/12 rounded-lg transition-colors duration-200 font-medium flex items-center justify-center gap-2 border border-[#0972d3]/25 hover:border-[#0972d3]/40"
            style={{ transitionTimingFunction: 'var(--ease-out-quint)' }}
            aria-label={`Configure ${selectedNode.data.label || selectedNode.data.componentType}`}
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
              Configure
            </button>
          )}
        </div>
      )}

      {/* Empty state */}
      {nodes.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
          <div
            className="absolute pointer-events-none"
            style={{
              width: 720, height: 440,
              backgroundImage:
                'radial-gradient(40% 50% at 38% 42%, rgba(34,211,238,0.16), transparent 70%), radial-gradient(40% 50% at 64% 55%, rgba(167,139,250,0.16), transparent 70%)',
              filter: 'blur(10px)',
            }}
          />
          <m.div
            className="relative text-center max-w-xl px-4"
            variants={staggerContainer(0.09, 0.05)}
            initial="hidden"
            animate="visible"
          >
            <m.div variants={fadeRise} className="inline-flex mb-6">
              <div
                className="no-darkmap flex items-center gap-2 rounded-full px-3 py-1 text-sm font-medium tracking-tight"
                style={{
                  background: 'var(--glass-bg)',
                  backdropFilter: 'blur(10px)',
                  border: '1px solid var(--glass-border)',
                  color: 'var(--color-text-secondary)',
                  boxShadow: '0 0 20px -8px var(--neon-cyan)',
                }}
              >
                <span className="relative flex h-1.5 w-1.5">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-75" style={{ background: 'var(--neon-cyan)' }} />
                  <span className="relative inline-flex h-1.5 w-1.5 rounded-full" style={{ background: 'var(--neon-cyan)' }} />
                </span>
                Visual Workflow Builder
              </div>
            </m.div>

            <m.h2
              variants={fadeRise}
              className="no-darkmap text-4xl sm:text-5xl md:text-6xl mb-3 leading-tight u-neon-text u-gradient-anim"
              style={{ fontFamily: 'var(--font-accent)', fontStyle: 'italic', fontWeight: 400 }}
            >
              Build Your First Agent
            </m.h2>
            <m.p
              variants={fadeRise}
              className="no-darkmap text-base sm:text-lg mb-8 font-light tracking-tight leading-relaxed"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              Drag components from the sidebar, start with a template, or let AI generate an agent for you.
            </m.p>
            {authoringDisabled && (
              <m.p
                variants={fadeRise}
                role="status"
                data-testid="authoring-disabled-reason"
                className="no-darkmap text-sm mb-6 font-medium tracking-tight"
                style={{ color: 'var(--color-text-secondary)' }}
              >
                {authoringDisabledReason}
              </m.p>
            )}

            <m.div variants={fadeRise} className="flex gap-3 justify-center">
              <m.button
                {...pressable}
                onClick={onOpenTemplateGallery}
                disabled={authoringDisabled}
                className="no-darkmap pointer-events-auto px-5 py-2.5 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-60"
                style={{
                  background: 'var(--accent)',
                  color: 'var(--accent-foreground)',
                  borderRadius: '2px',
                  boxShadow: '0 0 22px -6px var(--neon-cyan)',
                }}
              >
                Browse Templates
              </m.button>
              <m.button
                {...pressable}
                onClick={onOpenAgentGenerator}
                disabled={authoringDisabled}
                className="no-darkmap pointer-events-auto px-5 py-2.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-60"
                style={{
                  background: 'var(--glass-bg)',
                  backdropFilter: 'blur(10px)',
                  color: 'var(--accent)',
                  border: '1px solid color-mix(in srgb, var(--accent) 40%, transparent)',
                  borderRadius: '2px',
                }}
              >
                Generate with AI
              </m.button>
            </m.div>
          </m.div>
        </div>
      )}
    </div>
  );
}
