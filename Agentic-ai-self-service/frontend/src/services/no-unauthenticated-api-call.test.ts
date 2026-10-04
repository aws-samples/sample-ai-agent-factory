/**
 * Every call to a platform API path must carry the caller's bearer token.
 *
 * Four separate call sites had been written with bare `fetch` instead of
 * `authFetch`, and every one of them failed *quietly*:
 *
 *  - DeployPanel's `/api/test-runtime-stream` 401'd, the `!response.ok` guard
 *    returned false, and the code fell through to the non-streaming path. Chat
 *    kept working, so nobody noticed that streaming was dead in every deployed
 *    UI. Measured live on the throwaway stack: 401 on every message.
 *  - Two `/api/observability/platform-defaults` calls 401'd into
 *    `enabled: false`, which tells the user the per-agent OTEL fields are live
 *    when the platform has locked them — so what they type is dropped
 *    server-side at deploy time with no feedback.
 *  - `/api/observability/credentials` 401'd, making it impossible to store an
 *    OTLP auth header at all.
 *
 * A per-component test cannot catch the next one of these, because the bug is
 * the *absence* of a call that nothing asserts on. So this is a source-level
 * invariant instead: grep the tree, and fail on a bare fetch to an API path.
 *
 * If you are here because this test failed: use `authFetch` from
 * `src/auth/authFetch.ts`, or `apiRequest` from `src/services/api/client.ts`.
 * Add to ALLOWED below only for a route that is genuinely unauthenticated, with
 * the reason.
 */
import { describe, it, expect } from 'vitest';

/**
 * Sources are read through Vite's `import.meta.glob` rather than `node:fs`.
 * `tsconfig.app.json` pins `"types": ["vite/client"]` and includes all of
 * `src`, so a `node:fs` import here typechecks under
 * `tsc --noEmit -p tsconfig.app.json` only by accident and breaks
 * `npm run build` (`tsc -b`, which builds every referenced project). The glob
 * is typed by vite/client, so the invariant compiles in the same project as
 * the code it guards.
 */
const SOURCES = import.meta.glob<string>(
  ['../**/*.ts', '../**/*.tsx', '!../**/*.test.ts', '!../**/*.test.tsx', '!../**/*.d.ts'],
  { query: '?raw', import: 'default', eager: true }
);

/** Glob keys are relative to this file (`src/services/`); make them src-relative. */
const srcRelative = (key: string): string => key.replace(/^\.\.\//, '');

/** Files permitted to call bare `fetch` on an API path, with why. */
const ALLOWED = new Set<string>([
  // The wrapper itself — this is where the token is attached.
  'auth/authFetch.ts',
]);

describe('no unauthenticated platform API calls', () => {
  it('has no bare fetch() to an /api/ path outside the auth wrapper', () => {
    const offenders: string[] = [];

    for (const [key, source] of Object.entries(SOURCES)) {
      const rel = srcRelative(key);
      if (ALLOWED.has(rel)) continue;

      source.split('\n').forEach((line: string, i: number) => {
        // A `fetch(` NOT preceded by an identifier character — so `authFetch(`,
        // `myFetch(` and `.fetch(` are all excluded, bare `fetch(` is not.
        if (!/(^|[^A-Za-z0-9_$.])fetch\s*\(/.test(line)) return;
        // Only care about calls aimed at a platform API route. A fetch of a
        // presigned S3 URL or an external endpoint is not this test's business.
        if (!line.includes('/api/')) return;
        offenders.push(`${rel}:${i + 1}: ${line.trim()}`);
      });
    }

    expect(
      offenders,
      `These call a platform API route without the caller's bearer token, which 401s ` +
        `(often silently). Use authFetch or apiRequest:\n${offenders.join('\n')}`
    ).toEqual([]);
  });

  it('actually inspects a non-trivial number of source files', () => {
    // Guard against the test above passing because the glob matched nothing —
    // a green assertion over an empty set is the failure mode it exists to
    // prevent elsewhere in this repo.
    expect(Object.keys(SOURCES).length).toBeGreaterThan(50);
  });

  it('reads real file contents, and reaches the wrapper it exempts', () => {
    // A glob that resolved to module objects instead of raw text would make the
    // line scan above silently inspect nothing useful. Pin both the raw-text
    // shape and that the walk actually reaches a known file.
    const wrapper = SOURCES['../auth/authFetch.ts'];
    expect(typeof wrapper).toBe('string');
    expect(wrapper).toContain('Authorization');
    expect(Object.keys(SOURCES).map(srcRelative)).toContain(
      'components/modals/ObservabilityConfigurationModal.tsx'
    );
  });

  it('detects a bare fetch when one is present', () => {
    // Pins the regex itself: without this, a typo that matches nothing would
    // make the invariant above vacuously true forever.
    const bad = `  const r = await fetch('/api/observability/credentials', { method: 'POST' });`;
    const good = `  const r = await authFetch('/api/observability/credentials', { method: 'POST' });`;
    const pattern = /(^|[^A-Za-z0-9_$.])fetch\s*\(/;
    expect(pattern.test(bad)).toBe(true);
    expect(pattern.test(good)).toBe(false);
  });
});
