import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PageHeader } from './PageHeader';

describe('PageHeader', () => {
  it('renders the eyebrow, a single h1, the lead and the actions on a dark band', () => {
    render(
      <PageHeader
        eyebrow="Start"
        title="Getting started"
        lead="Pick a project and deploy it."
        actions={<a href="/start/which-project/">Which project?</a>}
      />,
    );
    const banner = screen.getByRole('banner');
    expect(banner).toHaveClass('on-dark');
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Getting started');
    expect(screen.getByText('Start')).toBeInTheDocument();
    expect(screen.getByText('Pick a project and deploy it.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Which project?' })).toBeInTheDocument();
  });

  it('renders a stage badge with the default label and an optional figure', () => {
    render(<PageHeader title="Workshop" stage="learn" figure={<img alt="Workshop journey" src="/x.svg" />} />);
    expect(screen.getByText('Learn')).toHaveAttribute('data-stage', 'learn');
    expect(screen.getByRole('img', { name: 'Workshop journey' })).toBeInTheDocument();
  });

  it('renders the meta slot between the lead and the actions', () => {
    const { container } = render(
      <PageHeader title="Workshop" lead="Lead text" meta={<dl data-testid="facts" />} actions={<a href="/x/">Go</a>} />,
    );
    const order = Array.from(container.querySelectorAll('p, [data-testid="facts"], a')).map((el) => el.tagName);
    expect(order).toEqual(['P', 'DL', 'A']);
  });

  it('lets pages override the badge label', () => {
    render(<PageHeader title="MCP Gateway" stage="govern" stageLabel="3. Govern" />);
    expect(screen.getByText('3. Govern')).toHaveAttribute('data-stage', 'govern');
    expect(screen.queryByText('Govern')).not.toBeInTheDocument();
  });

  it('omits the optional parts when they are not provided', () => {
    const { container } = render(<PageHeader title="Plain" />);
    expect(container.querySelector('p')).toBeNull();
    expect(container.querySelector('[data-stage]')).toBeNull();
  });
});
