import { valueToEstree } from 'estree-util-value-to-estree';
import GithubSlugger from 'github-slugger';
import type { Heading, Root } from 'mdast';
import type { MdxjsEsm } from 'mdast-util-mdxjs-esm';
import { toString } from 'mdast-util-to-string';
import type { Plugin } from 'unified';
import { visit } from 'unist-util-visit';
import { normaliseDashes } from './normaliseDashes';

export interface TocEntry {
  depth: number;
  id: string;
  text: string;
}

/**
 * Assigns GitHub-style ids to every heading and injects
 * `export const toc = [{ depth, id, text }, ...]` (h2 and h3 only) into the
 * compiled module. Ids are assigned here, before rehype-slug runs, so the TOC
 * and the rendered headings always agree (rehype-slug keeps existing ids). The
 * TOC text has typographic dashes normalised; ids are slugged from the raw text.
 */
export const remarkToc: Plugin<[], Root> = () => (tree) => {
  const slugger = new GithubSlugger();
  const toc: TocEntry[] = [];

  visit(tree, 'heading', (node: Heading) => {
    const text = toString(node).trim();
    const id = slugger.slug(text);
    const data = (node.data ??= {});
    const hProperties = ((data as { hProperties?: Record<string, unknown> }).hProperties ??= {});
    hProperties.id = id;
    if (node.depth === 2 || node.depth === 3) {
      toc.push({ depth: node.depth, id, text: normaliseDashes(text) });
    }
  });

  const esm: MdxjsEsm = {
    type: 'mdxjsEsm',
    value: `export const toc = ${JSON.stringify(toc)};`,
    data: {
      estree: {
        type: 'Program',
        sourceType: 'module',
        body: [
          {
            type: 'ExportNamedDeclaration',
            specifiers: [],
            source: null,
            attributes: [],
            declaration: {
              type: 'VariableDeclaration',
              kind: 'const',
              declarations: [
                {
                  type: 'VariableDeclarator',
                  id: { type: 'Identifier', name: 'toc' },
                  init: valueToEstree(toc),
                },
              ],
            },
          },
        ],
      },
    },
  };

  tree.children.push(esm);
};
