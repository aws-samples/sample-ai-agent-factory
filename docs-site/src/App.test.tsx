import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { createMemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { getProjectById, getProjectByStage, navigation, projects } from './content/data';
import { docs } from './content/docs';
import { docLoaders } from './docs/docModules';
import { PATHS, REDIRECTS } from './paths';
import { routes, staticPaths } from './routes';

const ROUTES = staticPaths();
const ALLOWED_INTERNAL_LINKS = new Set<string>([...ROUTES, ...Object.keys(REDIRECTS)]);
const ALLOWED_INTERNAL_ASSETS = new Set([
  '/repository-atlas-journey.svg',
  '/sample-ai-agent-factory/repository-atlas-journey.svg',
]);

/**
 * Renders a route in a memory router and waits for the page h1. Uses plain
 * DOM queries for the wait: testing-library role queries are far too slow on
 * the large DOM produced by a rendered README.
 */
async function renderRoute(route = '/') {
  const router = createMemoryRouter(routes, { initialEntries: [route] });
  const utils = render(<App router={router} />);
  await waitFor(() => expect(utils.container.querySelector('main h1')).not.toBeNull(), { timeout: 20_000 });
  return { ...utils, router };
}

function expectValidPage(container: HTMLElement) {
  const mains = container.querySelectorAll('main');
  expect(mains).toHaveLength(1);
  expect(mains[0]).toHaveAttribute('id', 'main-content');
  expect(mains[0]).toHaveAttribute('tabindex', '-1');
  expect(container.querySelectorAll('h1')).toHaveLength(1);
}

describe('routes and links', () => {
  it('enumerates every planned route', () => {
    expect(ROUTES).toContain('/');
    expect(ROUTES).toContain(PATHS.start);
    expect(ROUTES).toContain(PATHS.referenceSecurity);
    expect(ROUTES).toContain('/projects/workshop/');
    expect(ROUTES).toContain('/projects/self-service/docs/costs/');
    expect(ROUTES).toContain(PATHS.contributing);
    expect(ROUTES.every((route) => route.endsWith('/'))).toBe(true);
    expect(new Set(ROUTES).size).toBe(ROUTES.length);
    for (const doc of docs) expect(ROUTES).toContain(doc.route);
  });

  it.each(ROUTES)('renders %s with canonical landmarks, declared internal links and labelled new-tab links', async (route) => {
    const { container } = await renderRoute(route);
    expectValidPage(container);

    for (const link of container.querySelectorAll<HTMLAnchorElement>('a[href^="/"]')) {
      const href = (link.getAttribute('href') ?? '').split('#')[0];
      expect(ALLOWED_INTERNAL_LINKS.has(href) || ALLOWED_INTERNAL_ASSETS.has(href), `unexpected link ${href}`).toBe(true);
    }

    for (const link of container.querySelectorAll<HTMLAnchorElement>('a[target="_blank"]')) {
      expect(link.getAttribute('rel')).toContain('noopener');
      expect(link.textContent, `link ${link.getAttribute('href')}`).toMatch(/opens in new tab/i);
    }

    for (const image of container.querySelectorAll('img')) {
      expect(image.hasAttribute('alt'), `image without alt: ${image.getAttribute('src')}`).toBe(true);
      expect(image.getAttribute('src') ?? '', 'externally hosted image').not.toMatch(/^https?:\/\//);
    }
  });

  it('renders the not-found page for unknown routes', async () => {
    const { container } = await renderRoute('/unknown-route');
    expectValidPage(container);
    expect(screen.getByRole('heading', { level: 1, name: /not found/i })).toBeInTheDocument();
  });

  it.each(Object.entries(REDIRECTS))('redirects legacy %s to %s', async (from, to) => {
    const { router, container } = await renderRoute(from);
    await waitFor(() => expect(router.state.location.pathname).toBe(to));
    expectValidPage(container);
  });
});

describe('route change behaviour', () => {
  it('keeps the route and focuses main when the skip link is used', async () => {
    const { router } = await renderRoute('/');
    const skipLink = screen.getByText(/skip to main content/i);
    expect(skipLink).toHaveAttribute('href', '#main-content');

    fireEvent.click(skipLink);

    expect(screen.getByRole('main')).toHaveFocus();
    expect(router.state.location.pathname).toBe('/');
    expect(router.state.location.hash).toBe('');
  });

  it('sets the document title, resets scroll and moves focus to main on navigation', async () => {
    const scrollTo = vi.mocked(window.scrollTo);
    scrollTo.mockClear();
    const { router } = await renderRoute('/');
    expect(document.title).toBe('AI Agent Factory: enterprise agentic AI samples on AWS');

    const startLink = screen.getByRole('navigation', { name: /main navigation/i }).querySelector('a[href*="/start/"]');
    expect(startLink).not.toBeNull();
    fireEvent.click(startLink as HTMLAnchorElement);

    await waitFor(() => expect(router.state.location.pathname).toBe(PATHS.start));
    await waitFor(() => expect(document.title).toBe('Getting started | AI Agent Factory'));
    await waitFor(() => expect(screen.getByRole('main')).toHaveFocus());
    expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({ top: 0 }));
    // Pages may carry their own status regions (CodeBlock copy feedback), so target the route announcer.
    expect(document.getElementById('route-announcer')).toHaveTextContent(/navigated to getting started/i);
  });
});

describe('navigation accessibility', () => {
  it('opens the mobile menu as a dialog, marks main and footer inert, focuses the first link, and closes on Escape', async () => {
    const { container } = await renderRoute('/');
    const menuButton = screen.getByRole('button', { name: /open menu/i });
    fireEvent.click(menuButton);

    const dialog = screen.getByRole('dialog', { name: /site navigation/i });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByRole('navigation', { name: /mobile navigation/i })).toBeInTheDocument();
    expect(container.querySelector('main')).toHaveAttribute('inert');
    expect(container.querySelector('footer')).toHaveAttribute('inert');
    await waitFor(() => expect(dialog.querySelector('a')).toHaveFocus());

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(menuButton).toHaveFocus();
    });
    expect(container.querySelector('main')).not.toHaveAttribute('inert');
  });

  it('closes the projects dropdown on Escape and returns focus to the trigger', async () => {
    await renderRoute('/');
    const projectsButton = screen.getByRole('button', { name: /^projects/i });
    fireEvent.click(projectsButton);
    expect(projectsButton).toHaveAttribute('aria-expanded', 'true');

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => {
      expect(projectsButton).toHaveAttribute('aria-expanded', 'false');
      expect(projectsButton).toHaveFocus();
    });
  });

  it('closes the projects dropdown on pointer-down outside it', async () => {
    await renderRoute('/');
    const projectsButton = screen.getByRole('button', { name: /^projects/i });
    fireEvent.click(projectsButton);
    expect(projectsButton).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('link', { name: /all projects/i })).toBeInTheDocument();

    fireEvent.pointerDown(document.body);
    await waitFor(() => expect(projectsButton).toHaveAttribute('aria-expanded', 'false'));
  });

  it('closes the projects dropdown when focus leaves it', async () => {
    const { container } = await renderRoute('/');
    const projectsButton = screen.getByRole('button', { name: /^projects/i });
    fireEvent.click(projectsButton);
    const menu = container.querySelector('#dropdown-projects');
    expect(menu).not.toBeNull();
    const lastLink = [...(menu as HTMLElement).querySelectorAll('a')].at(-1) as HTMLAnchorElement;
    lastLink.focus();
    fireEvent.blur(lastLink, { relatedTarget: container.querySelector('main') });
    await waitFor(() => expect(projectsButton).toHaveAttribute('aria-expanded', 'false'));
  });
});

