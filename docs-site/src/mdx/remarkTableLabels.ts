import type { Root, Table, TableCell } from 'mdast';
import { toString } from 'mdast-util-to-string';
import type { Plugin } from 'unified';
import { normaliseDashes } from './normaliseDashes';

const MAX_HEADERS = 6;

/**
 * Gives every Markdown table an accessible name for its scroll region: "Table: <nearest
 * preceding heading>", else "Table: <header cells>", else "Table". Names are made unique
 * within the document ("... (2)", "... (3)") so the regions never fail axe landmark-unique.
 * The name travels as `aria-label` on the table node; ResponsiveTable moves it onto the
 * region wrapper. Runs after the leading h1 has been removed.
 */
export const remarkTableLabels: Plugin<[], Root> = () => (tree) => {
  let heading = '';
  const labels: { node: Table; label: string }[] = [];
  for (const node of tree.children) {
    if (node.type === 'heading') {
      heading = normaliseDashes(toString(node).trim());
    } else if (node.type === 'table') {
      labels.push({ node, label: baseLabel(node, heading) });
    }
  }
  const seen = new Map<string, number>();
  for (const { label } of labels) seen.set(label, (seen.get(label) ?? 0) + 1);
  const used = new Map<string, number>();
  for (const { node, label } of labels) {
    const total = seen.get(label) ?? 1;
    const index = (used.get(label) ?? 0) + 1;
    used.set(label, index);
    const unique = total > 1 && index > 1 ? `${label} (${index})` : label;
    const data = (node.data ??= {});
    const hProperties = ((data as { hProperties?: Record<string, unknown> }).hProperties ??= {});
    hProperties.ariaLabel = unique;
  }
};

function baseLabel(table: Table, heading: string): string {
  if (heading) return `Table: ${heading}`;
  const headerRow = table.children[0];
  const cells = headerRow ? headerRow.children.map((cell: TableCell) => normaliseDashes(toString(cell).trim())).filter(Boolean) : [];
  if (cells.length === 0) return 'Table';
  const shown = cells.slice(0, MAX_HEADERS);
  return `Table: ${shown.join(', ')}${cells.length > shown.length ? ', and more' : ''}`;
}
