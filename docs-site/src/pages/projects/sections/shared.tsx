import type { ReactNode } from 'react';
import styles from './ProjectSections.module.css';

export { Sources } from '../../../components/Sources';
export { Section, type SectionProps } from '../../../components/Section';

const INLINE_TOKEN = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g;

/**
 * Renders verbatim README text that carries inline Markdown (code spans, bold,
 * emphasis) as React elements. No raw HTML is ever produced.
 */
export function InlineMarkdown({ text }: { text: string }) {
  const parts = text.split(INLINE_TOKEN);
  return (
    <>
      {parts.map((part, index) => {
        if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
          return <code key={index}>{part.slice(1, -1)}</code>;
        }
        if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
          return <strong key={index}>{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith('*') && part.endsWith('*') && part.length > 2) {
          return <em key={index}>{part.slice(1, -1)}</em>;
        }
        return part;
      })}
    </>
  );
}

/** Caption element for the data tables on the project pages. */
export function TableCaption({ children }: { children: ReactNode }) {
  return <caption className={styles.caption}>{children}</caption>;
}
