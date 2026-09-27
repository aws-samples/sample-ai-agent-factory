import { isValidElement, useEffect, useRef, useState, type HTMLAttributes, type ReactNode } from 'react';
import styles from './CodeBlock.module.css';

export interface CodeBlockProps extends Omit<HTMLAttributes<HTMLPreElement>, 'children'> {
  /** MDX passes the <code> element here; page code may pass `code` instead. */
  children?: ReactNode;
  /** Raw code when used directly from a page (quickstart steps). */
  code?: string;
  /** Language label, e.g. "bash". Derived from the `language-*` class when omitted. */
  language?: string;
}

function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return '';
}

/**
 * Code block with a language label, a Copy button (navigator.clipboard, no
 * network) and a keyboard-scrollable <pre tabIndex={0}>.
 */
export function CodeBlock({ children, code, language, className, ...rest }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const codeElement = isValidElement<{ className?: string; children?: ReactNode }>(children) ? children : null;
  const lang = language ?? /language-([\w+-]+)/.exec(codeElement?.props.className ?? '')?.[1] ?? 'text';
  const text = code ?? textOf(codeElement?.props.children ?? children);

  const copy = async () => {
    if (typeof navigator === 'undefined' || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(text.replace(/\n$/, ''));
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className={styles.block}>
      <div className={styles.toolbar}>
        <span className={styles.language} aria-hidden="true">
          {lang}
        </span>
        <button type="button" className={styles.copy} onClick={copy}>
          {copied ? 'Copied' : 'Copy'}
          <span className="visually-hidden"> {lang} code to clipboard</span>
        </button>
        <span role="status" aria-live="polite" className="visually-hidden">
          {copied ? 'Code copied to clipboard' : ''}
        </span>
      </div>
      <pre tabIndex={0} className={className ? `${styles.pre} ${className}` : styles.pre} {...rest}>
        {code !== undefined ? <code className={`language-${lang}`}>{code}</code> : children}
      </pre>
    </div>
  );
}
