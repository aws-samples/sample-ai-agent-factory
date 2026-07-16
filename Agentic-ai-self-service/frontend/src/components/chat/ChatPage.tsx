/**
 * ChatPage — the end-user (t-user) conversational experience (Loom-study 3.1).
 *
 * Standard users don't build on the canvas; they CONSUME deployed agents through
 * a chat. Two-column layout: a left sidebar (agent picker + New Conversation +
 * user/sign-out) and the main streaming chat area. Reuses the platform's existing
 * SSE invoke path (streamInvokeApi) and the caller's own succeeded deployments
 * (listMyAgentsApi) — no new backend.
 *
 * Admins reach this via View-as preview (a banner + Exit is rendered by App.tsx).
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { signOut } from 'aws-amplify/auth';
import {
  listMyAgentsApi,
  streamInvokeApi,
  getErrorMessage,
  type DeployedAgentSummary,
} from '../../services/api';

interface Msg {
  id: string;
  role: 'user' | 'assistant';
  content: string;
}

export interface ChatPageProps {
  /** Rendered above the chat when an admin is previewing (View-as). */
  previewBanner?: React.ReactNode;
}

export function ChatPage({ previewBanner }: ChatPageProps) {
  const [agents, setAgents] = useState<DeployedAgentSummary[] | null>(null);
  const [agentError, setAgentError] = useState<string | null>(null);
  const [selected, setSelected] = useState<DeployedAgentSummary | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    listMyAgentsApi()
      .then((list) => {
        const ready = list.filter((a) => a.runtime_id && a.status === 'succeeded');
        setAgents(ready);
        if (ready.length && !selected) setSelected(ready[0]);
      })
      .catch((e) => setAgentError(getErrorMessage(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  const hasMemory = useMemo(
    () => !!(selected?.memory_result && Object.keys(selected.memory_result).length),
    [selected],
  );

  function newConversation() {
    setMessages([]);
    setSessionId(null);
  }

  async function send() {
    const text = input.trim();
    if (!text || !selected?.runtime_id || sending) return;
    setInput('');
    const userMsg: Msg = { id: `u-${Date.now()}`, role: 'user', content: text };
    const asstId = `a-${Date.now()}`;
    setMessages((prev) => [...prev, userMsg, { id: asstId, role: 'assistant', content: '' }]);
    setSending(true);
    try {
      const { sessionId: sid } = await streamInvokeApi(
        { runtimeId: selected.runtime_id, input: text, sessionId },
        (tok) =>
          setMessages((prev) =>
            prev.map((m) => (m.id === asstId ? { ...m, content: m.content + tok } : m)),
          ),
      );
      if (sid) setSessionId(sid);
    } catch (e) {
      setMessages((prev) =>
        prev.map((m) => (m.id === asstId ? { ...m, content: `⚠️ ${getErrorMessage(e)}` } : m)),
      );
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="flex h-screen" style={{ background: 'var(--color-bg)', color: 'var(--color-text-primary)' }}>
      {/* Sidebar */}
      <aside className="w-64 flex flex-col border-r" style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-subtle)' }}>
        <div className="px-4 py-3 font-semibold tracking-tight" style={{ borderBottom: '1px solid var(--color-border)' }}>
          AgentCore Chat
        </div>
        <div className="p-3 space-y-2 flex-1 overflow-y-auto">
          <button
            type="button"
            onClick={newConversation}
            className="w-full px-3 py-2 text-sm rounded-lg bg-blue-600 text-white hover:bg-blue-700"
          >
            + New Conversation
          </button>
          <div className="text-xs mt-3 mb-1" style={{ color: 'var(--color-text-secondary)' }}>Your agents</div>
          {agentError && <div className="text-xs" style={{ color: '#dc2626' }}>{agentError}</div>}
          {agents === null && <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>Loading…</div>}
          {agents?.length === 0 && (
            <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
              No deployed agents yet.
            </div>
          )}
          {agents?.map((a) => (
            <button
              key={a.deployment_id}
              type="button"
              onClick={() => { setSelected(a); newConversation(); }}
              className="w-full text-left px-3 py-2 text-sm rounded-lg truncate transition-colors"
              style={{
                background: selected?.deployment_id === a.deployment_id ? 'var(--accent)' : 'transparent',
                color: selected?.deployment_id === a.deployment_id ? '#fff' : 'var(--color-text-primary)',
              }}
            >
              {a.agentcore_runtime_name || a.runtime_id}
            </button>
          ))}
        </div>
        <div className="p-3 text-xs" style={{ borderTop: '1px solid var(--color-border)', color: 'var(--color-text-secondary)' }}>
          {hasMemory && <div className="mb-2">🧠 This agent remembers your conversations.</div>}
          <button type="button" onClick={() => void signOut()} className="hover:underline">Sign out</button>
        </div>
      </aside>

      {/* Main chat */}
      <main className="flex-1 flex flex-col">
        {previewBanner}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 py-4">
          <div className="mx-auto max-w-2xl space-y-4">
            {messages.length === 0 && (
              <div className="text-center text-sm mt-20" style={{ color: 'var(--color-text-secondary)' }}>
                {selected ? `Chat with ${selected.agentcore_runtime_name || selected.runtime_id}` : 'Select an agent to start.'}
              </div>
            )}
            {messages.map((m) => (
              <div key={m.id} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                <div
                  className="px-3 py-2 rounded-2xl text-sm whitespace-pre-wrap"
                  style={{
                    maxWidth: '80%',
                    background: m.role === 'user' ? 'var(--accent)' : 'var(--color-bg-subtle)',
                    color: m.role === 'user' ? '#fff' : 'var(--color-text-primary)',
                    border: m.role === 'assistant' ? '1px solid var(--color-border)' : 'none',
                  }}
                >
                  {m.content || (m.role === 'assistant' && sending ? '…' : '')}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Composer */}
        <div className="border-t px-6 py-3" style={{ borderColor: 'var(--color-border)' }}>
          <div className="mx-auto max-w-2xl flex gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              placeholder={selected ? 'Message your agent… (Enter to send, Shift+Enter for newline)' : 'Select an agent first'}
              disabled={!selected || sending}
              rows={1}
              className="flex-1 px-3 py-2 text-sm rounded-lg resize-none border focus:outline-none focus:ring-2 focus:ring-blue-500"
              style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg)', color: 'var(--color-text-primary)' }}
            />
            <button
              type="button"
              onClick={() => void send()}
              disabled={!selected || sending || !input.trim()}
              className="px-4 py-2 text-sm font-medium rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40"
            >
              {sending ? '…' : 'Send'}
            </button>
          </div>
        </div>
      </main>
    </div>
  );
}
