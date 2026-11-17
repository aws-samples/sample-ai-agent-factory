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
  type ChatImageInput,
} from '../../services/api';

const MAX_CHAT_IMAGES = 5;

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
  const [images, setImages] = useState<ChatImageInput[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const IMAGE_FORMATS: Record<string, ChatImageInput['format']> = {
    'image/png': 'png',
    'image/jpeg': 'jpeg',
    'image/gif': 'gif',
    'image/webp': 'webp',
  };

  async function onPickImages(fileList: FileList | null) {
    if (!fileList) return;
    const picked: ChatImageInput[] = [];
    for (const file of Array.from(fileList).slice(0, MAX_CHAT_IMAGES)) {
      const format = IMAGE_FORMATS[file.type];
      if (!format) continue; // only the Converse-supported image types
      const data = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const result = String(reader.result || '');
          const comma = result.indexOf(','); // strip the "data:...;base64," prefix
          resolve(comma >= 0 ? result.slice(comma + 1) : result);
        };
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
      if (data) picked.push({ format, data });
    }
    if (picked.length) setImages((prev) => [...prev, ...picked].slice(0, MAX_CHAT_IMAGES));
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  useEffect(() => {
    listMyAgentsApi()
      .then((list) => {
        const ready = list.filter((a) => a.runtime_id && a.runtime_name && a.status === 'succeeded');
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
    setImages([]);
  }

  async function send() {
    const text = input.trim();
    if ((!text && images.length === 0) || !selected?.runtime_name || sending) return;
    const outgoing = images;
    setInput('');
    setImages([]);
    const attachNote = outgoing.length ? `📎 ${outgoing.length} image${outgoing.length > 1 ? 's' : ''}` : '';
    const label = [text, attachNote].filter(Boolean).join('\n');
    const userMsg: Msg = { id: `u-${Date.now()}`, role: 'user', content: label };
    const asstId = `a-${Date.now()}`;
    setMessages((prev) => [...prev, userMsg, { id: asstId, role: 'assistant', content: '' }]);
    setSending(true);
    try {
      const { sessionId: sid } = await streamInvokeApi(
        {
          runtimeName: selected.runtime_name,
          input: text,
          sessionId,
          images: outgoing.length ? outgoing : undefined,
        },
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
        <h1 className="px-4 py-3 font-semibold tracking-tight" style={{ borderBottom: '1px solid var(--color-border)' }}>
          AgentCore Chat
        </h1>
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
          {/* `agents` stays null when the fetch rejects, so this has to exclude the
              error case or the sidebar shows the failure and a spinner that never
              resolves, side by side — observed live as "Agent list failed (401)"
              above a permanent "Loading…". */}
          {agents === null && !agentError && (
            <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>Loading…</div>
          )}
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
          <div className="mx-auto max-w-2xl space-y-2">
            {images.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {images.map((img, i) => (
                  <div key={i} className="relative">
                    <img
                      src={`data:image/${img.format};base64,${img.data}`}
                      alt={`attachment ${i + 1}`}
                      className="h-14 w-14 object-cover rounded-lg border"
                      style={{ borderColor: 'var(--color-border)' }}
                    />
                    <button
                      type="button"
                      onClick={() => setImages((prev) => prev.filter((_, j) => j !== i))}
                      aria-label={`Remove attachment ${i + 1}`}
                      className="absolute -top-1.5 -right-1.5 h-5 w-5 rounded-full bg-gray-800 text-white text-xs leading-none"
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="flex gap-2">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/png,image/jpeg,image/gif,image/webp"
                multiple
                className="hidden"
                onChange={(e) => void onPickImages(e.target.files)}
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={!selected || sending || images.length >= MAX_CHAT_IMAGES}
                aria-label="Attach image"
                title="Attach an image for the agent"
                className="px-3 py-2 text-sm rounded-lg border disabled:opacity-40"
                style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg)', color: 'var(--color-text-primary)' }}
              >
                📎
              </button>
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
                aria-label="Message your agent"
                disabled={!selected || sending}
                rows={1}
                className="flex-1 px-3 py-2 text-sm rounded-lg resize-none border focus:outline-none focus:ring-2 focus:ring-blue-500"
                style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg)', color: 'var(--color-text-primary)' }}
              />
              <button
                type="button"
                onClick={() => void send()}
                disabled={!selected || sending || (!input.trim() && images.length === 0)}
                className="px-4 py-2 text-sm font-medium rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40"
              >
                {sending ? '…' : 'Send'}
              </button>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
