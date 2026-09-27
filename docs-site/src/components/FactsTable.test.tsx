import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FactsTable } from './FactsTable';

describe('FactsTable', () => {
  it('is a region named by its caption with one row per fact that cites a source or says "not documented"', () => {
    render(
      <FactsTable
        rows={[
          {
            label: 'First deploy',
            fact: {
              value: 'About 5 minutes',
              source: { file: 'enterprise-mcp-governance-gateway/README.md', heading: 'Quick start' },
            },
          },
          {
            label: 'Version',
            fact: { value: 'not documented', notDocumented: true, note: 'No tag or CHANGELOG.' },
          },
        ]}
      />,
    );

    const region = screen.getByRole('region', { name: 'Facts' });
    const [header, deploy, version] = within(region).getAllByRole('row');
    expect(header).toHaveTextContent(/^FactValueSource$/);

    expect(within(deploy).getByRole('rowheader')).toHaveTextContent('First deploy');
    expect(deploy.textContent?.startsWith('First deploy')).toBe(true);
    expect(within(deploy).getByRole('link')).toHaveAttribute(
      'href',
      expect.stringMatching(/^https:\/\/github\.com\/aws-samples\/sample-ai-agent-factory\/blob\/main\//),
    );

    expect(within(version).getByRole('rowheader')).toHaveTextContent('Version');
    expect(version).toHaveTextContent(/not documented/i);
    expect(version).toHaveTextContent('No tag or CHANGELOG.');
    expect(within(version).queryByRole('link')).toBeNull();
  });

  it('uses a custom caption as the region name', () => {
    render(<FactsTable caption="Workshop facts" rows={[]} />);
    expect(screen.getByRole('region', { name: 'Workshop facts' })).toBeInTheDocument();
  });
});
