import { readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import type { Plugin } from 'vite';
import type { DocEntry } from '../content/docs';
import { normaliseDashes } from '../mdx/normaliseDashes';

const VIRTUAL_ID = 'virtual:repo-index';
const RESOLVED_ID = '\0' + VIRTUAL_ID;

const NOTEBOOK_ROOT = 'workshop-building-agentic-ai-platform/source';
const POLICY_ROOT = 'enterprise-mcp-governance-gateway/policies';
const REPO_URL = 'https://github.com/aws-samples/sample-ai-agent-factory';

export interface RepoIndexOptions {
  repoRoot: string;
  docs: readonly DocEntry[];
}

/**
 * Builds `virtual:repo-index` from the repository at build time so that
 * notebook lists, Cedar policies and document titles are never hand-typed.
 */
export function repoIndexPlugin(options: RepoIndexOptions): Plugin {
  const repoRoot = path.resolve(options.repoRoot);

  return {
    name: 'aaf-repo-index',
    resolveId(id) {
      return id === VIRTUAL_ID ? RESOLVED_ID : undefined;
    },
    load(id) {
      if (id !== RESOLVED_ID) return undefined;

      const notebooks = listNotebooks(repoRoot);
      const cedarPolicies = listCedarPolicies(repoRoot);
      const docTitles = readDocTitles(repoRoot, options.docs);

      for (const entry of [...notebooks, ...cedarPolicies].map((e) => e.path)) {
        this.addWatchFile(path.join(repoRoot, entry));
      }
      for (const doc of options.docs) {
        this.addWatchFile(path.join(repoRoot, doc.sourcePath));
      }

      return [
        `export const notebooks = ${JSON.stringify(notebooks)};`,
        `export const cedarPolicies = ${JSON.stringify(cedarPolicies)};`,
        `export const docTitles = ${JSON.stringify(docTitles)};`,
      ].join('\n');
    },
  };
}

function blobUrl(repoRelative: string): string {
  return `${REPO_URL}/blob/main/${repoRelative}`;
}

function walk(dir: string, predicate: (file: string) => boolean): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) {
      out.push(...walk(full, predicate));
    } else if (predicate(full)) {
      out.push(full);
    }
  }
  return out.sort();
}

function listNotebooks(repoRoot: string) {
  const root = path.join(repoRoot, NOTEBOOK_ROOT);
  return walk(root, (f) => f.endsWith('.ipynb') && !f.includes('.ipynb_checkpoints')).map((full) => {
    const repoRelative = path.relative(repoRoot, full).split(path.sep).join('/');
    const module = path.relative(root, full).split(path.sep)[0];
    return {
      path: repoRelative,
      module,
      title: notebookTitle(full),
      githubUrl: blobUrl(repoRelative),
    };
  });
}

interface NotebookCell {
  cell_type: string;
  source: string | string[];
}

function notebookTitle(file: string): string {
  try {
    const nb = JSON.parse(readFileSync(file, 'utf8')) as { cells?: NotebookCell[] };
    for (const cell of nb.cells ?? []) {
      if (cell.cell_type !== 'markdown') continue;
      const source = Array.isArray(cell.source) ? cell.source.join('') : cell.source;
      const match = /^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$/m.exec(source);
      if (match) return siteText(match[1]);
    }
  } catch {
    // fall through to the file-name fallback
  }
  return path
    .basename(file, '.ipynb')
    .replace(/[-_]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function listCedarPolicies(repoRoot: string) {
  const root = path.join(repoRoot, POLICY_ROOT);
  return readdirSync(root)
    .filter((name) => name.endsWith('.cedar'))
    .sort()
    .map((name) => {
      const repoRelative = `${POLICY_ROOT}/${name}`;
      return {
        file: name,
        name: name.replace(/\.cedar$/, ''),
        path: repoRelative,
        text: readFileSync(path.join(root, name), 'utf8'),
        githubUrl: blobUrl(repoRelative),
      };
    });
}

function readDocTitles(repoRoot: string, docs: readonly DocEntry[]): Record<string, string> {
  const titles: Record<string, string> = {};
  for (const doc of docs) {
    const content = readFileSync(path.join(repoRoot, doc.sourcePath), 'utf8');
    const match = /^#[ \t]+(.+?)[ \t]*#*[ \t]*$/m.exec(content);
    if (!match) {
      throw new Error(`No h1 heading found in ${doc.sourcePath}`);
    }
    titles[doc.sourcePath] = siteText(match[1]);
  }
  return titles;
}

/**
 * Text that ends up in site copy (h1s, link lists, page titles). Inline Markdown is
 * stripped and typographic dashes are normalised mechanically: a spaced em or en dash
 * becomes a colon, any remaining em or en dash becomes a hyphen, so page-owned copy
 * never carries a dash the content checks forbid.
 */
function siteText(text: string): string {
  return normaliseDashes(stripInlineMarkdown(text));
}

/** Removes inline code, emphasis and link syntax from heading text. */
export function stripInlineMarkdown(text: string): string {
  return text
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/(\*\*|__)(.*?)\1/g, '$2')
    .replace(/(\*|_)(.*?)\1/g, '$2')
    .replace(/<[^>]+>/g, '')
    .trim();
}