describe('automated accessibility', () => {
  it.each(['/', PATHS.whichProject, PATHS.referenceSecurity, '/projects/self-service/docs/data-retention/'])(
    'has no serious or critical axe findings on %s',
    async (route) => {
      const { container } = await renderRoute(route);
      const result = await axe.run(container, {
        rules: {
          // jsdom has no layout engine; colour contrast is checked in the browser suite.
          'color-contrast': { enabled: false },
        },
      });
      const actionable = result.violations.filter(({ impact }) => impact === 'serious' || impact === 'critical');
      expect(actionable.map(({ id, nodes }) => ({ id, targets: nodes.map(({ target }) => target) }))).toEqual([]);
    },
  );
});

describe('content model', () => {
  it('defines four unique journey projects', () => {
    expect(projects).toHaveLength(4);
    expect(new Set(projects.map(({ id }) => id)).size).toBe(projects.length);
    expect(
      projects.every(({ name, shortName, description, stage, folder }) => name && shortName && description && stage && folder),
    ).toBe(true);
  });

  it('resolves projects by id and stage', () => {
    expect(getProjectById('workshop')?.shortName).toBe('Workshop');
    expect(getProjectById('blueprint')?.shortName).toBe('Blueprint');
    expect(getProjectById('missing')).toBeUndefined();
    expect(getProjectByStage('learn')?.id).toBe('workshop');
    expect(getProjectByStage('build')?.id).toBe('self-service');
    expect(getProjectByStage('govern')?.id).toBe('mcp-gateway');
    expect(getProjectByStage('scale')?.id).toBe('blueprint');
  });

  it('keeps data.ts navigation inside the routed set', () => {
    const navPaths = navigation.flatMap(({ path, children = [] }) => [path, ...children.map((child) => child.path)]);
    for (const p of navPaths) {
      const canonical = p === '/' || p.endsWith('/') ? p : `${p}/`;
      expect(ALLOWED_INTERNAL_LINKS.has(canonical), `navigation path ${p} is not routed`).toBe(true);
    }
  });

  it('keeps the doc index and the MDX loader map in sync', () => {
    const indexed = docs.map((d) => d.sourcePath).sort();
    const loaded = Object.keys(docLoaders).sort();
    expect(loaded).toEqual(indexed);
    expect(new Set(docs.map((d) => d.route)).size).toBe(docs.length);
    expect(docs.filter((d) => d.project === 'self-service' && d.kind === 'doc')).toHaveLength(12);
    expect(docs.some((d) => d.sourcePath.includes('MCP_CATALOG'))).toBe(false);
  });
});
