import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { AppRoutes } from './App';
import { getProjectById, getProjectByStage, navigation, projects } from './content/data';

const ROUTES = [
  '/',
  '/choose-a-path',
  '/how-it-works',
  '/projects',
  '/projects/workshop',
  '/projects/self-service',
  '/projects/mcp-gateway',
  '/projects/blueprint',
  '/capabilities',
  '/architecture',
  '/security',
  '/getting-started',
] as const;

const ALLOWED_INTERNAL_LINKS = new Set<string>(ROUTES);
const ALLOWED_INTERNAL_ASSETS = new Set([
  '/repository-atlas-journey.svg',
  '/sample-ai-agent-factory/repository-atlas-journey.svg',
]);

function renderRoute(route = '/') {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AppRoutes />
    </MemoryRouter>,
  );
}

function expectValidPage() {
  expect(screen.getAllByRole('main')).toHaveLength(1);
  expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content');
  expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
}

describe('routes and links', () => {
  it.each(ROUTES)('renders %s with canonical landmarks', (route) => {
    renderRoute(route);
    expectValidPage();
  });

  it('renders the not-found page for unknown routes', () => {
    renderRoute('/unknown-route');
    expectValidPage();
    expect(screen.getByRole('heading', { level: 1, name: /not found/i })).toBeInTheDocument();
  });

  it.each(ROUTES)('uses only declared internal links on %s', (route) => {
    const { container } = renderRoute(route);
    const internalLinks = [...container.querySelectorAll<HTMLAnchorElement>('a[href^="/"]')];

    for (const link of internalLinks) {
      const href = link.getAttribute('href') ?? '';
      expect(ALLOWED_INTERNAL_LINKS.has(href) || ALLOWED_INTERNAL_ASSETS.has(href)).toBe(true);
    }
  });
});

describe('navigation accessibility', () => {
  it('connects the skip link to the main landmark', () => {
    renderRoute('/');
    expect(screen.getByText(/skip to main content/i)).toHaveAttribute('href', '#main-content');
  });

  it('opens the mobile menu and returns focus after Escape', async () => {
    renderRoute('/');
    const menuButton = screen.getByRole('button', { name: /open menu/i });
    fireEvent.click(menuButton);
    expect(screen.getByRole('navigation', { name: /mobile navigation/i })).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByRole('navigation', { name: /mobile navigation/i })).not.toBeInTheDocument();
      expect(menuButton).toHaveFocus();
    });
  });

  it('returns focus to the projects trigger after Escape', async () => {
    renderRoute('/');
    const projectsButton = screen.getByRole('button', { name: /^projects/i });
    fireEvent.click(projectsButton);
    expect(projectsButton).toHaveAttribute('aria-expanded', 'true');

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => {
      expect(projectsButton).toHaveAttribute('aria-expanded', 'false');
      expect(projectsButton).toHaveFocus();
    });
  });
});

describe('automated accessibility', () => {
  it.each(['/', '/choose-a-path', '/security'] as const)('has no serious or critical axe findings on %s', async (route) => {
    const { container } = renderRoute(route);
    const result = await axe.run(container, {
      rules: {
        'color-contrast': { enabled: false },
      },
    });
    const actionable = result.violations.filter(
      ({ impact }) => impact === 'serious' || impact === 'critical',
    );
    expect(actionable.map(({ id, nodes }) => ({ id, targets: nodes.map(({ target }) => target) }))).toEqual([]);
  });
});

describe('content model', () => {
  it('defines four unique journey projects', () => {
    expect(projects).toHaveLength(4);
    expect(new Set(projects.map(({ id }) => id)).size).toBe(projects.length);
    expect(projects.every(({ name, shortName, description, stage, folder }) => name && shortName && description && stage && folder)).toBe(true);
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

  it('keeps navigation routes inside the tested route set', () => {
    const navPaths = navigation.flatMap(({ path, children = [] }) => [path, ...children.map(({ path: childPath }) => childPath)]);
    expect(navPaths.every((path) => ALLOWED_INTERNAL_LINKS.has(path as (typeof ROUTES)[number]))).toBe(true);
  });
});
