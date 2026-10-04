import { Children, isValidElement, useRef, type ReactElement, type ReactNode, type TableHTMLAttributes } from 'react';
import { useIsScrollable } from './useIsScrollable';
import styles from './ResponsiveTable.module.css';

const MAX_LABEL_HEADERS = 6;

/**
 * Table wrapped in a horizontally scrollable region. The region takes the `aria-label`
 * given to the table (Markdown tables get a unique one at build time from the nearest
 * heading, see remarkTableLabels); otherwise it is named from the caption, else from the
 * header row ("Table: A, B, C"), else "Table". Keyboard-focusable only while it scrolls.
 */
export function ResponsiveTable({
  children,
  className,
  'aria-label': ariaLabel,
  ...rest
}: TableHTMLAttributes<HTMLTableElement>) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const scrollable = useIsScrollable(wrapperRef);
  return (
    <div
      ref={wrapperRef}
      className={styles.wrapper}
      role="region"
      aria-label={ariaLabel || tableLabel(children)}
      tabIndex={scrollable ? 0 : undefined}
    >
      <table className={className ? `${styles.table} ${className}` : styles.table} {...rest}>
        {children}
      </table>
    </div>
  );
}

function tableLabel(children: ReactNode): string {
  const caption = findElement(children, (element) => element.type === 'caption');
  const captionText = caption ? textOf(caption.props.children).trim() : '';
  if (captionText) return captionText;

  const thead = findElement(children, (element) => element.type === 'thead');
  const row = thead ? findElement(thead.props.children, (element) => element.type === 'tr') : undefined;
  if (row) {
    const headers = Children.toArray(row.props.children)
      .filter(isValidElement)
      .map((cell) => textOf((cell as ReactElement<{ children?: ReactNode }>).props.children).trim())
      .filter(Boolean);
    if (headers.length > 0) {
      const shown = headers.slice(0, MAX_LABEL_HEADERS);
      return `Table: ${shown.join(', ')}${headers.length > shown.length ? ', and more' : ''}`;
    }
  }
  return 'Table';
}

type AnyElement = ReactElement<{ children?: ReactNode }>;

function findElement(children: ReactNode, matches: (element: AnyElement) => boolean): AnyElement | undefined {
  for (const child of Children.toArray(children)) {
    if (isValidElement(child) && matches(child as AnyElement)) return child as AnyElement;
  }
  return undefined;
}

function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (isValidElement(node)) return textOf((node as AnyElement).props.children);
  return '';
}
