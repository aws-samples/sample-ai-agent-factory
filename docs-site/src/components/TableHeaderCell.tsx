import { Children, type ReactNode, type ThHTMLAttributes } from 'react';

function hasText(children: ReactNode): boolean {
  return Children.toArray(children).some((child) =>
    typeof child === 'string' || typeof child === 'number' ? String(child).trim() !== '' : true,
  );
}

/**
 * Header cell for Markdown tables. GitHub-flavoured tables often leave the first header
 * cell blank (a corner above a label column); an empty `<th>` has no accessible name, so
 * such cells become plain `<td>` cells instead.
 */
export function TableHeaderCell({ children, ...rest }: ThHTMLAttributes<HTMLTableCellElement>) {
  if (!hasText(children)) return <td {...rest} />;
  return <th {...rest}>{children}</th>;
}
