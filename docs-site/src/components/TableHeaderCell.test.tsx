import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { TableHeaderCell } from './TableHeaderCell';

function renderRow(cells: React.ReactNode) {
  const { container } = render(
    <table>
      <thead>
        <tr>{cells}</tr>
      </thead>
    </table>,
  );
  return container.querySelector('tr') as HTMLTableRowElement;
}

describe('TableHeaderCell', () => {
  it('keeps header cells that have text and demotes blank ones to td', () => {
    const row = renderRow(
      <>
        <TableHeaderCell> </TableHeaderCell>
        <TableHeaderCell>Region</TableHeaderCell>
        <TableHeaderCell>
          <code>us-east-1</code>
        </TableHeaderCell>
      </>,
    );
    expect(Array.from(row.children).map((cell) => cell.tagName)).toEqual(['TD', 'TH', 'TH']);
  });
});
