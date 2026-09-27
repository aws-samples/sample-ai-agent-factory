import type { Heading, Image, Link, Paragraph, PhrasingContent, Root, RootContent } from 'mdast';
import type { Plugin } from 'unified';
import { visit } from 'unist-util-visit';

/**
 * Prepares repository Markdown for rendering inside a site page that already
 * has its own h1:
 *
 * 1. drops the first h1 (the page renders the title itself);
 * 2. drops paragraphs made only of externally hosted images or links wrapping
 *    them (CI and shields badges), which also keeps the "no externally hosted
 *    assets" contract;
 * 3. turns any other externally hosted image into a plain link with the alt
 *    text, so no remote image is ever fetched;
 * 4. demotes remaining h1 headings to h2 and closes heading-level gaps so the
 *    page never skips a level (WCAG 2.2 heading structure).
 */
export const remarkStripLeadingH1AndBadges: Plugin<[], Root> = () => (tree) => {
  let removedFirstH1 = false;

  tree.children = tree.children.filter((node) => {
    if (!removedFirstH1 && node.type === 'heading' && node.depth === 1) {
      removedFirstH1 = true;
      return false;
    }
    if (node.type === 'paragraph' && isBadgeParagraph(node)) {
      return false;
    }
    return true;
  });

  visit(tree, 'image', (node: Image, index, parent) => {
    if (!isExternal(node.url) || !parent || index === undefined) return;
    const link: Link = {
      type: 'link',
      url: node.url,
      title: node.title ?? null,
      children: [{ type: 'text', value: node.alt || node.url }],
    };
    (parent.children as RootContent[])[index] = link;
  });

  normaliseHeadingLevels(tree);
};

function isExternal(url: string): boolean {
  return /^(https?:)?\/\//i.test(url);
}

function isBadgeParagraph(node: Paragraph): boolean {
  let images = 0;
  for (const child of node.children) {
    if (child.type === 'text' && child.value.trim() === '') continue;
    if (child.type === 'break' || child.type === 'html') continue;
    if (child.type === 'image' && isExternal(child.url)) {
      images += 1;
      continue;
    }
    if (child.type === 'link' && child.children.length > 0 && child.children.every(isExternalImage)) {
      images += 1;
      continue;
    }
    return false;
  }
  return images > 0;
}

function isExternalImage(node: PhrasingContent): boolean {
  return node.type === 'image' && isExternal(node.url);
}

/**
 * Re-levels headings so the sequence never jumps by more than one level and
 * never uses h1 (reserved for the page title). Sibling headings keep the same
 * level relative to each other.
 */
function normaliseHeadingLevels(tree: Root): void {
  const stack: { original: number; mapped: number }[] = [];
  visit(tree, 'heading', (node: Heading) => {
    const original = node.depth === 1 ? 2 : node.depth;
    while (stack.length > 0 && stack[stack.length - 1].original >= original) {
      stack.pop();
    }
    const parentLevel = stack.length > 0 ? stack[stack.length - 1].mapped : 1;
    const mapped = Math.min(6, parentLevel + 1);
    stack.push({ original, mapped });
    node.depth = mapped as Heading['depth'];
  });
}
