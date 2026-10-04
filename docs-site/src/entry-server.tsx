import { StrictMode } from 'react';
import { renderToString } from 'react-dom/server';
import { createStaticHandler, createStaticRouter, StaticRouterProvider } from 'react-router-dom';
import { MetaProvider } from './components/PageMeta';
import { createMetaCollector } from './components/metaCollector';
import { routes } from './routes';

export { staticPaths } from './routes';
export { REDIRECTS } from './paths';

const basename = import.meta.env.BASE_URL.replace(/\/$/, '') || '/';

export interface RenderResult {
  html: string;
  /** 200, or 404 when the not-found page rendered. */
  status: number;
  meta: { title: string; description: string };
}

/**
 * Renders one URL (including the base path, e.g. "/sample-ai-agent-factory/start/")
 * to static HTML. Used only by scripts/prerender.mjs at build time.
 */
export async function render(url: string): Promise<RenderResult> {
  const handler = createStaticHandler(routes, { basename });
  const context = await handler.query(new Request(new URL(url, 'http://localhost')));

  if (context instanceof Response) {
    throw new Error(`Unexpected response (${context.status}) while rendering ${url}`);
  }
  if (context.errors) {
    const first = Object.values(context.errors)[0];
    throw first instanceof Error ? first : new Error(`Route error while rendering ${url}: ${String(first)}`);
  }

  const router = createStaticRouter(handler.dataRoutes, context);
  const collector = createMetaCollector();

  const html = renderToString(
    <StrictMode>
      <MetaProvider collector={collector}>
        <StaticRouterProvider router={router} context={context} hydrate={false} />
      </MetaProvider>
    </StrictMode>,
  );

  return {
    html,
    status: collector.status,
    meta: { title: collector.title, description: collector.description },
  };
}
