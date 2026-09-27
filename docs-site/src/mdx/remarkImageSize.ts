import { readFileSync } from 'node:fs';
import path from 'node:path';
import { imageSize } from 'image-size';
import type { Image, Root } from 'mdast';
import type { Plugin } from 'unified';
import { visit } from 'unist-util-visit';

/**
 * Gives every local Markdown image its intrinsic width and height at build time, so
 * the browser reserves the space before the file loads and fragment links into long
 * pages land on the right heading. Runs before rehype-mdx-import-media rewrites
 * `src` into an import; external and unreadable images are left alone.
 */
export const remarkImageSize: Plugin<[], Root> = () => (tree, file) => {
  const sourceDir = file.path ? path.dirname(file.path) : undefined;
  visit(tree, 'image', (node: Image) => {
    if (!sourceDir || !node.url || /^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(node.url)) return;
    const target = node.url.split('#')[0].split('?')[0];
    if (!target) return;
    const absolute = path.resolve(sourceDir, decodeURIComponent(target));
    let size: { width?: number; height?: number };
    try {
      size = imageSize(readFileSync(absolute));
    } catch {
      return;
    }
    if (!size.width || !size.height) return;
    const data = (node.data ??= {});
    const hProperties = ((data as { hProperties?: Record<string, unknown> }).hProperties ??= {});
    hProperties.width = size.width;
    hProperties.height = size.height;
  });
};
