import type { ReactNode } from 'react';
import { githubUrl, type Source } from '../content/facts';
import { ExternalLink } from './ExternalLink';

export interface SourceLinkProps {
  source: Source;
  /** Link text; defaults to the file name plus heading. */
  children?: ReactNode;
  className?: string;
}

function defaultLabel(source: Source): string {
  if (!source.file) return source.label ?? source.url ?? 'source';
  const file = source.file.split('/').pop() ?? source.file;
  return source.heading ? `${file}: ${source.heading}` : file;
}

/**
 * Link to the exact repository file (and heading) a fact was taken from, or to the
 * external URL for sources that live outside the repository (label required there).
 */
export function SourceLink({ source, children, className }: SourceLinkProps) {
  return (
    <ExternalLink href={source.url ?? githubUrl(source)} className={className}>
      {children ?? defaultLabel(source)}
    </ExternalLink>
  );
}
