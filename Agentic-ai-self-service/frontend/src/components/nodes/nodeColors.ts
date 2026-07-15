/**
 * Canonical node accent colors (redesign).
 *
 * Kept in its own module (not the component file) so it can be imported by both
 * AgentCoreNode and any other surface without tripping react-refresh's
 * "only export components" rule. Values resolve to the --node-* CSS variables
 * in index.css — the single source of truth shared with the minimap.
 */
export const NODE_ACCENT: Record<string, string> = {
  runtime: 'var(--node-runtime)',
  gateway: 'var(--node-gateway)',
  memory: 'var(--node-memory)',
  code_interpreter: 'var(--node-code_interpreter)',
  browser: 'var(--node-browser)',
  observability: 'var(--node-observability)',
  identity: 'var(--node-identity)',
  evaluation: 'var(--node-evaluation)',
  policy: 'var(--node-policy)',
  guardrails: 'var(--node-guardrails)',
  a2a: 'var(--node-a2a)',
  tool: 'var(--node-tool)',
};

export function accentFor(type: string): string {
  return NODE_ACCENT[type] || 'var(--node-default)';
}
