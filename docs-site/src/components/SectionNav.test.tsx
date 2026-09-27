import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { SectionNav } from './SectionNav';

const items = [
  { label: 'The Agent Factory', to: '/concepts/agent-factory/' },
  { label: 'Architecture', to: '/concepts/architecture/' },
];

describe('SectionNav', () => {
  it('renders a labelled nav with one link per item and marks the current page', () => {
    render(
      <MemoryRouter initialEntries={['/concepts/architecture/']}>
        <SectionNav label="Concepts section" items={items} />
      </MemoryRouter>,
    );
    const nav = screen.getByRole('navigation', { name: 'Concepts section' });
    expect(nav).toHaveTextContent('In this section:');
    expect(screen.getAllByRole('link')).toHaveLength(2);
    expect(screen.getByRole('link', { name: 'Architecture' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'The Agent Factory' })).not.toHaveAttribute('aria-current');
  });

  it('defaults the landmark name and ignores a missing trailing slash', () => {
    render(
      <MemoryRouter initialEntries={['/concepts/agent-factory']}>
        <SectionNav items={items} />
      </MemoryRouter>,
    );
    expect(screen.getByRole('navigation', { name: 'In this section' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'The Agent Factory' })).toHaveAttribute('aria-current', 'page');
  });
});
