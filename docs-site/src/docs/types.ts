import type { ComponentType } from 'react';

export interface TocEntry {
  depth: number;
  id: string;
  text: string;
}

export interface MDXContentProps {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  components?: Record<string, ComponentType<any>>;
}

/** Shape of a compiled Markdown module (see src/types/mdx.d.ts). */
export interface DocModule {
  default: ComponentType<MDXContentProps>;
  toc: TocEntry[];
}
