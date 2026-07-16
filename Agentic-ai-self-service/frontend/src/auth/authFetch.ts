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
    _sessionId =
      (globalThis.crypto && 'randomUUID' in globalThis.crypto)
        ? globalThis.crypto.randomUUID()
        : `sess-${Date.now()}-${Math.floor(Math.random() * 1e9).toString(36)}`;
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
