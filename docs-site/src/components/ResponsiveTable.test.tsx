import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ResponsiveTable } from './ResponsiveTable';

describe('ResponsiveTable', () => {
  it('names the region from the header row when there is no caption', () => {
    render(
      <ResponsiveTable>
        <thead>
          <tr>
            <th>Capability</th>
            <th>Reference implementation</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>LLM Gateway</td>
            <td>LiteLLM</td>
          </tr>
        </tbody>
      </ResponsiveTable>,
    );
    const region = screen.getByRole('region', { name: 'Table: Capability, Reference implementation' });
    expect(region).not.toHaveAttribute('tabindex');
  });

  it('uses the aria-label handed down from the Markdown pipeline when present', () => {
    render(
      <ResponsiveTable aria-label="Table: Environment variables (2)">
        <thead>
          <tr>
            <th>Name</th>
          </tr>
        </thead>
      </ResponsiveTable>,
    );
    expect(screen.getByRole('region', { name: 'Table: Environment variables (2)' })).toBeInTheDocument();
    expect(screen.getByRole('table')).not.toHaveAttribute('aria-label');
  });

  it('prefers the caption and falls back to "Table"', () => {
    render(
      <ResponsiveTable>
        <caption>Costs by project</caption>
        <tbody>
          <tr>
            <td>x</td>
          </tr>
        </tbody>
      </ResponsiveTable>,
    );
    expect(screen.getByRole('region', { name: 'Costs by project' })).toBeInTheDocument();
    render(
      <ResponsiveTable>
        <tbody>
          <tr>
            <td>y</td>
          </tr>
        </tbody>
      </ResponsiveTable>,
    );
    expect(screen.getByRole('region', { name: 'Table' })).toBeInTheDocument();
  });
});
