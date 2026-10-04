/**
 * Write-only credentials must never be PERSISTED — not in flow autosave, not in a registry
 * snapshot, not in an export of the canvas. They may live in memory for the one request that
 * consumes them (deploy stages the LiteLLM virtual key into Secrets Manager and keeps only the
 * reference; MCP/OAuth secrets are staged the same way). Anything that survives that request
 * must be a *reference* (`litellmApiKeyRef`, an ARN, an id), never the value.
 *
 * ARCC cnt_dwzZ05hLnqhYXQ: a plaintext secret as API input is the antipattern; scrubbing at
 * every persistence boundary is the defense-in-depth half, the backend refuses/scrubs too.
 */
const WRITE_ONLY_KEYS = new Set([
  'litellmApiKey',
  'litellm_api_key',
  'apiKey',
  'api_key',
  'clientSecret',
  'client_secret',
  'secretValue',
  'secret_value',
  'password',
  'accessToken',
  'access_token',
  'refreshToken',
  'refresh_token',
  'privateKey',
  'private_key',
  'bearerToken',
  'bearer_token',
]);

export function isWriteOnlyCredentialKey(key: string): boolean {
  return WRITE_ONLY_KEYS.has(key);
}

/** Deep copy with every write-only credential key removed, at any depth (objects and arrays). */
export function stripWriteOnlyCredentials<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((v) => stripWriteOnlyCredentials(v)) as unknown as T;
  }
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (isWriteOnlyCredentialKey(k)) continue;
      out[k] = stripWriteOnlyCredentials(v);
    }
    return out as T;
  }
  return value;
}
