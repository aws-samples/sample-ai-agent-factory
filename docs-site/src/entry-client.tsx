import { StrictMode } from 'react';
import { createRoot, hydrateRoot } from 'react-dom/client';
import { createBrowserRouter, matchRoutes } from 'react-router-dom';
import { App } from './App';
import { routes } from './routes';
import './styles/tokens.css';

const basename = import.meta.env.BASE_URL.replace(/\/$/, '') || '/';

async function preloadLazyMatches(): Promise<void> {
  const matches = matchRoutes(routes, window.location, basename) ?? [];
  await Promise.all(
    matches.map(async ({ route }) => {
      if (typeof route.lazy !== 'function') return;
      const loaded = await route.lazy();
      Object.assign(route, { ...loaded, lazy: undefined });
    }),
  );
}

async function main(): Promise<void> {
  // Resolve lazy route modules for the current URL first so hydration renders
  // the same tree the prerender produced.
  await preloadLazyMatches();

  const router = createBrowserRouter(routes, { basename });
  const rootElement = document.getElementById('root');
  if (!rootElement) {
    throw new Error('Root element not found');
  }

  const app = (
    <StrictMode>
      <App router={router} />
    </StrictMode>
  );

  if (rootElement.firstElementChild) {
    hydrateRoot(rootElement, app);
  } else {
    createRoot(rootElement).render(app);
  }
}

void main();
