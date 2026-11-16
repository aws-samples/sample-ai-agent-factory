import { describe, it, expect } from 'vitest';
import { resolveManagementRuntimeName } from './runtimeName';

describe('resolveManagementRuntimeName', () => {
  it('prefers the deployed (served) runtime name over the raw canvas name', () => {
    // The canvas name "My Agent" deployed as "My_Agent"; the tabs must use the
    // served name or AgentCore answers 400.
    expect(resolveManagementRuntimeName('My_Agent', undefined, 'My Agent')).toBe('My_Agent');
  });

  it('uses a restored deployment name when there is no fresh deploy', () => {
    expect(resolveManagementRuntimeName(undefined, 'My_Agent', 'My Agent')).toBe('My_Agent');
  });

  it('falls back to a valid canvas name before any deployment', () => {
    expect(resolveManagementRuntimeName(undefined, undefined, 'draft_agent')).toBe('draft_agent');
  });

  it('is null when nothing is known', () => {
    expect(resolveManagementRuntimeName(undefined, undefined, undefined)).toBeNull();
    expect(resolveManagementRuntimeName(null, null, null)).toBeNull();
  });

  it('refuses a name that is not a valid AgentCore runtime name (no request path from a hostile value)', () => {
    // A raw canvas name with a space is not query-able; show the empty state.
    expect(resolveManagementRuntimeName(undefined, undefined, 'My Agent')).toBeNull();
    // Path-traversal-shaped values never reach a request path.
    expect(resolveManagementRuntimeName('..', undefined, undefined)).toBeNull();
    expect(resolveManagementRuntimeName('../admin', undefined, undefined)).toBeNull();
    expect(resolveManagementRuntimeName('a/b', undefined, undefined)).toBeNull();
    // A hyphenated name is not the underscore AgentCore pattern either.
    expect(resolveManagementRuntimeName('my-agent', undefined, undefined)).toBeNull();
  });
});
