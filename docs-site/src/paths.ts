/**
 * Canonical site routes (always with a trailing slash). Use these constants in
 * <Link to> so URLs match the prerendered directories and canonical tags.
 */
export const PATHS = {
  home: '/',
  start: '/start/',
  whichProject: '/start/which-project/',
  prerequisites: '/start/prerequisites/',
  costsAndCleanup: '/start/costs-and-cleanup/',
  faq: '/start/faq/',
  conceptsAgentFactory: '/concepts/agent-factory/',
  conceptsCapabilityContracts: '/concepts/capability-contracts/',
  conceptsArchitecture: '/concepts/architecture/',
  conceptsGlossary: '/concepts/glossary/',
  projects: '/projects/',
  mcpGatewayPolicies: '/projects/mcp-gateway/policies/',
  referenceSecurity: '/reference/security/',
  referenceSupportEnvelope: '/reference/support-envelope/',
  contributing: '/contributing/',
} as const;

export function projectPath(projectId: string): string {
  return `/projects/${projectId}/`;
}

export function projectReadmePath(projectId: string): string {
  return `/projects/${projectId}/readme/`;
}

/** Legacy hash-router paths and their canonical replacements. */
export const REDIRECTS: Readonly<Record<string, string>> = {
  '/choose-a-path': PATHS.whichProject,
  '/how-it-works': PATHS.conceptsAgentFactory,
  '/capabilities': PATHS.conceptsCapabilityContracts,
  '/architecture': PATHS.conceptsArchitecture,
  '/security': PATHS.referenceSecurity,
  '/getting-started': PATHS.start,
};

/** Route path (no leading or trailing slash) for a React Router route object. */
export function routePattern(path: string): string {
  return path.replace(/^\/+/, '').replace(/\/+$/, '');
}
