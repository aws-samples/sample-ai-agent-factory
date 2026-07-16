import { fetchAuthSession } from 'aws-amplify/auth';

/**
 * A stable per-browser-session id, minted once per tab load and sent on every
 * API call as X-Session-Id. The backend audit middleware records it as
 * session_uuid so admin analytics can distinguish activity streams even on a
 * shared account (Loom-study 0.5 — the field existed but was never populated).
 */
let _sessionId: string | null = null;
function browserSessionId(): string {
  if (_sessionId) return _sessionId;
  try {
    if (globalThis.crypto && 'randomUUID' in globalThis.crypto) {
      _sessionId = globalThis.crypto.randomUUID();
    } else if (globalThis.crypto && 'getRandomValues' in globalThis.crypto) {
      // Cryptographically-secure fallback for older runtimes without randomUUID.
      // NEVER Math.random() — a session-correlation id lives in a security
      // context (CodeQL js/insecure-randomness).
      const b = new Uint8Array(16);
      globalThis.crypto.getRandomValues(b);
      _sessionId = `sess-${Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('')}`;
    } else {
      _sessionId = `sess-${Date.now()}`;
    }
  } catch {
    _sessionId = `sess-${Date.now()}`;
  }
  return _sessionId;
}

export async function authFetch(url: string, options?: RequestInit): Promise<Response> {
  const session = await fetchAuthSession();
  const token = session.tokens?.accessToken?.toString();
  return fetch(url, {
    ...options,
    headers: {
      ...options?.headers,
      'X-Session-Id': browserSessionId(),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });
}
