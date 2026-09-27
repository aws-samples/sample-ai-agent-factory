declare module '*.md' {
  import type { ComponentType } from 'react';
  import type { MDXContentProps, TocEntry } from '../docs/types';

  /** Injected by src/mdx/remarkToc.ts: h2 and h3 headings in document order. */
  export const toc: TocEntry[];

  const MDXContent: ComponentType<MDXContentProps>;
  export default MDXContent;
}
