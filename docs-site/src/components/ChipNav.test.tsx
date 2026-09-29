import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { ChipNav } from './ChipNav';

describe('ChipNav', () => {
  it('marks the current page among router items and renders plain anchors for fragments', () => {
    render(
      <MemoryRouter initialEntries={['/concepts/architecture']}>
        <ChipNav
          label="Concepts section"
          lead="In this section:"
          items={[
            { label: 'Agent Factory', to: '/concepts/agent-factory/' },
            { label: 'Architecture', to: '/concepts/architecture/' },
            { label: 'Diagrams', href: '#diagrams' },
          ]}
        />
      </MemoryRouter>,
    );
    const nav = screen.getByRole('navigation', { name: 'Concepts section' });
    expect(nav).toHaveTextContent('In this section:');
    expect(nav).toHaveAttribute('data-overflow', 'scroll');
    expect(screen.getByRole('link', { name: 'Architecture' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Agent Factory' })).not.toHaveAttribute('aria-current');
    expect(screen.getByRole('link', { name: 'Diagrams' })).toHaveAttribute('href', '#diagrams');
  });

  it('exposes sticky and size as data attributes', () => {
    render(
      <MemoryRouter>
        <ChipNav label="Letters" size="sm" sticky overflow="wrap" items={[{ label: 'A', href: '#a' }]} />
      </MemoryRouter>,
    );
    const nav = screen.getByRole('navigation', { name: 'Letters' });
    expect(nav).toHaveAttribute('data-sticky', '');
    expect(nav).toHaveAttribute('data-size', 'sm');
    expect(nav).toHaveAttribute('data-overflow', 'wrap');
  });
});
