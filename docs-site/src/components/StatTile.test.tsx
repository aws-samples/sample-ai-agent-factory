import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Fact } from '../content/facts';
import { FactStat, StatTile } from './StatTile';

const fact: Fact = {
  value: 'About 5 minutes',
  note: 'The gateway stack itself takes about 2 minutes.',
  source: { file: 'enterprise-mcp-governance-gateway/README.md', heading: 'Quick start', quote: 'about 5 minutes' },
};

describe('StatTile', () => {
  it('renders a dt/dd pair with a source link that carries hidden context', () => {
    render(
      <dl>
        <StatTile label="First deploy" value="About 5 minutes" source={fact.source} context="first deploy, MCP Gateway" />
      </dl>,
    );
    expect(screen.getByRole('term')).toHaveTextContent('First deploy');
    expect(screen.getByRole('definition')).toHaveTextContent('About 5 minutes');
    const link = screen.getByRole('link', { name: 'source for first deploy, MCP Gateway (opens in new tab)' });
    expect(link).toHaveAttribute('href', expect.stringContaining('enterprise-mcp-governance-gateway/README.md'));
  });

  it('collapses the note behind a disclosure when asked', () => {
    render(
      <dl>
        <FactStat label="First deploy" fact={fact} project="MCP Gateway" noteMode="collapsed" />
      </dl>,
    );
    expect(screen.getByText('Why this figure', { exact: false })).toBeInTheDocument();
    const details = screen.getByText(fact.note!).closest('details');
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute('open');
    expect(screen.getByRole('link', { name: 'source for first deploy, MCP Gateway (opens in new tab)' })).toBeInTheDocument();
  });

  it('renders not-documented facts muted and without a source link', () => {
    render(
      <dl>
        <FactStat label="Cost" fact={{ value: '', notDocumented: true, note: 'No figure is published.' }} />
      </dl>,
    );
    expect(screen.getByRole('definition')).toHaveTextContent('not documented');
    expect(screen.getByText('No figure is published.')).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });
});
