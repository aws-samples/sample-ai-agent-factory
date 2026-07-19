/**
 * ChatInterface - extracted chat UI from DeployPanel.
 * Handles message display, input, and streaming responses.
 */

import { useRef, useEffect } from 'react';

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: Date;
  latencyMs?: number;
}

interface ChatInterfaceProps {
  chatMessages: ChatMessage[];
  testInput: string;
  isTesting: boolean;
  onTestInputChange: (value: string) => void;
  onSendMessage: () => void;
  onKeyDown: (e: React.KeyboardEvent) => void;
}

export function ChatInterface({
  chatMessages,
  testInput,
  isTesting,
  onTestInputChange,
  onSendMessage,
  onKeyDown,
}: ChatInterfaceProps) {
  const chatEndRef = useRef<HTMLDivElement>(null);
  const chatInputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [chatMessages, isTesting]);

  useEffect(() => {
    setTimeout(() => chatInputRef.current?.focus(), 100);
  }, []);

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
        {chatMessages.length === 0 && !isTesting && (
          <div className="h-full flex items-center justify-center">
            <div className="text-center">
              <div className="w-12 h-12 mx-auto mb-3 rounded-xl bg-[#0972d3]/10 flex items-center justify-center">
                <svg className="w-6 h-6 text-[#0972d3]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                </svg>
              </div>
              <h4 className="text-sm font-medium text-[#16191f] mb-1">Chat with your Agent</h4>
              <p className="text-xs text-[#5f6b7a]">Send a message to test your deployed agent</p>
            </div>
          </div>
        )}
        {chatMessages.map((msg) => (
          <div key={msg.id} className={`flex gap-2 ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            {msg.role === 'assistant' && (
              <div className="w-6 h-6 rounded-full bg-[#232f3e] flex items-center justify-center flex-shrink-0 mt-1">
                <span className="text-[10px] text-white font-bold">A</span>
              </div>
            )}
            <div className={`max-w-[85%] ${msg.role === 'user' ? 'px-4 py-2.5 rounded-2xl rounded-br-md bg-[#0972d3] text-white' : 'px-4 py-3 rounded-2xl rounded-bl-md bg-[#f2f3f3] border border-[#e9ebed] text-[#16191f]'}`}>
              {msg.role === 'user' ? (
                <div className="text-sm whitespace-pre-wrap">{msg.content}</div>
              ) : (
                <div>
                  <div className="text-sm whitespace-pre-wrap">{msg.content}</div>
                  {msg.latencyMs !== undefined && (
                    <div className="mt-2 text-[10px] text-[#8d99a8]">{msg.latencyMs}ms</div>
                  )}
                </div>
              )}
            </div>
            {msg.role === 'user' && (
              <div className="w-6 h-6 rounded-full bg-gradient-to-br from-[#0972d3] to-[#0961b9] flex items-center justify-center flex-shrink-0 mt-1">
                <span className="text-[10px] text-white font-bold">U</span>
              </div>
            )}
          </div>
        ))}

        {isTesting && (
          <div className="flex justify-start gap-2">
            <div className="w-6 h-6 rounded-full bg-[#232f3e] flex items-center justify-center flex-shrink-0 mt-1">
              <span className="text-[10px] text-white font-bold">A</span>
            </div>
            <div className="px-4 py-3 rounded-2xl rounded-bl-md bg-[#f2f3f3] border border-[#e9ebed]">
              <div className="flex items-center gap-1.5">
                <div className="w-2 h-2 bg-[#0972d3] rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                <div className="w-2 h-2 bg-[#0972d3] rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                <div className="w-2 h-2 bg-[#0972d3] rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
            </div>
          </div>
        )}

        <div ref={chatEndRef} />
      </div>

      <div className="border-t border-[#e9ebed] bg-[#fafafa] p-3 flex-shrink-0">
        <div className="flex gap-2">
          <textarea
            ref={chatInputRef}
            value={testInput}
            onChange={(e) => onTestInputChange(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Type a message..."
            className="flex-1 resize-none rounded-xl border border-[#e9ebed] px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-[#0972d3] focus:border-transparent bg-white"
            rows={1}
            disabled={isTesting}
          />
          <button
            onClick={onSendMessage}
            disabled={!testInput.trim() || isTesting}
            className={`self-end p-2.5 rounded-xl transition-all ${
              testInput.trim() && !isTesting
                ? 'bg-[#0972d3] text-white hover:bg-[#0961b9]'
                : 'bg-[#e9ebed] text-[#8d99a8] cursor-not-allowed'
            }`}
          >
            {isTesting ? (
              <div className="w-5 h-5 border-2 border-current border-t-transparent rounded-full animate-spin" />
            ) : (
              <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7z" />
              </svg>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
