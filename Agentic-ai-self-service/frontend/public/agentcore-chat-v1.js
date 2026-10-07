const MAX_INPUT_LENGTH = 10_000;
const MAX_HISTORY_MESSAGES = 50;
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const RUNTIME_NAME = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;

function canonicalBaseUrl(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new TypeError('baseUrl must be an absolute URL');
  }
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname);
  if (parsed.protocol !== 'https:' && !(local && parsed.protocol === 'http:')) {
    throw new TypeError('baseUrl must use HTTPS (HTTP is allowed only on loopback)');
  }
  if (parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) {
    throw new TypeError('baseUrl must be a canonical origin without credentials, path, query, or fragment');
  }
  return parsed.origin;
}

function validateRuntimeName(value) {
  const name = String(value || '').trim();
  if (!RUNTIME_NAME.test(name)) {
    throw new TypeError('runtimeName must match [A-Za-z][A-Za-z0-9_]{0,63}');
  }
  return name;
}

function validateInput(value) {
  const input = String(value || '').trim();
  if (!input || input.length > MAX_INPUT_LENGTH) {
    throw new TypeError(`input must contain 1..${MAX_INPUT_LENGTH} characters`);
  }
  return input;
}

function validateHistory(value) {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.length > MAX_HISTORY_MESSAGES) {
    throw new TypeError(`history must contain at most ${MAX_HISTORY_MESSAGES} messages`);
  }
  return value.map((message) => {
    if (!message || !['user', 'assistant'].includes(message.role)) {
      throw new TypeError('history messages require a user or assistant role');
    }
    const content = String(message.content || '');
    if (content.length > MAX_INPUT_LENGTH) {
      throw new TypeError(`history message content exceeds ${MAX_INPUT_LENGTH} characters`);
    }
    return { role: message.role, content };
  });
}

