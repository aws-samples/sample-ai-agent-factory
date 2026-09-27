import { Navigate, type RouteObject } from 'react-router-dom';
import { DocPage } from './components/DocPage';
import { Layout } from './components/Layout';
import { projects } from './content/data';
import { docs } from './content/docs';
import { loadDoc } from './docs/docModules';
import { PATHS, REDIRECTS, routePattern } from './paths';
import { ArchitecturePage } from './pages/concepts/ArchitecturePage';
import { AgentFactoryPage } from './pages/concepts/AgentFactoryPage';
import { CapabilityContractsPage } from './pages/concepts/CapabilityContractsPage';
import { GlossaryPage } from './pages/concepts/GlossaryPage';
import { HomePage } from './pages/home/HomePage';
import { NotFoundPage } from './pages/NotFoundPage';
import { ProjectsPage } from './pages/projects/ProjectsPage';
import { SecurityPage } from './pages/reference/SecurityPage';
import { SupportEnvelopePage } from './pages/reference/SupportEnvelopePage';
import { CostsAndCleanupPage } from './pages/start/CostsAndCleanupPage';
import { FaqPage } from './pages/start/FaqPage';
import { PrerequisitesPage } from './pages/start/PrerequisitesPage';
import { StartPage } from './pages/start/StartPage';
import { WhichProjectPage } from './pages/start/WhichProjectPage';

/** Rendered repository Markdown: one lazy route per doc index entry. */
const docRoutes: RouteObject[] = docs.map((entry) => ({
  path: routePattern(entry.route),
  lazy: async () => {
    const mod = await loadDoc(entry);
    return { element: <DocPage entry={entry} mod={mod} /> };
  },
}));

const redirectRoutes: RouteObject[] = Object.entries(REDIRECTS).map(([from, to]) => ({
  path: routePattern(from),
  element: <Navigate to={to} replace />,
}));

/** Hub pages keyed by canonical path (static imports unless noted). */
const hubRoutes: RouteObject[] = [
  { path: routePattern(PATHS.start), element: <StartPage /> },
  { path: routePattern(PATHS.whichProject), element: <WhichProjectPage /> },
  { path: routePattern(PATHS.prerequisites), element: <PrerequisitesPage /> },
  { path: routePattern(PATHS.costsAndCleanup), element: <CostsAndCleanupPage /> },
  { path: routePattern(PATHS.faq), element: <FaqPage /> },
  { path: routePattern(PATHS.conceptsAgentFactory), element: <AgentFactoryPage /> },
  { path: routePattern(PATHS.conceptsCapabilityContracts), element: <CapabilityContractsPage /> },
  { path: routePattern(PATHS.conceptsArchitecture), element: <ArchitecturePage /> },
  { path: routePattern(PATHS.conceptsGlossary), element: <GlossaryPage /> },
  { path: routePattern(PATHS.projects), element: <ProjectsPage /> },
  // Lazy: these pull in virtual:repo-index (notebooks, Cedar policy text) and the
  // per-project section components, which would otherwise sit in the main bundle.
  {
    path: 'projects/:projectId',
    lazy: async () => ({ Component: (await import('./pages/projects/ProjectDetailPage')).ProjectDetailPage }),
  },
  {
    path: routePattern(PATHS.mcpGatewayPolicies),
    lazy: async () => ({ Component: (await import('./pages/projects/PoliciesPage')).PoliciesPage }),
  },
  { path: routePattern(PATHS.referenceSecurity), element: <SecurityPage /> },
  { path: routePattern(PATHS.referenceSupportEnvelope), element: <SupportEnvelopePage /> },
];

/** The single route table used by the client, the prerender and the tests. */
export const routes: RouteObject[] = [
  {
    path: '/',
    element: <Layout />,
    hydrateFallbackElement: <p className="container page-section">Loading</p>,
    children: [
      { index: true, element: <HomePage /> },
      ...hubRoutes,
      ...docRoutes,
      ...redirectRoutes,
      { path: '*', element: <NotFoundPage /> },
    ],
  },
];

const REDIRECT_PATTERNS = new Set(Object.keys(REDIRECTS).map(routePattern));

/**
 * Every canonical page path (trailing slash), with `:projectId` expanded from
 * data.ts. Redirects and the catch-all are excluded. Throws on any other
 * dynamic segment so new params cannot be silently skipped by the prerender.
 */
export function staticPaths(): string[] {
  const out: string[] = [];
  const walk = (list: RouteObject[], prefix: string) => {
    for (const route of list) {
      if (route.index) {
        out.push(prefix || '/');
        continue;
      }
      const segment = route.path ?? '';
      if (segment === '*' || REDIRECT_PATTERNS.has(segment)) continue;
      const full = segment === '/' ? '' : `${prefix}/${segment}`.replace(/\/+/g, '/');
      const expanded = expandParams(full);
      if (route.children) {
        for (const p of expanded) walk(route.children, p);
      } else if (route.element || route.lazy || route.Component) {
        for (const p of expanded) out.push(p.endsWith('/') ? p : `${p}/`);
      }
    }
  };
  walk(routes, '');
  return Array.from(new Set(out));
}

function expandParams(pattern: string): string[] {
  if (!pattern.includes(':')) return [pattern];
  const params = pattern.match(/:[A-Za-z0-9_]+/g) ?? [];
  let results = [pattern];
  for (const param of params) {
    if (param !== ':projectId') {
      throw new Error(`staticPaths(): no expansion defined for route param ${param} in ${pattern}`);
    }
    results = results.flatMap((p) => projects.map((project) => p.replace(param, project.id)));
  }
  return results;
}
