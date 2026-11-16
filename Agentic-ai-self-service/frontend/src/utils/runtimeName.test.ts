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

  it('falls back to the canvas name before any deployment', () => {
    expect(resolveManagementRuntimeName(undefined, undefined, 'draft_agent')).toBe('draft_agent');
  });

  it('is null when nothing is known', () => {
    expect(resolveManagementRuntimeName(undefined, undefined, undefined)).toBeNull();
    expect(resolveManagementRuntimeName(null, null, null)).toBeNull();
  });
});
