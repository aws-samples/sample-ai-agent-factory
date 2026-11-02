export interface AgentCoreHistoryMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface AgentCoreInvokeRequest {
  runtimeName: string;
  input: string;
  sessionId?: string | null;
  history?: AgentCoreHistoryMessage[];
}

export interface AgentCoreInvokeResult {
  response: string;
  sessionId: string | null;
  runtimeName: string;
  versionId: string;
  deploymentId: string;
  toolReceipts: Record<string, unknown>[] | null;
  identityEvidence: Record<string, unknown> | null;
}

export class AgentCoreClientError extends Error {
  status: number;
  code: string;
  retryAfter: number | null;
}

export class AgentCoreClient {
  constructor(options: {
    baseUrl: string;
    tokenProvider: () => string | Promise<string>;
    fetchImpl?: typeof fetch;
  });
  invoke(request: AgentCoreInvokeRequest): Promise<AgentCoreInvokeResult>;
}

export class AgentCoreChatElement extends HTMLElement {
  tokenProvider: (() => string | Promise<string>) | null;
  fetchImpl: typeof fetch;
  newConversation(): void;
  sendMessage(message: string): Promise<AgentCoreInvokeResult>;
}

export function defineAgentCoreChat(tagName?: string): CustomElementConstructor;

declare global {
  interface HTMLElementTagNameMap {
    'agentcore-chat': AgentCoreChatElement;
  }
}