async function responseJson(response) {
  const text = await response.text();
  if (new TextEncoder().encode(text).length > MAX_RESPONSE_BYTES) {
    throw new AgentCoreClientError('Response exceeded the client safety limit', {
      status: 502,
      code: 'response_too_large',
    });
  }
  if (!text) return {};
  try {
    const value = JSON.parse(text);
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch {
    throw new AgentCoreClientError('Server returned an invalid JSON response', {
      status: 502,
      code: 'invalid_response',
    });
  }
}

export class AgentCoreClientError extends Error {
  constructor(message, { status = 0, code = 'client_error', retryAfter = null } = {}) {
    super(message);
    this.name = 'AgentCoreClientError';
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

export class AgentCoreClient {
  constructor({ baseUrl, tokenProvider, fetchImpl = globalThis.fetch } = {}) {
    this.baseUrl = canonicalBaseUrl(baseUrl);
    if (typeof tokenProvider !== 'function') {
      throw new TypeError('tokenProvider must be a function returning a short-lived access token');
    }
    if (typeof fetchImpl !== 'function') {
      throw new TypeError('fetchImpl must be a function');
    }
    this.tokenProvider = tokenProvider;
    this.fetchImpl = fetchImpl;
  }

  async invoke({ runtimeName, input, sessionId = null, history } = {}) {
    const runtime = validateRuntimeName(runtimeName);
    const prompt = validateInput(input);
    const validatedHistory = validateHistory(history);
    const token = String(await this.tokenProvider()).trim();
    if (!token || /\s/.test(token) || token.split('.').length !== 3) {
      throw new AgentCoreClientError('Token provider returned no valid JWT access token', {
        status: 401,
        code: 'invalid_token',
      });
    }
    if (sessionId !== null && (typeof sessionId !== 'string' || sessionId.length > 256)) {
      throw new TypeError('sessionId must be null or a string of at most 256 characters');
    }

    const response = await this.fetchImpl(
      `${this.baseUrl}/api/runtimes/${encodeURIComponent(runtime)}/invoke`,
      {
        method: 'POST',
        mode: 'cors',
        credentials: 'omit',
        cache: 'no-store',
        redirect: 'error',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          input: prompt,
          ...(sessionId ? { sessionId } : {}),
          ...(validatedHistory?.length ? { history: validatedHistory } : {}),
        }),
      },
    );
    const data = await responseJson(response);
    if (!response.ok || data.success !== true) {
      const retryHeader = response.headers?.get?.('retry-after');
      const retryAfter = retryHeader && /^\d+$/.test(retryHeader) ? Number(retryHeader) : null;
      const detail = typeof data.detail === 'string'
        ? data.detail
        : typeof data.error === 'string'
          ? data.error
          : `Invocation failed (${response.status})`;
      throw new AgentCoreClientError(detail, {
        status: response.status,
        code: response.status === 429 ? 'authorization_exhausted' : 'invocation_failed',
        retryAfter,
      });
    }
    return {
      response: String(data.response || ''),
      sessionId: data.sessionId || sessionId || null,
      runtimeName: String(data.runtimeName || runtime),
      versionId: String(data.versionId || ''),
      deploymentId: String(data.deploymentId || ''),
      toolReceipts: Array.isArray(data.toolReceipts) ? data.toolReceipts : null,
      identityEvidence: data.identityEvidence && typeof data.identityEvidence === 'object'
        ? data.identityEvidence
        : null,
    };
  }
}

const template = document.createElement('template');
template.innerHTML = `
  <style>
    :host { display: block; color: var(--agentcore-text, #111827); font: 14px/1.45 system-ui, sans-serif; }
    .shell { border: 1px solid var(--agentcore-border, #d1d5db); border-radius: 12px; overflow: hidden; background: var(--agentcore-bg, #fff); }
    header { padding: 12px 14px; font-weight: 650; border-bottom: 1px solid var(--agentcore-border, #d1d5db); }
    [role="log"] { min-height: 220px; max-height: 420px; overflow: auto; padding: 14px; display: grid; gap: 10px; }
    .message { max-width: 82%; padding: 8px 10px; border-radius: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
    .user { justify-self: end; color: var(--agentcore-user-text, #fff); background: var(--agentcore-accent, #2563eb); }
    .assistant { justify-self: start; background: var(--agentcore-assistant-bg, #f3f4f6); }
    .error { color: var(--agentcore-error, #b91c1c); border: 1px solid currentColor; }
    form { display: flex; gap: 8px; padding: 12px; border-top: 1px solid var(--agentcore-border, #d1d5db); }
    textarea { flex: 1; min-height: 40px; max-height: 140px; resize: vertical; padding: 8px; color: inherit; background: inherit; border: 1px solid var(--agentcore-border, #d1d5db); border-radius: 8px; }
    button { border: 0; border-radius: 8px; padding: 8px 14px; color: var(--agentcore-user-text, #fff); background: var(--agentcore-accent, #2563eb); font-weight: 650; cursor: pointer; }
    button:disabled, textarea:disabled { cursor: not-allowed; opacity: .55; }
  </style>
  <section class="shell" aria-label="Agent chat">
    <header></header>
    <div role="log" aria-live="polite" aria-relevant="additions"></div>
    <form>
      <textarea aria-label="Message your agent" maxlength="10000" required></textarea>
      <button type="submit">Send</button>
    </form>
  </section>
`;

export class AgentCoreChatElement extends HTMLElement {
  static observedAttributes = ['api-base-url', 'runtime-name', 'title', 'placeholder'];

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._tokenProvider = null;
    this._fetchImpl = globalThis.fetch;
    this._sessionId = null;
    this._messages = [];
    this._busy = false;
  }

  connectedCallback() {
    if (!this.shadowRoot.hasChildNodes()) {
      this.shadowRoot.append(template.content.cloneNode(true));
      this.shadowRoot.querySelector('form').addEventListener('submit', (event) => {
        event.preventDefault();
        const input = this.shadowRoot.querySelector('textarea');
        const message = input.value;
        input.value = '';
        void this.sendMessage(message);
      });
      this.shadowRoot.querySelector('textarea').addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey) {
          event.preventDefault();
          this.shadowRoot.querySelector('form').requestSubmit();
        }
      });
    }
    this._refreshLabels();
  }

  attributeChangedCallback() {
    if (this.isConnected) this._refreshLabels();
  }

  set tokenProvider(value) {
    if (value !== null && typeof value !== 'function') {
      throw new TypeError('tokenProvider must be a function or null');
    }
    this._tokenProvider = value;
  }

  get tokenProvider() {
    return this._tokenProvider;
  }

  set fetchImpl(value) {
    if (typeof value !== 'function') throw new TypeError('fetchImpl must be a function');
    this._fetchImpl = value;
  }

  newConversation() {
    this._sessionId = null;
    this._messages = [];
    const log = this.shadowRoot?.querySelector('[role="log"]');
    if (log) log.replaceChildren();
  }

  async sendMessage(value) {
    if (this._busy) throw new AgentCoreClientError('An invocation is already in progress', { code: 'busy' });
    const input = validateInput(value);
    const history = this._messages.slice(-MAX_HISTORY_MESSAGES);
    this._appendMessage('user', input);
    const pending = this._appendMessage('assistant', '…', false);
    this._setBusy(true);
    try {
      const client = new AgentCoreClient({
        baseUrl: this.getAttribute('api-base-url') || new URL('.', import.meta.url).origin,
        tokenProvider: this._tokenProvider,
        fetchImpl: this._fetchImpl,
      });
      const result = await client.invoke({
        runtimeName: this.getAttribute('runtime-name'),
        input,
        sessionId: this._sessionId,
        history,
      });
      pending.textContent = result.response;
      this._messages.push({ role: 'assistant', content: result.response });
      this._sessionId = result.sessionId;
      this.dispatchEvent(new CustomEvent('agentcore-response', {
        bubbles: true,
        composed: true,
        detail: result,
      }));
      return result;
    } catch (error) {
      const normalized = error instanceof AgentCoreClientError
        ? error
        : new AgentCoreClientError('Invocation could not be completed', { code: 'network_error' });
      pending.textContent = normalized.message;
      pending.classList.add('error');
      this.dispatchEvent(new CustomEvent('agentcore-error', {
        bubbles: true,
        composed: true,
        detail: {
          message: normalized.message,
          status: normalized.status,
          code: normalized.code,
          retryAfter: normalized.retryAfter,
        },
      }));
      throw normalized;
    } finally {
      this._setBusy(false);
    }
  }

  _appendMessage(role, content, track = true) {
    const node = document.createElement('div');
    node.className = `message ${role}`;
    node.textContent = content;
    this.shadowRoot.querySelector('[role="log"]').append(node);
    node.scrollIntoView?.({ block: 'nearest' });
    if (track) this._messages.push({ role, content });
    return node;
  }

  _refreshLabels() {
    const header = this.shadowRoot.querySelector('header');
    const input = this.shadowRoot.querySelector('textarea');
    header.textContent = this.getAttribute('title') || 'AgentCore Chat';
    input.placeholder = this.getAttribute('placeholder') || 'Message your agent…';
  }

  _setBusy(value) {
    this._busy = value;
    const input = this.shadowRoot?.querySelector('textarea');
    const button = this.shadowRoot?.querySelector('button');
    if (input) input.disabled = value;
    if (button) button.disabled = value;
  }
}

export function defineAgentCoreChat(tagName = 'agentcore-chat') {
  if (!customElements.get(tagName)) customElements.define(tagName, AgentCoreChatElement);
  return customElements.get(tagName);
}

defineAgentCoreChat();
