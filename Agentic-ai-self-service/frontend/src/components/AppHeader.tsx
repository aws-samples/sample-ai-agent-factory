/**
 * AppHeader - main application toolbar.
 * Extracted from App.tsx for better separation of concerns.
 */

import { useEffect, useState } from 'react';
import { m } from 'motion/react';
import { spring } from '../lib/motion';
import { ThemeToggle } from './ThemeToggle';
import { signOutAfterFlush } from '../auth/signOutAfterFlush';
import type { RuntimeConfiguration } from '../types/components';

interface AppHeaderProps {
  activeFlowName: string | null;
  nodesCount: number;
  deployableConfig: RuntimeConfiguration | undefined;
  /** The canvas validator's verdict: the badge must not say "Ready" over a red canvas. */
  isReadyToDeploy: boolean;
  validationErrorCount: number;
  authoringMode: 'visual' | 'harness';
  onAuthoringModeChange: (mode: 'visual' | 'harness') => void;
  onDeploy: () => void;
  onOpenRegistry: () => void;
  onPreviewAsEndUser: () => void;
  onOpenHitlInbox: () => void;
  canDeploy: boolean;
  canOpenRegistry: boolean;
  showAdminControls: boolean;
  canOpenHitlInbox: boolean;
}

export function AppHeader({
  activeFlowName,
  nodesCount,
  deployableConfig,
  isReadyToDeploy,
  validationErrorCount,
  authoringMode,
  onAuthoringModeChange,
  onDeploy,
  onOpenRegistry,
  onPreviewAsEndUser,
  onOpenHitlInbox,
  canDeploy,
  canOpenRegistry,
  showAdminControls,
  canOpenHitlInbox,
}: AppHeaderProps) {
  const [focusedAuthoringMode, setFocusedAuthoringMode] = useState(authoringMode);

  useEffect(() => {
    setFocusedAuthoringMode(authoringMode);
  }, [authoringMode]);

  const activateAuthoringMode = (mode: 'visual' | 'harness') => {
    setFocusedAuthoringMode(mode);
    onAuthoringModeChange(mode);
    // App renders the two authoring surfaces through different branches, so
    // activation can replace the header node that currently owns focus.
    window.requestAnimationFrame(() => {
      document.getElementById(`authoring-mode-${mode}`)?.focus();
    });
  };

  const handleAuthoringModeKeyDown = (
    event: React.KeyboardEvent<HTMLButtonElement>,
    currentMode: 'visual' | 'harness',
  ) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      activateAuthoringMode(currentMode);
      return;
    }

    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      return;
    }

    event.preventDefault();
    const nextMode =
      event.key === 'ArrowLeft' || event.key === 'Home'
        ? 'visual'
        : event.key === 'ArrowRight' || event.key === 'End'
          ? 'harness'
          : currentMode;
    setFocusedAuthoringMode(nextMode);
    window.requestAnimationFrame(() => {
      document.getElementById(`authoring-mode-${nextMode}`)?.focus();
    });
  };

  // Authoring mode toggle segment
  const authoringToggle = (
    <div className="no-darkmap flex items-center gap-0.5 p-0.5 backdrop-blur-sm rounded-md" style={{ background: 'rgba(255,255,255,0.08)' }} role="tablist" aria-label="Authoring mode">
      <button
        id="authoring-mode-visual"
        type="button"
        role="tab"
        aria-selected={authoringMode === 'visual'}
        aria-controls="authoring-panel-visual"
        tabIndex={focusedAuthoringMode === 'visual' ? 0 : -1}
        onFocus={() => setFocusedAuthoringMode('visual')}
        onClick={() => activateAuthoringMode('visual')}
        onKeyDown={(event) => handleAuthoringModeKeyDown(event, 'visual')}
        className="no-darkmap px-2.5 py-1 rounded text-xs font-semibold transition-colors duration-200"
        style={{
          transitionTimingFunction: 'var(--ease-out-quint)',
          background: authoringMode === 'visual' ? 'var(--accent)' : 'transparent',
          color: authoringMode === 'visual' ? 'var(--accent-foreground)' : 'rgba(255,255,255,0.88)',
          boxShadow: authoringMode === 'visual' ? '0 0 14px -4px var(--accent)' : 'none',
        }}
      >
        Visual Canvas
      </button>
      <button
        id="authoring-mode-harness"
        type="button"
        role="tab"
        aria-selected={authoringMode === 'harness'}
        aria-controls="authoring-panel-harness"
        tabIndex={focusedAuthoringMode === 'harness' ? 0 : -1}
        onFocus={() => setFocusedAuthoringMode('harness')}
        onClick={() => activateAuthoringMode('harness')}
        onKeyDown={(event) => handleAuthoringModeKeyDown(event, 'harness')}
        className="no-darkmap px-2.5 py-1 rounded text-xs font-semibold transition-colors duration-200"
        style={{
          transitionTimingFunction: 'var(--ease-out-quint)',
          background: authoringMode === 'harness' ? 'var(--accent)' : 'transparent',
          color: authoringMode === 'harness' ? 'var(--accent-foreground)' : 'rgba(255,255,255,0.88)',
          boxShadow: authoringMode === 'harness' ? '0 0 14px -4px var(--accent)' : 'none',
        }}
      >
        Harness
      </button>
    </div>
  );

  return (
    <header
      className="no-darkmap h-12 flex items-center justify-between px-4 z-20 relative"
      style={{
        background: 'var(--header-bg)',
        backdropFilter: 'blur(12px)',
        borderBottom: '1px solid var(--color-border)',
        boxShadow: '0 1px 0 var(--header-hairline)',
      }}
    >
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2.5">
          <div
            className="w-7 h-7 rounded-md flex items-center justify-center"
            style={{
              background: 'linear-gradient(135deg, var(--neon-cyan), var(--neon-violet))',
              boxShadow: '0 0 14px -2px var(--neon-cyan)',
            }}
          >
            <svg className="w-4 h-4 text-[#06080f]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
            </svg>
          </div>
          <h1
            className="font-semibold text-sm tracking-tight"
            style={{ color: 'var(--header-fg)' }}
          >
            AgentCore Flows
          </h1>
        </div>
        <div className="h-5 w-px bg-white/25" />
        <span className="font-medium text-white/95 text-sm">
          {activeFlowName || 'Untitled Flow'}
        </span>
        <div className="h-5 w-px bg-white/25" />
        <div className="flex items-center gap-2 text-xs text-white/80">
          <span className="px-2 py-0.5 backdrop-blur-sm rounded font-normal" style={{ backgroundColor: 'rgba(255, 255, 255, 0.14)' }}>{nodesCount} node{nodesCount !== 1 ? 's' : ''}</span>
        </div>
        <div className="h-5 w-px bg-white/20" />
        {authoringToggle}
      </div>

      <div className="flex items-center gap-3">
        {deployableConfig && isReadyToDeploy && (
          <m.div
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={spring.bouncy}
            className="no-darkmap flex items-center gap-1.5 px-2.5 py-1 backdrop-blur-sm rounded-md text-xs font-semibold border" style={{ backgroundColor: 'rgba(52, 211, 153, 0.22)', color: '#6ee7b7', borderColor: 'rgba(52, 211, 153, 0.5)', boxShadow: '0 0 14px -4px rgba(52,211,153,0.7)' }}
          >
            <div className="w-1.5 h-1.5 rounded-full animate-pulse" style={{ background: '#34d399', boxShadow: '0 0 8px #34d399' }} />
            Ready to deploy
          </m.div>
        )}
        {deployableConfig && !isReadyToDeploy && (
          <m.div
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={spring.bouncy}
            role="status"
            data-testid="canvas-validation-badge"
            className="no-darkmap flex items-center gap-1.5 px-2.5 py-1 backdrop-blur-sm rounded-md text-xs font-semibold border"
            style={{ backgroundColor: 'rgba(245, 158, 11, 0.12)', borderColor: 'rgba(245, 158, 11, 0.5)', color: '#fbbf24' }}
          >
            <div className="w-1.5 h-1.5 rounded-full" style={{ background: '#fbbf24', boxShadow: '0 0 8px #fbbf24' }} />
            {validationErrorCount === 1
              ? '1 validation error'
              : `${validationErrorCount} validation errors`}
          </m.div>
        )}

        {canOpenRegistry && (
          <button
            onClick={onOpenRegistry}
            className="px-3 py-1.5 rounded-md text-sm text-white/85 hover:text-white hover:bg-white/10 transition-colors duration-200 flex items-center gap-1.5"
            style={{ transitionTimingFunction: 'var(--ease-out-quint)' }}
            title="Browse the agent registry"
            aria-label="Browse agent registry"
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
            </svg>
            Registry
          </button>
        )}

        {showAdminControls && (
          <button
            onClick={onPreviewAsEndUser}
            className="px-3 py-1.5 rounded-md text-sm text-white/85 hover:text-white hover:bg-white/10 transition-colors duration-200 flex items-center gap-1.5"
            style={{ transitionTimingFunction: 'var(--ease-out-quint)' }}
            title="Preview the end-user chat experience"
            aria-label="View as end-user"
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z" /><circle cx="12" cy="12" r="3" />
            </svg>
            View as user
          </button>
        )}

        {canOpenHitlInbox && (
          <button
            onClick={onOpenHitlInbox}
            className="px-3 py-1.5 rounded-md text-sm text-white/85 hover:text-white hover:bg-white/10 transition-colors duration-200 flex items-center gap-1.5"
            style={{ transitionTimingFunction: 'var(--ease-out-quint)' }}
            title="Human-in-the-loop approvals"
            aria-label="Human-in-the-loop approvals inbox"
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" />
            </svg>
            Approvals
          </button>
        )}

        <m.button
          // The shipped deploy control. `deploy-button` previously existed only on
          // DeployButton.tsx, which nothing rendered, so every UI test that reached
          // for it waited on an element that was never in the DOM while a working
          // Deploy button sat right here.
          data-testid="deploy-button"
          onClick={onDeploy}
          disabled={!canDeploy}
          whileHover={canDeploy ? { scale: 1.03 } : undefined}
          whileTap={canDeploy ? { scale: 0.96 } : undefined}
          transition={spring.snappy}
          className={`
            no-darkmap relative overflow-hidden px-4 py-1.5 font-semibold flex items-center gap-2 text-sm
            ${canDeploy ? 'text-[#06080f]' : 'text-white/30 cursor-not-allowed'}
            ${canDeploy ? 'u-gradient-anim' : ''}
          `}
          style={{
            background: canDeploy
              ? 'linear-gradient(90deg, var(--neon-cyan), var(--neon-violet), var(--neon-magenta))'
              : 'rgba(255,255,255,0.06)',
            borderRadius: '2px',
            boxShadow: canDeploy ? '0 0 18px -4px var(--neon-cyan)' : 'none',
          }}
          title={!canDeploy ? 'Configure a Runtime node first' : 'Deploy to AgentCore'}
          aria-label={!canDeploy ? 'Configure a Runtime node first' : 'Deploy agent to AgentCore'}
        >
          {canDeploy && (
            <span
              className="pointer-events-none absolute inset-y-0 -left-1/3 w-1/3 opacity-0 group-hover:opacity-100"
              style={{ background: 'linear-gradient(90deg, transparent, rgba(255,255,255,0.7), transparent)' }}
            />
          )}
          <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7z" />
          </svg>
          Deploy
        </m.button>
        <ThemeToggle />
        <button
          onClick={() => { void signOutAfterFlush(); }}
          className="px-3 py-1.5 rounded-md text-sm text-white/80 hover:text-white hover:bg-white/10 transition-colors duration-200"
          style={{ transitionTimingFunction: 'var(--ease-out-quint)' }}
          title="Sign out"
          aria-label="Sign out"
        >
          Sign out
        </button>
      </div>
    </header>
  );
}
