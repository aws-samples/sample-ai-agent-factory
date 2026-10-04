/**
 * AgentGeneratorPanel — Phase 1 Gap 1E.
 *
 * Conversational UI for the NL agent generator. User describes the agent
 * they want; the backend asks 2-4 clarifying questions; then emits a
 * canvas spec (nodes + edges). On "Apply to Canvas" we feed the spec
 * through the existing instantiateTemplate helper and replace the
 * canvas via workflowStore.loadTemplate.
 *
 * Mirrors the visual structure of ToolGeneratorPanel for consistency.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { m } from 'motion/react';
import { spring, tween } from '../../lib/motion';
import {
  generateCanvasApi,
  type AgentGenerateResponse,
  type GeneratedCanvasSpec,
} from '../../services/api';
import { useWorkflowStore } from '../../store/workflowStore';
import { ConfirmDialog } from '../common/ConfirmDialog';
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap';

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  spec?: GeneratedCanvasSpec;
}

export interface AgentGeneratorPanelProps {
  isVisible: boolean;
  onClose: () => void;
  onApplySpec: (spec: GeneratedCanvasSpec) => void;
  hasExistingNodes?: boolean;
}

export function AgentGeneratorPanel({
  isVisible,
  onClose,
  onApplySpec,
  hasExistingNodes,
}: AgentGeneratorPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [isGenerating, setIsGenerating] = useState(false);
  const [currentSpec, setCurrentSpec] = useState<GeneratedCanvasSpec | null>(null);
  const [showApplyConfirm, setShowApplyConfirm] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const focusOriginRef = useRef<HTMLElement | null>(null);
  const wasGeneratingRef = useRef(false);
  const validationState = useWorkflowStore((state) => state.validationState);

  const closePanel = useCallback(() => {
    setShowApplyConfirm(false);
    onClose();
  }, [onClose]);
  useDialogFocusTrap(isVisible, panelRef, inputRef, closePanel);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isGenerating]);

  useEffect(() => {
    if (!isVisible) {
      wasGeneratingRef.current = false;
      focusOriginRef.current = null;
      return;
    }

    if (isGenerating) {
      wasGeneratingRef.current = true;
      return;
    }

    if (!wasGeneratingRef.current || showApplyConfirm) return;
    wasGeneratingRef.current = false;

    const origin = focusOriginRef.current;
    focusOriginRef.current = null;
    const active = document.activeElement;
    if (
      origin &&
      (active === origin ||
        active === document.body ||
        active === inputRef.current ||
        (active instanceof HTMLElement && !active.isConnected))
    ) {
      inputRef.current?.focus();
    }
  }, [isVisible, isGenerating, showApplyConfirm]);

  // Panel is closed via onClose prop — validation results remain visible

  const handleSubmit = useCallback(async () => {
    const trimmed = inputValue.trim();
    if (!trimmed || isGenerating) return;

    focusOriginRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const userMsg: ChatMessage = {
      id: `u-${Date.now()}`,
      role: 'user',
      content: trimmed,
    };
    const nextMessages = [...messages, userMsg];
    setMessages(nextMessages);
    setInputValue('');
    setIsGenerating(true);

    try {
      const conversationHistory = nextMessages.map((m) => ({
        role: m.role,
        content: m.content,
      }));
      const result: AgentGenerateResponse = await generateCanvasApi({
        prompt: trimmed,
        // Send only PRIOR turns; the backend appends the prompt itself.
        conversationHistory: conversationHistory.slice(0, -1),
      });

      if (!result.success) {
        setMessages((prev) => [
          ...prev,
          {
            id: `a-${Date.now()}`,
            role: 'assistant',
            content: `Error: ${result.error ?? 'Generation failed'}`,
          },
        ]);
        return;
      }

      if (result.responseType === 'clarification') {
        setMessages((prev) => [
          ...prev,
          {
            id: `a-${Date.now()}`,
            role: 'assistant',
            content: result.message ?? 'Could you tell me more?',
          },
        ]);
        return;
      }

      if (result.responseType === 'spec' && result.spec) {
        const spec = result.spec;
        setCurrentSpec(spec);
        const summary = formatSpecSummary(spec);
        setMessages((prev) => [
          ...prev,
          {
            id: `a-${Date.now()}`,
            role: 'assistant',
            content: summary,
            spec,
          },
        ]);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setMessages((prev) => [
        ...prev,
        {
          id: `a-${Date.now()}`,
          role: 'assistant',
          content: `Error: ${message}`,
        },
      ]);
    } finally {
      setIsGenerating(false);
    }
  }, [inputValue, isGenerating, messages]);

  const applyCurrentSpec = useCallback(() => {
    if (!currentSpec) return;
    setShowApplyConfirm(false);
    onApplySpec(currentSpec);
  }, [currentSpec, onApplySpec]);

  const handleApply = useCallback(() => {
    if (!currentSpec) return;
    if (hasExistingNodes) {
      setShowApplyConfirm(true);
      return;
    }
    applyCurrentSpec();
    // Panel stays open to show validation results — user can close manually
  }, [currentSpec, hasExistingNodes, applyCurrentSpec]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void handleSubmit();
    }
  };

  if (!isVisible) return null;

  return (
    <>
      <m.div
        className="fixed inset-0 z-40"
        style={{ background: 'rgba(11, 18, 32, 0.28)', backdropFilter: 'blur(2px)' }}
        onClick={closePanel}
        aria-hidden="true"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={tween.base}
      />
      <m.div
        ref={panelRef}
        className="fixed right-0 top-0 bottom-0 w-[460px] bg-white z-50 flex flex-col overflow-hidden border-l border-[#e9ebed]"
        style={{ boxShadow: 'var(--elevation-4)' }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-generator-title"
        tabIndex={-1}
        initial={{ x: '100%' }}
        animate={{ x: 0 }}
        transition={spring.gentle}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#e9ebed] bg-[#232f3e]">
          <div className="flex items-center gap-3">
            <div className="w-7 h-7 rounded-md bg-[#ff9900] flex items-center justify-center text-white">
              ✨
            </div>
            <div>
              <h2 id="agent-generator-title" className="font-semibold text-white text-sm">Generate Agent</h2>
              <p className="text-[11px] text-white/50">
                Describe the agent you want; we'll wire the canvas.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={closePanel}
            className="p-1.5 rounded-md hover:bg-white/10 transition-colors"
            aria-label="Close the agent generator"
          >
            <svg className="w-4 h-4 text-white/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {messages.length === 0 && (
            <div className="text-xs text-gray-500 px-2 py-3 rounded-lg bg-gray-50 border border-gray-200">
              <div className="font-medium mb-1 text-gray-700">Examples</div>
              <ul className="space-y-1 list-disc list-inside">
                <li>
                  "An agent that searches our refund policy KB and escalates angry users."
                </li>
                <li>
                  "A research agent that summarizes daily news from a Confluence space."
                </li>
                <li>
                  "A code review bot with safety guardrails that uses Bedrock."
                </li>
              </ul>
            </div>
          )}
          {messages.map((m) => (
            <div
              key={m.id}
              className={`text-sm rounded-lg px-3 py-2 ${
                m.role === 'user'
                  ? 'bg-[#0972d3] text-white ml-8'
                  : 'bg-gray-50 text-gray-800 mr-8 border border-gray-200'
              }`}
            >
              {m.spec ? (
                <div className="space-y-2">
                  <p className="font-semibold">{m.spec.name}</p>
                  {m.spec.description && <p>{m.spec.description}</p>}
                  <div>
                    <p className="font-medium">Components:</p>
                    <ul className="mt-1 list-disc space-y-0.5 pl-5">
                      {m.spec.nodes.map((node) => (
                        <li key={node.idSuffix}>
                          {node.type}
                          {node.label ? ` (${node.label})` : ''}
                        </li>
                      ))}
                    </ul>
                  </div>
                  {m.spec.rationale && (
                    <p>
                      <span className="font-medium">Why:</span> {m.spec.rationale}
                    </p>
                  )}
                </div>
              ) : (
                <p className="whitespace-pre-wrap">{m.content}</p>
              )}
              {m.spec && (
                <details className="mt-2 text-xs text-gray-700">
                  <summary className="cursor-pointer hover:underline">
                    View raw JSON
                  </summary>
                  <pre
                    role="region"
                    aria-label="Generated agent specification JSON"
                    tabIndex={0}
                    className="mt-1 p-2 bg-white border border-gray-200 rounded text-[10px] overflow-auto max-h-48"
                  >
                    {JSON.stringify(m.spec, null, 2)}
                  </pre>
                </details>
              )}
            </div>
          ))}
          {isGenerating && (
            <div className="text-xs text-gray-500 italic flex items-center gap-2">
              <div className="w-3 h-3 border-2 border-current border-t-transparent rounded-full animate-spin" />
              Thinking…
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Action bar (visible once we have a spec) */}
        {currentSpec && !isGenerating && (
          <div className={`border-t border-[#e9ebed] p-3 ${validationState?.errors && validationState.errors.length > 0 ? 'bg-red-50' : 'bg-emerald-50'}`}>
            <div className="flex items-center justify-between mb-2">
              <div className="text-xs text-emerald-900">
                <span className="font-medium">{currentSpec.name}</span>{' '}
                · {currentSpec.nodes.length} node
                {currentSpec.nodes.length === 1 ? '' : 's'}
              </div>
              <button
                type="button"
                onClick={handleApply}
                className="text-xs px-3 py-1.5 rounded bg-[#047857] text-white hover:bg-[#065f46]"
              >
                Apply to Canvas →
              </button>
            </div>
            {/* Validation feedback — show errors/warnings if present after apply */}
            {validationState && (validationState.errors.length > 0 || validationState.warnings.length > 0) && (
              <div className="mt-2 space-y-1.5">
                {validationState.errors.length > 0 && (
                  <div className="rounded border border-red-300 bg-red-50 px-2.5 py-2">
                    <div className="text-xs font-semibold text-red-800 mb-1">
                      {validationState.errors.length} Error{validationState.errors.length !== 1 ? 's' : ''}
                    </div>
                    <ul className="space-y-0.5 text-[11px] text-red-700">
                      {validationState.errors.slice(0, 5).map((err, i) => (
                        <li key={i} className="flex items-start gap-1.5">
                          <span className="text-red-400 mt-0.5">•</span>
                          <span>
                            <span className="font-medium">{err.componentId}:</span> {err.message}
                          </span>
                        </li>
                      ))}
                      {validationState.errors.length > 5 && (
                        <li className="text-red-600 italic">...and {validationState.errors.length - 5} more</li>
                      )}
                    </ul>
                  </div>
                )}
                {validationState.warnings.length > 0 && (
                  <div className="rounded border border-yellow-300 bg-yellow-50 px-2.5 py-2">
                    <div className="text-xs font-semibold text-yellow-800 mb-1">
                      {validationState.warnings.length} Warning{validationState.warnings.length !== 1 ? 's' : ''}
                    </div>
                    <ul className="space-y-0.5 text-[11px] text-yellow-700">
                      {validationState.warnings.slice(0, 3).map((warn, i) => (
                        <li key={i} className="flex items-start gap-1.5">
                          <span className="text-yellow-400 mt-0.5">•</span>
                          <span>
                            <span className="font-medium">{warn.componentId}:</span> {warn.message}
                          </span>
                        </li>
                      ))}
                      {validationState.warnings.length > 3 && (
                        <li className="text-yellow-600 italic">...and {validationState.warnings.length - 3} more</li>
                      )}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Input */}
        <div className="border-t border-[#e9ebed] bg-[#fafafa] p-3 flex-shrink-0">
          <div className="flex gap-2">
            <textarea
              ref={inputRef}
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Describe the agent you want…"
              aria-label="Agent description"
              className="flex-1 resize-none rounded-xl border border-[#e9ebed] px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-[#0972d3] focus:border-transparent bg-white"
              rows={2}
              disabled={isGenerating}
            />
            <button
              onClick={() => void handleSubmit()}
              disabled={!inputValue.trim() || isGenerating}
              className={`self-end p-2.5 rounded-xl transition-all ${
                inputValue.trim() && !isGenerating
                  ? 'bg-[#0972d3] text-white hover:bg-[#0961b9]'
                  : 'bg-[#e9ebed] text-[#8d99a8] cursor-not-allowed'
              }`}
              aria-label="Send"
            >
              <svg
                className="w-5 h-5"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M22 2L11 13" />
                <path d="M22 2l-7 20-4-9-9-4 20-7z" />
              </svg>
            </button>
          </div>
        </div>
      </m.div>
      <ConfirmDialog
        isOpen={showApplyConfirm}
        title="Replace current workflow?"
        message="Applying this generated agent will replace every component currently on the canvas."
        confirmLabel="Replace workflow"
        variant="danger"
        onConfirm={applyCurrentSpec}
        onCancel={() => setShowApplyConfirm(false)}
      />
    </>
  );
}

function formatSpecSummary(spec: GeneratedCanvasSpec): string {
  const components = spec.nodes
    .map((n) => `  • ${n.type}${n.label ? ` (${n.label})` : ''}`)
    .join('\n');
  let txt = `**${spec.name}**\n`;
  if (spec.description) txt += `${spec.description}\n\n`;
  txt += `Components:\n${components}\n`;
  if (spec.rationale) txt += `\nWhy: ${spec.rationale}`;
  return txt;
}
