import { fireEvent, render, screen } from '@testing-library/react';
import { ArrowRight } from 'lucide-react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { Button } from './Button';

describe('Button', () => {
  it('renders a router link with variant and size data attributes', () => {
    render(
      <MemoryRouter>
        <Button to="/start/" variant="secondary" size="lg" iconEnd={<ArrowRight />}>
          Get started
        </Button>
      </MemoryRouter>,
    );
    const link = screen.getByRole('link', { name: 'Get started' });
    expect(link).toHaveAttribute('href', '/start/');
    expect(link).toHaveAttribute('data-variant', 'secondary');
    expect(link).toHaveAttribute('data-size', 'lg');
    expect(link.querySelector('[data-icon-end]')).toHaveAttribute('aria-hidden', 'true');
  });

  it('renders an external anchor that opens in a new tab and says so', () => {
    render(
      <Button href="https://github.com/aws-samples/sample-ai-agent-factory" external>
        Source on GitHub
      </Button>,
    );
    const link = screen.getByRole('link', { name: 'Source on GitHub (opens in new tab)' });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(link).toHaveAttribute('data-variant', 'primary');
  });

  it('renders a plain anchor for in-page fragments', () => {
    render(<Button href="#quickstart">Quickstart</Button>);
    const link = screen.getByRole('link', { name: 'Quickstart' });
    expect(link).toHaveAttribute('href', '#quickstart');
    expect(link).not.toHaveAttribute('target');
  });

  it('renders a real button that defaults to type="button" and forwards clicks', () => {
    const onClick = vi.fn();
    render(
      <Button variant="ghost" onClick={onClick} aria-pressed="false">
        Pause
      </Button>,
    );
    const button = screen.getByRole('button', { name: 'Pause' });
    expect(button).toHaveAttribute('type', 'button');
    expect(button).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
