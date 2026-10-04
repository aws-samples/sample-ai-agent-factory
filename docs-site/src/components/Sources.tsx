import { Fragment } from 'react';
import { githubUrl, type Source } from '../content/facts';
import { SourceLink } from './SourceLink';
import styles from './Sources.module.css';

export interface SourcesProps {
  sources: Source[];
  label?: string;
  /** Render as a span inside running text instead of a paragraph. */
  inline?: boolean;
  className?: string;
}

/**
 * "Source: file: heading, file" line under a table, list or paragraph. Several
 * quotes from the same file and heading collapse into one link. The one
 * treatment for source lines across the site.
 */
export function Sources({ sources, label = 'Source', inline = false, className }: SourcesProps) {
  const unique = [...new Map(sources.map((source) => [githubUrl(source), source])).values()];
  const fileNames = unique.map((source) => source.file?.split('/').pop());
  const Tag = inline ? 'span' : 'p';
  return (
    <Tag className={[styles.sources, className].filter(Boolean).join(' ')} data-sources>
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

/** Small "source" link with hidden context, for a step or a single value. */
export function SmallSource({ source, context, className }: { source: Source; context: string; className?: string }) {
  return (
    <SourceLink source={source} className={[styles.small, className].filter(Boolean).join(' ')}>
      source<span className="visually-hidden"> for {context}</span>
    </SourceLink>
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
