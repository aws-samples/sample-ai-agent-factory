import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { NumberedSteps } from './NumberedSteps';

describe('NumberedSteps', () => {
  it('renders an ordered list whose items keep native semantics', () => {
    render(
      <NumberedSteps stage="build" connector aria-label="Deploy steps">
        <NumberedSteps.Item title="Clone the repository">
          <p>Folder names are exact.</p>
        </NumberedSteps.Item>
        <NumberedSteps.Item title="Deploy" />
      </NumberedSteps>,
    );
    const list = screen.getByRole('list', { name: 'Deploy steps' });
    expect(list.tagName).toBe('OL');
    expect(list).toHaveAttribute('data-stage', 'build');
    expect(list).toHaveAttribute('data-connector', '');
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
    expect(screen.getByText('Clone the repository')).toBeInTheDocument();
    expect(screen.getByText('Folder names are exact.')).toBeInTheDocument();
  });

  it('continues numbering from start via a CSS counter reset', () => {
    render(
      <NumberedSteps start={4} dense>
        <NumberedSteps.Item title="Fourth" />
      </NumberedSteps>,
    );
    const list = screen.getByRole('list');
    expect(list).toHaveStyle({ counterReset: 'step 3' });
    expect(list).toHaveAttribute('data-dense', '');
  });
});
