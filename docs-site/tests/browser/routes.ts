/**
 * Route inventory for the browser gates.
 *
 * Static routes come from the site's information architecture. Dynamic routes
 * (project pages, rendered docs) are discovered at run time from dist/sitemap.xml
 * so the specs never go stale when a doc is added or removed.
 */
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const BASE_PATH = '/sample-ai-agent-factory/';
/** Local runs may set PREVIEW_PORT to avoid sharing a preview server with another session; CI uses 4173. */
export const PREVIEW_PORT = Number(process.env.PREVIEW_PORT) || 4173;
export const SITE_ORIGIN = `http://127.0.0.1:${PREVIEW_PORT}`;
export const SITE_URL = `${SITE_ORIGIN}${BASE_PATH}`;
export const SITE_NAME = 'AI Agent Factory';

const HERE = dirname(fileURLToPath(import.meta.url));
export const DIST_DIR = resolve(HERE, '..', '..', 'dist');
export const SITEMAP_FILE = resolve(DIST_DIR, 'sitemap.xml');

export const PROJECT_IDS = ['workshop', 'self-service', 'mcp-gateway', 'blueprint'] as const;
export type ProjectId = (typeof PROJECT_IDS)[number];

/** Routes with a fixed path in the information architecture. */
export const STATIC_ROUTES: readonly string[] = [
  '/',
  '/start/',
  '/start/which-project/',
  '/start/prerequisites/',
  '/start/costs-and-cleanup/',
  '/start/faq/',
  '/concepts/agent-factory/',
  '/concepts/capability-contracts/',
  '/concepts/architecture/',
  '/concepts/glossary/',
  '/projects/',
  '/projects/self-service/changelog/',
  '/projects/mcp-gateway/policies/',
  '/projects/mcp-gateway/connectors/atlassian/',
  '/reference/security/',
  '/reference/support-envelope/',
  '/contributing/',
];

export const PROJECT_ROUTES: readonly string[] = PROJECT_IDS.map((id) => `/projects/${id}/`);
export const PROJECT_README_ROUTES: readonly string[] = PROJECT_IDS.map((id) => `/projects/${id}/readme/`);

/**
 * Every route the specs can name without reading the sitemap. Self-Service doc
 * slugs (/projects/self-service/docs/<slug>/) are only known from the sitemap.
 */
export const KNOWN_ROUTES: readonly string[] = [...STATIC_ROUTES, ...PROJECT_ROUTES, ...PROJECT_README_ROUTES];

/** Old HashRouter URLs and the page the inline hash shim must land on. */
export const LEGACY_HASH_REDIRECTS: ReadonlyArray<{ hash: string; route: string }> = [
  { hash: '#/security', route: '/reference/security/' },
  { hash: '#/choose-a-path', route: '/start/which-project/' },
  { hash: '#/how-it-works', route: '/concepts/agent-factory/' },
  { hash: '#/capabilities', route: '/concepts/capability-contracts/' },
  { hash: '#/architecture', route: '/concepts/architecture/' },
  { hash: '#/getting-started', route: '/start/' },
  { hash: '#/projects/blueprint', route: '/projects/blueprint/' },
  { hash: '#/', route: '/' },
];

/**
 * Pages whose body is repository Markdown rendered verbatim through MDX.
 * Site-copy wording rules (no em-dash, no "upstream" or "mirror") do not apply
 * to them because the source files are owned by the sub-projects.
 */
export function isVerbatimMarkdownRoute(route: string): boolean {
  return (
    route.endsWith('/readme/') ||
    route.startsWith('/projects/self-service/docs/') ||
    route === '/projects/self-service/changelog/' ||
    route === '/projects/mcp-gateway/connectors/atlassian/' ||
    route === '/contributing/'
  );
}

/** Absolute path (base path included) for a site route such as "/start/". */
export function href(route: string): string {
  return BASE_PATH + route.replace(/^\/+/, '');
}

export type SitemapResult = { ok: true; routes: readonly string[] } | { ok: false; error: string };

/** Reads dist/sitemap.xml and returns site routes ("/start/"), or a clear error. */
export function readSitemap(): SitemapResult {
  if (!existsSync(SITEMAP_FILE)) {
    return { ok: false, error: `${SITEMAP_FILE} not found. Run "npm run build" before "npm run test:browser".` };
  }
  const xml = readFileSync(SITEMAP_FILE, 'utf8');
  const locs = [...xml.matchAll(/<loc>\s*([^<\s]+)\s*<\/loc>/g)].map((match) => match[1]);
  if (locs.length === 0) {
    return { ok: false, error: `${SITEMAP_FILE} contains no <loc> entries.` };
  }
  const routes: string[] = [];
  for (const loc of locs) {
    let pathname: string;
    try {
      pathname = new URL(loc).pathname;
    } catch {
      return { ok: false, error: `sitemap <loc> is not an absolute URL: ${loc}` };
    }
    if (!pathname.startsWith(BASE_PATH)) {
      return { ok: false, error: `sitemap <loc> is outside the site base path ${BASE_PATH}: ${loc}` };
    }
    if (!pathname.endsWith('/')) {
      return { ok: false, error: `sitemap <loc> must end with a trailing slash: ${loc}` };
    }
    routes.push('/' + pathname.slice(BASE_PATH.length));
  }
  return { ok: true, routes: [...new Set(routes)] };
}

/** Routes from the sitemap; throws with a clear message when dist is missing. */
export function routesFromSitemap(): readonly string[] {
  const result = readSitemap();
  if (!result.ok) {
    throw new Error(result.error);
  }
  return result.routes;
}

/**
 * Routes for a full sweep. Prefers the sitemap; falls back to the known list so
 * the gate still runs (and the sitemap test still fails) when dist is incomplete.
 */
export function sweepRoutes(): { routes: readonly string[]; sitemap: SitemapResult } {
  const sitemap = readSitemap();
  return { routes: sitemap.ok ? sitemap.routes : KNOWN_ROUTES, sitemap };
}

export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
