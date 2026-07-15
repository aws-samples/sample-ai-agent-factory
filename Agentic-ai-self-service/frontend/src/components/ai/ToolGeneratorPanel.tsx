/**
 * AI Tool Generator Panel — conversational chat interface for generating,
 * testing, and adding Lambda tools to the canvas.
 *
 * Flow: Describe tool → LLM asks questions → Generate code + test cases →
 * Auto-test on real Lambda → Pass → Add to Canvas.
 */

import { useState, useCallback, useRef, useEffect } from 'react';
import { generateToolApi, testToolApi } from '../../services/api';
import type { GeneratedTool, TestCase, TestResult } from '../../services/api';

// ============================================================================
// Types
// ============================================================================

type TestPhase = 'idle' | 'testing' | 'passed' | 'failed';

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  tool?: GeneratedTool;
  testCases?: TestCase[];
  testResults?: TestResult[];
  testPhase?: TestPhase;
}

export interface ToolGeneratorPanelProps {
  isVisible: boolean;
  onClose: () => void;
  onAddToolToCanvas: (tool: GeneratedTool) => void;
}

const MAX_AUTO_FIX_RETRIES = 2;

// ============================================================================
// Component
// ============================================================================

export function ToolGeneratorPanel({ isVisible, onClose, onAddToolToCanvas }: ToolGeneratorPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [isGenerating, setIsGenerating] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [currentTool, setCurrentTool] = useState<GeneratedTool | null>(null);
  const [showCode, setShowCode] = useState(false);
  const [autoFixCount, setAutoFixCount] = useState(0);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isTesting]);

  // Focus input when panel opens
  useEffect(() => {
    if (isVisible) {
      setTimeout(() => inputRef.current?.focus(), 300);
    }
  }, [isVisible]);

  // Run tests on a generated tool
  const runTests = useCallback(async (tool: GeneratedTool, testCases: TestCase[], msgId: string) => {
    setIsTesting(true);

    // Update the message to show testing state
    setMessages((prev) =>
      prev.map((m) => (m.id === msgId ? { ...m, testPhase: 'testing' as TestPhase } : m)),
    );

    try {
      const result = await testToolApi({
        lambdaCode: tool.lambdaCode,
        testCases,
      });

      const phase: TestPhase = result.allPassed ? 'passed' : 'failed';

      // Update message with test results
      setMessages((prev) =>
        prev.map((m) =>
          m.id === msgId ? { ...m, testResults: result.results, testPhase: phase } : m,
        ),
      );

      return result;
    } catch (err) {

      setMessages((prev) =>
        prev.map((m) =>
          m.id === msgId
            ? {
                ...m,
                testPhase: 'failed' as TestPhase,
                testResults: [
                  {
                    testCaseName: 'deployment',
                    passed: false,
                    error: err instanceof Error ? err.message : 'Test deployment failed',
                    durationMs: 0,
                  },
                ],
              }
            : m,
        ),
      );
      return null;
    } finally {
      setIsTesting(false);
    }
  }, []);

  // Auto-fix: send failure context back to LLM
  const handleAutoFix = useCallback(
    async (failedResults: TestResult[], tool: GeneratedTool, testCases: TestCase[]) => {
      if (autoFixCount >= MAX_AUTO_FIX_RETRIES) return;

      const failureDetails = failedResults
        .filter((r) => !r.passed)
        .map((r) => `- ${r.testCaseName}: ${r.error || 'Unknown error'}${r.actualOutput ? ` (got: ${JSON.stringify(r.actualOutput).slice(0, 200)})` : ''}`)
        .join('\n');

      const fixPrompt = `Tests failed for the generated tool "${tool.displayName}". Fix the Lambda code.\n\nFailures:\n${failureDetails}\n\nPlease regenerate the tool with fixed code that passes all tests.`;

      // Add as user message
      const userMsg: ChatMessage = {
        id: `user-autofix-${Date.now()}`,
        role: 'user',
        content: `Auto-fixing: ${failedResults.filter((r) => !r.passed).length} test(s) failed. Requesting fix...`,
      };
      setMessages((prev) => [...prev, userMsg]);
      setIsGenerating(true);
      setAutoFixCount((c) => c + 1);

      try {
        const conversationHistory = messages.map((m) => ({
          role: m.role,
          content: m.content,
        }));

        const response = await generateToolApi({
          prompt: fixPrompt,
          conversationHistory,
          existingTool: tool as unknown as Record<string, unknown>,
        });

        if (response.success && response.tool && response.responseType === 'generation') {
          const msgId = `assistant-fix-${Date.now()}`;
          const assistantMsg: ChatMessage = {
            id: msgId,
            role: 'assistant',
            content: response.message || `Fixed tool: ${response.tool.displayName}`,
            tool: response.tool,
            testCases: response.testCases || testCases,
            testPhase: 'idle',
          };
          setMessages((prev) => [...prev, assistantMsg]);
          setCurrentTool(response.tool);
          setIsGenerating(false);

          // Auto-run tests on the fixed tool
          await runTests(response.tool, response.testCases || testCases, msgId);
        } else {
          const assistantMsg: ChatMessage = {
            id: `assistant-fix-err-${Date.now()}`,
            role: 'assistant',
            content: response.message || response.error || 'Failed to auto-fix.',
          };
          setMessages((prev) => [...prev, assistantMsg]);
          setIsGenerating(false);
        }
      } catch (err) {
        const errorMsg: ChatMessage = {
          id: `error-fix-${Date.now()}`,
          role: 'assistant',
          content: `Auto-fix error: ${err instanceof Error ? err.message : 'Unknown error'}`,
        };
        setMessages((prev) => [...prev, errorMsg]);
        setIsGenerating(false);
      }
    },
    [messages, autoFixCount, runTests],
  );

  const handleSend = useCallback(async () => {
    const prompt = inputValue.trim();
    if (!prompt || isGenerating || isTesting) return;

    const userMsg: ChatMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: prompt,
    };
    setMessages((prev) => [...prev, userMsg]);
    setInputValue('');
    setIsGenerating(true);

    try {
      const conversationHistory = messages.map((m) => ({
        role: m.role,
        content: m.content,
      }));

      const response = await generateToolApi({
        prompt,
        conversationHistory,
        existingTool: currentTool ? (currentTool as unknown as Record<string, unknown>) : undefined,
      });

      // Handle clarification mode — just a conversation message
      if (response.responseType === 'clarification') {
        const assistantMsg: ChatMessage = {
          id: `assistant-${Date.now()}`,
          role: 'assistant',
          content: response.message || 'Could you provide more details?',
        };
        setMessages((prev) => [...prev, assistantMsg]);
        setIsGenerating(false);
        return;
      }

      // Handle generation mode — tool + test cases
      const msgId = `assistant-${Date.now()}`;
      const assistantMsg: ChatMessage = {
        id: msgId,
        role: 'assistant',
        content: response.success
          ? response.message || `Generated tool: ${response.tool?.displayName}`
          : `Error: ${response.error || 'Failed to generate tool'}`,
        tool: response.tool || undefined,
        testCases: response.testCases || undefined,
        testPhase: response.tool ? 'idle' : undefined,
      };

      setMessages((prev) => [...prev, assistantMsg]);

      if (response.success && response.tool) {
        setCurrentTool(response.tool);
        setShowCode(false);
        setAutoFixCount(0);
        setIsGenerating(false);

        // Auto-trigger testing if we have test cases
        if (response.testCases && response.testCases.length > 0) {
          const testResult = await runTests(response.tool, response.testCases, msgId);

          // Auto-fix if tests failed and we haven't exceeded retries
          if (testResult && !testResult.allPassed && autoFixCount < MAX_AUTO_FIX_RETRIES) {
            await handleAutoFix(testResult.results, response.tool, response.testCases);
          }
        }
      } else {
        setIsGenerating(false);
      }
    } catch (err) {
      const errorMsg: ChatMessage = {
        id: `error-${Date.now()}`,
        role: 'assistant',
        content: `Error: ${err instanceof Error ? err.message : 'Unknown error occurred'}`,
      };
      setMessages((prev) => [...prev, errorMsg]);
      setIsGenerating(false);
    }
  }, [inputValue, isGenerating, isTesting, messages, currentTool, autoFixCount, runTests, handleAutoFix]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  const handleAddToCanvas = useCallback(
    (tool: GeneratedTool) => {
      onAddToolToCanvas(tool);
    },
    [onAddToolToCanvas],
  );

  const handleNewChat = useCallback(() => {
    setMessages([]);
    setCurrentTool(null);
    setShowCode(false);
    setInputValue('');
    setAutoFixCount(0);
  }, []);

  // Render test status badge
  const renderTestBadge = (phase?: TestPhase) => {
    switch (phase) {
      case 'testing':
        return (
          <span className="text-xs px-2 py-0.5 bg-amber-100 text-amber-700 rounded-full flex-shrink-0 ml-2 flex items-center gap-1">
            <svg className="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            Testing...
          </span>
        );
      case 'passed':
        return (
          <span className="text-xs px-2 py-0.5 bg-green-100 text-green-700 rounded-full flex-shrink-0 ml-2">
            Tests Passed
          </span>
        );
      case 'failed':
        return (
          <span className="text-xs px-2 py-0.5 bg-red-100 text-red-700 rounded-full flex-shrink-0 ml-2">
            Tests Failed
          </span>
        );
      default:
        return (
          <span className="text-xs px-2 py-0.5 bg-gray-100 text-gray-500 rounded-full flex-shrink-0 ml-2">
            Untested
          </span>
        );
    }
  };

  // Render test results inline
  const renderTestResults = (msg: ChatMessage) => {
    if (!msg.testResults || msg.testResults.length === 0) {
      if (msg.testPhase === 'testing') {
        return (
          <div className="mt-2 p-2 bg-amber-50 rounded-lg border border-amber-200">
            <div className="flex items-center gap-2 text-xs text-amber-700">
              <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Deploying temporary Lambda and running tests...
            </div>
          </div>
        );
      }
      return null;
    }

    return (
      <div className="mt-2 space-y-1.5">
        <div className="text-xs text-gray-400 font-medium">Test Results:</div>
        {msg.testResults.map((tr, i) => (
          <div
            key={i}
            className={`flex items-start gap-2 p-2 rounded-lg text-xs ${
              tr.passed ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'
            }`}
          >
            {tr.passed ? (
              <svg className="w-4 h-4 text-green-600 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
            ) : (
              <svg className="w-4 h-4 text-red-600 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            )}
            <div className="flex-1 min-w-0">
              <div className={`font-medium ${tr.passed ? 'text-green-800' : 'text-red-800'}`}>
                {tr.testCaseName}
                <span className="font-normal text-gray-400 ml-2">{tr.durationMs}ms</span>
              </div>
              {tr.error && <div className="text-red-600 mt-0.5 break-words">{tr.error}</div>}
              {tr.actualOutput && !tr.passed && (
                <details className="mt-1">
                  <summary className="cursor-pointer text-gray-500 hover:text-gray-700">Show output</summary>
                  <pre className="mt-1 p-1.5 bg-white rounded text-[10px] overflow-x-auto text-gray-600">
                    {JSON.stringify(tr.actualOutput, null, 2).slice(0, 500)}
                  </pre>
                </details>
              )}
            </div>
          </div>
        ))}

        {/* Auto-fix button for failures */}
        {msg.testPhase === 'failed' && msg.tool && msg.testCases && autoFixCount < MAX_AUTO_FIX_RETRIES && !isGenerating && !isTesting && (
          <button
            onClick={() => msg.testResults && msg.tool && msg.testCases && handleAutoFix(msg.testResults, msg.tool, msg.testCases)}
            className="w-full mt-1 py-1.5 px-3 bg-amber-500 text-white rounded-md text-xs font-medium hover:bg-amber-600 transition-colors"
          >
            Auto-fix ({MAX_AUTO_FIX_RETRIES - autoFixCount} retries left)
          </button>
        )}
      </div>
    );
  };

  return (
    <>
      {/* Backdrop */}
      {isVisible && (
        <div
          className="fixed inset-0 bg-black/20 z-40 transition-opacity"
          onClick={onClose}
        />
      )}

      {/* Panel */}
      <div
        className={`fixed top-0 right-0 h-full w-[480px] bg-white shadow-2xl z-50 flex flex-col transition-transform duration-300 ease-in-out ${
          isVisible ? 'translate-x-0' : 'translate-x-full'
        }`}
      >
        {/* Header */}
        <div className="h-14 flex items-center justify-between px-4 border-b border-gray-200 flex-shrink-0" style={{ background: 'var(--color-bg-subtle)' }}>
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-purple-500 to-indigo-600 flex items-center justify-center">
              <span className="text-white text-sm">AI</span>
            </div>
            <span className="font-semibold text-gray-800">AI Tool Generator</span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={handleNewChat}
              className="p-2 rounded-lg hover:bg-white/60 transition-colors text-gray-500 hover:text-gray-700"
              title="New conversation"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
              </svg>
            </button>
            <button
              onClick={onClose}
              className="p-2 rounded-lg hover:bg-white/60 transition-colors text-gray-500 hover:text-gray-700"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>

        {/* Messages Area */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {messages.length === 0 && (
            <div className="text-center py-12">
              <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-gradient-to-br from-purple-100 to-indigo-100 flex items-center justify-center">
                <span className="text-3xl">AI</span>
              </div>
              <h3 className="text-lg font-medium text-gray-700 mb-2">Create Tools with AI</h3>
              <p className="text-sm text-gray-500 mb-6 max-w-xs mx-auto">
                Describe a tool and AI will ask clarifying questions, generate Lambda code, and test it automatically.
              </p>
              <div className="space-y-2 text-left max-w-xs mx-auto">
                {[
                  'A tool that fetches GitHub repository info',
                  'A currency converter tool',
                  'A tool that generates random passwords',
                ].map((suggestion) => (
                  <button
                    key={suggestion}
                    onClick={() => {
                      setInputValue(suggestion);
                      inputRef.current?.focus();
                    }}
                    className="w-full text-left px-3 py-2 text-sm text-gray-600 bg-gray-50 hover:bg-purple-50 hover:text-purple-700 rounded-lg transition-colors border border-gray-200 hover:border-purple-200"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((msg) => (
            <div key={msg.id} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <div
                className={`max-w-[90%] rounded-xl px-4 py-3 ${
                  msg.role === 'user'
                    ? 'bg-gradient-to-r from-purple-500 to-indigo-600 text-white'
                    : 'bg-gray-100 text-gray-800'
                }`}
              >
                <p className="text-sm whitespace-pre-wrap">{msg.content}</p>

                {/* Tool Preview Card */}
                {msg.tool && (
                  <div className="mt-3 bg-white rounded-lg border border-gray-200 p-3 text-gray-800">
                    <div className="flex items-start justify-between mb-2">
                      <div>
                        <div className="font-medium text-sm">{msg.tool.displayName}</div>
                        <div className="text-xs text-gray-500 mt-0.5">{msg.tool.description}</div>
                      </div>
                      {renderTestBadge(msg.testPhase)}
                    </div>

                    {/* Input Schema Summary */}
                    {msg.tool.inputSchema?.properties != null && typeof msg.tool.inputSchema.properties === 'object' && (
                      <div className="mb-2">
                        <div className="text-xs text-gray-400 mb-1">Parameters:</div>
                        <div className="flex flex-wrap gap-1">
                          {Object.entries(msg.tool.inputSchema.properties as Record<string, { type?: string }>).map(
                            ([key, val]) => (
                              <span
                                key={key}
                                className="text-xs px-2 py-0.5 bg-gray-100 text-gray-600 rounded"
                              >
                                {key}: {String(val.type || 'any')}
                              </span>
                            ),
                          )}
                        </div>
                      </div>
                    )}

                    {/* Code Preview Toggle */}
                    <button
                      onClick={() => setShowCode((prev) => !prev)}
                      className="text-xs text-purple-600 hover:text-purple-800 mb-2 flex items-center gap-1"
                    >
                      <svg
                        className={`w-3 h-3 transition-transform ${showCode ? 'rotate-90' : ''}`}
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                      >
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                      </svg>
                      {showCode ? 'Hide' : 'View'} Lambda Code
                    </button>

                    {showCode && (
                      <pre className="text-[11px] bg-gray-900 text-green-400 p-3 rounded-lg overflow-x-auto max-h-64 overflow-y-auto mb-2">
                        <code>{msg.tool.lambdaCode}</code>
                      </pre>
                    )}

                    {/* Test Results */}
                    {renderTestResults(msg)}

                    {/* Add to Canvas Button — only enabled after tests pass */}
                    <button
                      onClick={() => msg.tool && handleAddToCanvas(msg.tool)}
                      disabled={msg.testPhase !== 'passed'}
                      className={`w-full mt-2 py-2 px-3 rounded-md text-sm font-medium transition-colors ${
                        msg.testPhase === 'passed'
                          ? 'bg-[#0972d3] text-white hover:bg-[#0961b9]'
                          : 'bg-gray-200 text-gray-400 cursor-not-allowed'
                      }`}
                    >
                      {msg.testPhase === 'passed' ? 'Add to Canvas' : msg.testPhase === 'testing' ? 'Testing...' : msg.testPhase === 'failed' ? 'Tests Must Pass' : 'Waiting for Tests'}
                    </button>

                    {/* Escape hatch for power users */}
                    {msg.testPhase === 'failed' && autoFixCount >= MAX_AUTO_FIX_RETRIES && (
                      <button
                        onClick={() => msg.tool && handleAddToCanvas(msg.tool)}
                        className="w-full mt-1 py-1 px-3 text-xs text-gray-400 hover:text-gray-600 transition-colors"
                      >
                        Add anyway (skip tests)
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>
          ))}

          {/* Loading indicator */}
          {isGenerating && (
            <div className="flex justify-start">
              <div className="bg-gray-100 rounded-xl px-4 py-3">
                <div className="flex items-center gap-2 text-sm text-gray-500">
                  <div className="flex gap-1">
                    <div className="w-2 h-2 bg-purple-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                    <div className="w-2 h-2 bg-purple-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                    <div className="w-2 h-2 bg-purple-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                  </div>
                  Generating tool...
                </div>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Input Area */}
        <div className="p-4 border-t border-gray-200 bg-gray-50 flex-shrink-0">
          <div className="flex gap-2">
            <textarea
              ref={inputRef}
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Describe a tool... (e.g., 'A tool that fetches stock prices')"
              className="flex-1 resize-none rounded-xl border border-gray-300 px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-purple-500 focus:border-transparent bg-white"
              rows={2}
              disabled={isGenerating || isTesting}
            />
            <button
              onClick={handleSend}
              disabled={!inputValue.trim() || isGenerating || isTesting}
              className={`self-end px-4 py-3 rounded-xl font-medium text-sm transition-all ${
                inputValue.trim() && !isGenerating && !isTesting
                  ? 'bg-gradient-to-r from-purple-500 to-indigo-600 text-white hover:from-purple-600 hover:to-indigo-700 shadow-lg shadow-purple-500/25'
                  : 'bg-gray-200 text-gray-400 cursor-not-allowed'
              }`}
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19V5m0 0l-7 7m7-7l7 7" />
              </svg>
            </button>
          </div>
          <div className="text-[10px] text-gray-400 mt-2 text-center">
            Powered by Claude Sonnet on Amazon Bedrock
          </div>
        </div>
      </div>
    </>
  );
}

export default ToolGeneratorPanel;
