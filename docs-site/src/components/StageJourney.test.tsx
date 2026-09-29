import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { projects } from '../content/data';
import { StageJourney } from './StageJourney';

describe('StageJourney', () => {
  it('renders the four stages in order as links to their project pages', () => {
    render(
      <MemoryRouter>
        <StageJourney />
      </MemoryRouter>,
    );
    const list = screen.getByRole('list', { name: 'Journey stages' });
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(4);
    expect(items.map((item) => item.getAttribute('data-stage'))).toEqual(['learn', 'build', 'govern', 'scale']);
    const links = screen.getAllByRole('link');
    const ordered = [...projects].sort((a, b) => a.stageNumber - b.stageNumber);
    expect(links.map((link) => link.getAttribute('href'))).toEqual(ordered.map((project) => project.route));
    expect(links[0]).toHaveTextContent('1. Learn');
    expect(links[0]).toHaveTextContent(ordered[0].shortName);
    expect(list.querySelectorAll('svg[aria-hidden], [aria-hidden="true"] svg').length).toBeGreaterThan(0);
  });

  it('can hide project names and shrink', () => {
    render(
      <MemoryRouter>
        <StageJourney showProjects={false} size="sm" label="Stages" />
      </MemoryRouter>,
    );
    const list = screen.getByRole('list', { name: 'Stages' });
    expect(list).toHaveAttribute('data-size', 'sm');
    expect(screen.queryByText(projects[0].shortName)).toBeNull();
  });
});
