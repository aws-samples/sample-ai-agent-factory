import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Card } from './Card';

describe('Card', () => {
  it('renders a plain div card with medium padding by default', () => {
    render(<Card>Body</Card>);
    const card = screen.getByText('Body');
    expect(card.tagName).toBe('DIV');
    expect(card).toHaveAttribute('data-card');
    expect(card).toHaveAttribute('data-variant', 'plain');
    expect(card).toHaveAttribute('data-padding', 'md');
    expect(card).not.toHaveAttribute('data-lift');
    expect(card).not.toHaveAttribute('data-reveal');
  });

  it('renders the requested element with stage accent, lift and reveal', () => {
    render(
      <ul>
        <Card as="li" variant="accent" stage="govern" interactive reveal padding="sm" aria-label="MCP Gateway">
          Content
        </Card>
      </ul>,
    );
    const card = screen.getByRole('listitem', { name: 'MCP Gateway' });
    expect(card).toHaveAttribute('data-variant', 'accent');
    expect(card).toHaveAttribute('data-stage', 'govern');
    expect(card).toHaveAttribute('data-lift', '');
    expect(card).toHaveAttribute('data-reveal', '');
    expect(card).toHaveAttribute('data-padding', 'sm');
  });

  it('defaults the tinted variant to the blue tint', () => {
    render(<Card variant="tinted">Note</Card>);
    expect(screen.getByText('Note')).toHaveAttribute('data-tint', 'blue');
  });
});
