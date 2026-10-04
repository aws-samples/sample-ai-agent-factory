import { statSync } from 'node:fs';
import path from 'node:path';
import type { Definition, Link, Root } from 'mdast';
import type { Plugin } from 'unified';
import { visit } from 'unist-util-visit';

export interface RewriteLinksOptions {
  /** Absolute path of the repository root. */
  repoRoot: string;
  /** Site route for a repo-relative Markdown path, or undefined when not rendered on the site. */
  routeFor: (repoRelativePath: string) => string | undefined;
  /** GitHub blob URL for a repo-relative file path. */
  blobUrl: (repoRelativePath: string) => string;
  /** GitHub tree URL for a repo-relative folder path. */
  treeUrl: (repoRelativePath: string) => string;
}

/**
 * Rewrites relative links in repository Markdown:
 * - links to Markdown files that are rendered on the site become site routes
 *   (anchors preserved);
 * - every other relative link becomes an absolute GitHub link (blob for files,
 *   tree for folders);
 * - plain-http links to aws.amazon.com and github.com (and their subdomains)
 *   are upgraded to https;
 * - other absolute URLs, mailto: links and same-page anchors are untouched.
 *
 * The source file is taken from `file.path`, which @mdx-js/rollup sets to the
 * absolute path of the Markdown file being compiled.
 */
export const remarkRewriteLinks: Plugin<[RewriteLinksOptions], Root> = (options) => (tree, file) => {
  const sourcePath = file.path;
  if (!sourcePath) {
    throw new Error('remarkRewriteLinks needs file.path to resolve relative links');
  }
  const sourceDir = path.dirname(sourcePath);
  const repoRoot = path.resolve(options.repoRoot);

  const rewrite = (url: string): string => {
    if (!url) return url;
    if (/^http:\/\//i.test(url)) return upgradeToHttps(url);
    if (/^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(url)) return url;

    const hashIndex = url.indexOf('#');
    const hash = hashIndex === -1 ? '' : url.slice(hashIndex);
    const withoutHash = hashIndex === -1 ? url : url.slice(0, hashIndex);
    const target = withoutHash.split('?')[0];
    if (!target) return url;

    const absolute = target.startsWith('/')
      ? path.join(repoRoot, target)
      : path.resolve(sourceDir, decodeURIComponent(target));
    const relative = path.relative(repoRoot, absolute).split(path.sep).join('/');
    if (relative.startsWith('..')) return url;

    const route = options.routeFor(relative);
    if (route) return `${route}${hash}`;

    return `${isDirectory(absolute) ? options.treeUrl(relative) : options.blobUrl(relative)}${hash}`;
  };

  visit(tree, ['link', 'definition'], (node) => {
    const target = node as Link | Definition;
    target.url = rewrite(target.url);
  });
};

function isDirectory(absolutePath: string): boolean {
  try {
    return statSync(absolutePath).isDirectory();
  } catch {
    return false;
  }
}

const HTTPS_HOSTS = /^(?:[a-z0-9-]+\.)*(?:aws\.amazon\.com|github\.com)$/i;

/** `http://docs.aws.amazon.com/...` -> `https://...`; other hosts are returned unchanged. */
function upgradeToHttps(url: string): string {
  try {
    const parsed = new URL(url);
    return HTTPS_HOSTS.test(parsed.hostname) ? url.replace(/^http:/i, 'https:') : url;
  } catch {
    return url;
  }
}
