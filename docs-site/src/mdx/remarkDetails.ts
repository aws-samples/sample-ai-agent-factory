import type { Html, Paragraph, Root, RootContent } from 'mdast';
import type { Plugin } from 'unified';

const SUMMARY = /<summary(?:\s[^>]*)?>([\s\S]*?)<\/summary>/i;
const DETAILS_TAGS = /<\/?details(?:\s[^>]*)?>/gi;

/**
 * Raw HTML in Markdown is dropped by the MDX compiler (the site does not parse it),
 * which would silently lose `<details><summary>Title</summary>` disclosure blocks used in
 * the READMEs.
 * This plugin keeps their content: the summary becomes a paragraph with a strong
 * label, the `<details>` wrappers are removed, and the body between them is
 * rendered like any other Markdown. Only these two tags are touched.
 */
export const remarkDetails: Plugin<[], Root> = () => (tree) => {
  const next: RootContent[] = [];
  for (const node of tree.children) {
    if (node.type !== 'html') {
      next.push(node);
      continue;
    }
    next.push(...convert(node));
  }
  tree.children = next;
};

function convert(node: Html): RootContent[] {
  const out: RootContent[] = [];
  const summary = node.value.match(SUMMARY);
  if (summary) {
    const text = summary[1].replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
    if (text) {
      const paragraph: Paragraph = {
        type: 'paragraph',
        children: [{ type: 'strong', children: [{ type: 'text', value: text }] }],
      };
      out.push(paragraph);
    }
  }
  const rest = node.value.replace(SUMMARY, '').replace(DETAILS_TAGS, '').trim();
  if (rest) {
    out.push({ ...node, value: rest });
  }
  return out;
}
