import { Fragment, type ReactNode } from 'react';
import { SourceLink } from '../../../components/SourceLink';
import { githubUrl, type Source } from '../../../content/facts';
import styles from './ProjectSections.module.css';

/**
 * "Source: file: heading, file" line under a table, list or paragraph. Several
 * quotes from the same file and heading collapse into one link.
 */
export function Sources({
  sources,
  label = 'Source',
  inline = false,
}: {
  sources: Source[];
  label?: string;
  /** Render as a span inside running text instead of a paragraph. */
  inline?: boolean;
}) {
  const unique = [...new Map(sources.map((source) => [githubUrl(source), source])).values()];
  const fileNames = unique.map((source) => source.file?.split('/').pop());
  const Tag = inline ? 'span' : 'p';
  return (
    <Tag className={styles.sources}>
      {label}:{' '}
      {unique.map((source, index) => {
        // Two files with the same name (module-3b/index.en.md, step-7/index.en.md) get their folder.
        const repeated =
          fileNames[index] !== undefined && fileNames.filter((name) => name === fileNames[index]).length > 1;
        return (
          <Fragment key={githubUrl(source)}>
            {index > 0 && ', '}
            <SourceLink source={source}>{repeated ? sourceLabel(source) : undefined}</SourceLink>
          </Fragment>
        );
      })}
    </Tag>
  );
}

/** "folder/file.md: Heading" for a source whose file name alone would be ambiguous. */
function sourceLabel(source: Source): string | undefined {
  if (!source.file) return undefined;
  const parts = source.file.split('/');
  const file = parts.pop() ?? source.file;
  const folder = parts.pop();
  const base = folder ? `${folder}/${file}` : file;
  return source.heading ? `${base}: ${source.heading}` : base;
}

export interface SectionProps {
  /** Fragment id, also used to label the region. */
  id: string;
  title: string;
  children: ReactNode;
}

/** A page section with an h2 heading. */
export function Section({ id, title, children }: SectionProps) {
  const headingId = `${id}-heading`;
  return (
    <section id={id} className={styles.section} aria-labelledby={headingId}>
      <h2 id={headingId}>{title}</h2>
      {children}
    </section>
  );
}

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
