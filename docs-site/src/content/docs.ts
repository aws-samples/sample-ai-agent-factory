/**
 * Doc index: every repository Markdown file rendered on the site.
 *
 * Pure data. This module is imported by vite.config.ts (Node) as well as by
 * browser code, so it must not import Vite virtual modules, MDX files or React.
 * Titles are NOT stored here: they are read from the first h1 of each file at
 * build time by src/vite-plugins/repo-index.ts (`docTitles`). The MDX loader
 * map lives in src/docs/docModules.ts.
 */

export type ProjectId = 'workshop' | 'self-service' | 'mcp-gateway' | 'blueprint';

export type DocKind = 'readme' | 'doc' | 'changelog' | 'connector' | 'contributing';

export interface DocEntry {
  /** Stable id, e.g. "self-service/docs/costs". */
  id: string;
  /** Owning project, or null for repository-level documents. */
  project: ProjectId | null;
  kind: DocKind;
  /** Last URL segment (kebab-case of the file name for docs). */
  slug: string;
  /** Repo-relative source path. */
  sourcePath: string;
  /** Site route with a trailing slash. */
  route: string;
  /** Short label for sidebars and prev/next; defaults to the h1 title. */
  navLabel?: string;
}

export const PROJECT_FOLDERS: Record<ProjectId, string> = {
  workshop: 'workshop-building-agentic-ai-platform',
  'self-service': 'Agentic-ai-self-service',
  'mcp-gateway': 'enterprise-mcp-governance-gateway',
  blueprint: 'enterprise-agentic-ai-platform-blueprint',
};

export const PROJECT_IDS = Object.keys(PROJECT_FOLDERS) as ProjectId[];

/** Self-Service docs rendered on the site. MCP_CATALOG.md is deliberately excluded. */
const SELF_SERVICE_DOC_FILES = [
  'API_REFERENCE.md',
  'COSTS.md',
  'DATA_RETENTION.md',
  'DEPLOYMENT_INTERNALS.md',
  'DEVELOPMENT.md',
  'ENTERPRISE_CAPABILITIES.md',
  'MCP_GATEWAY_INTEGRATION.md',
  'OBSERVABILITY.md',
  'PERSONAS.md',
  'RBAC_ROLLOUT.md',
  'REGISTRY_AND_RBAC.md',
  'SECURITY_HARDENING.md',
] as const;

export function kebabCase(fileName: string): string {
  return fileName
    .replace(/\.md$/i, '')
    .replace(/[_\s]+/g, '-')
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .toLowerCase();
}

const readmeEntries: DocEntry[] = PROJECT_IDS.map((id) => ({
  id: `${id}/readme`,
  project: id,
  kind: 'readme',
  slug: 'readme',
  sourcePath: `${PROJECT_FOLDERS[id]}/README.md`,
  route: `/projects/${id}/readme/`,
  navLabel: 'README',
}));

const selfServiceDocEntries: DocEntry[] = SELF_SERVICE_DOC_FILES.map((file) => {
  const slug = kebabCase(file);
  return {
    id: `self-service/docs/${slug}`,
    project: 'self-service',
    kind: 'doc',
    slug,
    sourcePath: `${PROJECT_FOLDERS['self-service']}/docs/${file}`,
    route: `/projects/self-service/docs/${slug}/`,
  };
});

export const docs: readonly DocEntry[] = [
  ...readmeEntries,
  ...selfServiceDocEntries,
  {
    id: 'self-service/changelog',
    project: 'self-service',
    kind: 'changelog',
    slug: 'changelog',
    sourcePath: `${PROJECT_FOLDERS['self-service']}/CHANGELOG.md`,
    route: '/projects/self-service/changelog/',
    navLabel: 'Changelog',
  },
  {
    id: 'mcp-gateway/connectors/atlassian',
    project: 'mcp-gateway',
    kind: 'connector',
    slug: 'atlassian',
    sourcePath: `${PROJECT_FOLDERS['mcp-gateway']}/connectors/atlassian/README.md`,
    route: '/projects/mcp-gateway/connectors/atlassian/',
    navLabel: 'Atlassian connector',
  },
  {
    id: 'contributing',
    project: null,
    kind: 'contributing',
    slug: 'contributing',
    sourcePath: 'CONTRIBUTING.md',
    route: '/contributing/',
    navLabel: 'Contributing',
  },
];

const bySource = new Map(docs.map((d) => [d.sourcePath, d]));
const byRoute = new Map(docs.map((d) => [d.route, d]));
const byId = new Map(docs.map((d) => [d.id, d]));

/** Site route for a repo-relative Markdown path, or undefined when the file is not rendered. */
export function routeForSourcePath(sourcePath: string): string | undefined {
  return bySource.get(sourcePath.replace(/^\/+/, ''))?.route;
}

export function docByRoute(route: string): DocEntry | undefined {
  return byRoute.get(route);
}

export function docById(id: string): DocEntry | undefined {
  return byId.get(id);
}

/** Docs for one project in sidebar order: README, docs (alphabetical), changelog, connectors. */
export function docsForProject(project: ProjectId): DocEntry[] {
  return docs.filter((d) => d.project === project);
}
