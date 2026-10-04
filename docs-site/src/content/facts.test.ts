/**
 * Provenance tests for the content model.
 *
 * For every source pointer exported from the content modules: the cited file
 * exists in the repository, every `quote` is a verbatim substring of that file
 * (20 to 120 characters), and every `heading` is a real Markdown heading.
 * Also asserts that no content file contains an em-dash or any of the banned
 * phrases.
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import * as data from './data';
import * as facts from './facts';
import * as faq from './faq';
import * as glossary from './glossary';
import * as limitations from './limitations';
import * as links from './links';
import * as matrix from './matrix';
import * as quickstarts from './quickstarts';
import * as tracks from './tracks';

const CONTENT_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(CONTENT_DIR, '..', '..', '..');

const MODULES: Record<string, unknown> = {
  data,
  facts,
  faq,
  glossary,
  limitations,
  matrix,
  quickstarts,
  tracks,
};

interface FilePointer {
  file: string;
  heading?: string;
  quote?: string;
}

interface Located extends FilePointer {
  path: string;
}

/** Recursively collect every object that carries a string `file` property. */
function collectPointers(value: unknown, path: string, out: Located[], seen = new Set<unknown>()): void {
  if (value === null || typeof value !== 'object') return;
  if (seen.has(value)) return;
  seen.add(value);

  if (Array.isArray(value)) {
    value.forEach((item, index) => collectPointers(item, `${path}[${index}]`, out, seen));
    return;
  }

  const record = value as Record<string, unknown>;
  if (typeof record.file === 'string') {
    out.push({
      path,
      file: record.file,
      heading: typeof record.heading === 'string' ? record.heading : undefined,
      quote: typeof record.quote === 'string' ? record.quote : undefined,
    });
  }
  for (const [key, child] of Object.entries(record)) {
    collectPointers(child, `${path}.${key}`, out, seen);
  }
}

const pointers: Located[] = [];
for (const [name, mod] of Object.entries(MODULES)) {
  collectPointers(mod, name, pointers);
}

interface UrlPointer {
  path: string;
  url: string;
  label?: string;
  file?: unknown;
  quote?: unknown;
  heading?: unknown;
}

/** Recursively collect every object that carries a string `url` property (external evidence). */
function collectUrlPointers(value: unknown, path: string, out: UrlPointer[], seen = new Set<unknown>()): void {
  if (value === null || typeof value !== 'object') return;
  if (seen.has(value)) return;
  seen.add(value);
  if (Array.isArray(value)) {
    value.forEach((item, index) => collectUrlPointers(item, `${path}[${index}]`, out, seen));
    return;
  }
  const record = value as Record<string, unknown>;
  if (typeof record.url === 'string' && typeof record.label === 'string') {
    out.push({
      path,
      url: record.url,
      label: record.label,
      file: record.file,
      quote: record.quote,
      heading: record.heading,
    });
  }
  for (const [key, child] of Object.entries(record)) {
    collectUrlPointers(child, `${path}.${key}`, out, seen);
  }
}

const urlPointers: UrlPointer[] = [];
for (const [name, mod] of Object.entries(MODULES)) {
  collectUrlPointers(mod, name, urlPointers);
}

const fileCache = new Map<string, string>();
function readRepoFile(file: string): string {
  const cached = fileCache.get(file);
  if (cached !== undefined) return cached;
  const text = readFileSync(join(REPO_ROOT, file), 'utf8');
  fileCache.set(file, text);
  return text;
}

function markdownHeadings(text: string): string[] {
  const headings: string[] = [];
  let inFence = false;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trimEnd();
    if (/^\s*(```|~~~)/.test(line)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    const match = /^#{1,6}\s+(.+?)\s*#*\s*$/.exec(line);
    if (match) headings.push(match[1].trim());
  }
  return headings;
}

describe('content sources', () => {
  it('collects at least one source from every module', () => {
    for (const name of Object.keys(MODULES)) {
      expect(pointers.some(p => p.path.startsWith(`${name}.`)), `${name} has sources`).toBe(true);
    }
  });

  const uniqueFiles = [...new Set(pointers.map(p => p.file))].sort();
  it.each(uniqueFiles)('cited file exists: %s', file => {
    expect(existsSync(join(REPO_ROOT, file))).toBe(true);
  });

  const quoted = pointers.filter(p => p.quote !== undefined);
  it.each(quoted.map(p => [p.path, p.file, p.quote as string] as const))(
    '%s quotes %s verbatim',
    (_path, file, quote) => {
      expect(quote.length, `quote too short: ${quote}`).toBeGreaterThanOrEqual(20);
      expect(quote.length, `quote too long: ${quote}`).toBeLessThanOrEqual(120);
      const text = readRepoFile(file);
      expect(text.includes(quote), `quote not found in ${file}: ${quote}`).toBe(true);
    },
  );

  const withHeading = pointers.filter(p => p.heading !== undefined);
  it.each(withHeading.map(p => [p.path, p.file, p.heading as string] as const))(
    '%s cites a real heading in %s',
    (_path, file, heading) => {
      const headings = markdownHeadings(readRepoFile(file));
      expect(headings, `heading "${heading}" not found in ${file}`).toContain(heading);
    },
  );

  it('url sources are absolute, labelled, and carry no file, quote or heading', () => {
    expect(urlPointers.length).toBeGreaterThan(0);
    for (const pointer of urlPointers) {
      expect(pointer.url, pointer.path).toMatch(/^https:\/\//);
      expect(pointer.label, pointer.path).toBeTruthy();
      expect(pointer.file, `${pointer.path} mixes file and url`).toBeUndefined();
      expect(pointer.quote, `${pointer.path} quotes a url source`).toBeUndefined();
      expect(pointer.heading, `${pointer.path} gives a heading for a url source`).toBeUndefined();
      expect(facts.githubUrl({ url: pointer.url, label: pointer.label })).toBe(pointer.url);
    }
    expect(facts.facts.workshop.status.source?.url).toBe(links.WORKSHOP_URL);
  });

  it('every project has facts, limitations, quickstarts and a matrix column', () => {
    for (const project of data.projects) {
      expect(facts.facts[project.id]).toBeDefined();
      expect(limitations.limitations[project.id].length).toBeGreaterThan(0);
      expect(limitations.validated[project.id]).toBeDefined();
      expect(quickstarts.getQuickstarts(project.id).length).toBeGreaterThan(0);
      for (const row of matrix.capabilityMatrix) expect(row.cells[project.id]).toBeDefined();
      for (const row of matrix.securityMatrix) expect(row.cells[project.id]).toBeDefined();
    }
  });

  it('facts with numbers carry a source or are marked not documented', () => {
    for (const [projectId, projectFacts] of Object.entries(facts.facts)) {
      for (const key of facts.FACT_KEYS) {
        const fact = projectFacts[key];
        const hasNumber = /\d/.test(fact.value);
        if (fact.notDocumented) {
          expect(fact.value, `${projectId}.${key}`).toBe(facts.NOT_DOCUMENTED_LABEL);
          continue;
        }
        if (hasNumber && !fact.source) {
          expect(fact.note, `${projectId}.${key} has a number but no source or note`).toBeTruthy();
        }
      }
    }
  });

  it('the blueprint validated envelope lists the twelve README bullets', () => {
    expect(limitations.validated.blueprint).toHaveLength(12);
    for (const item of limitations.validated.blueprint) {
      expect(item.source.heading).toBe('Live-validated reference envelope');
    }
  });

  it('every site path cites a README source for what you get', () => {
    for (const path of tracks.sitePaths) {
      expect(path.source.file, path.id).toBeTruthy();
      expect(path.source.heading, path.id).toBeTruthy();
    }
  });

  it('capability matrix rows match the capabilities in data.ts', () => {
    const ids = data.capabilities.map(c => c.id).sort();
    const rows = matrix.capabilityMatrix.map(r => r.capabilityId).sort();
    expect(rows).toEqual(ids);
  });

  it('navigation paths use trailing slashes', () => {
    const walk = (items: data.NavItem[]): void => {
      for (const item of items) {
        expect(item.path.endsWith('/'), item.path).toBe(true);
        if (item.children) walk(item.children);
      }
    };
    walk(data.navigation);
  });

  it('github heading slugs follow GitHub rules', () => {
    expect(links.githubHeadingSlug('15. Known limitations and support envelope')).toBe(
      '15-known-limitations-and-support-envelope',
    );
    expect(links.githubHeadingSlug('6.1 One-time setup')).toBe('61-one-time-setup');
    expect(links.githubHeadingSlug('Tracked production hardening (not in this sample)')).toBe(
      'tracked-production-hardening-not-in-this-sample',
    );
    expect(links.githubHeadingSlug("What you'll build")).toBe('what-youll-build');
    expect(links.githubSourceUrl({ file: 'README.md', heading: 'Quick Start' })).toBe(
      `${links.REPO_URL}/blob/main/README.md#quick-start`,
    );
  });
});

describe('content copy rules', () => {
  const contentFiles = readdirSync(CONTENT_DIR)
    .filter(name => name.endsWith('.ts') && !name.endsWith('.test.ts'))
    .sort();

  // Patterns are assembled from pieces so this test file itself never contains
  // the banned text and stays clean under a plain grep of src/content.
  const banned: Array<[string, RegExp]> = [
    ['em-dash character (U+2014)', /\u2014/],
    ['publication-status phrase', new RegExp(['not', 'yet', 'published'].join(' '), 'i')],
    ['copy-of-another-repo word (m...)', new RegExp('mir' + 'ror', 'i')],
    ['copy-of-another-repo word (u...)', new RegExp('up' + 'stream', 'i')],
    ['other repository slug', new RegExp('low' + '[- ]?code|no' + '[- ]?code', 'i')],
  ];

  it.each(contentFiles)('%s contains no banned text', file => {
    const text = readFileSync(join(CONTENT_DIR, file), 'utf8');
    for (const [label, pattern] of banned) {
      const match = pattern.exec(text);
      expect(match, `${file} contains ${label} near: ${match ? text.slice(Math.max(0, match.index - 40), match.index + 40) : ''}`).toBeNull();
    }
  });

  it('workshop track names appear only on workshop-owned content', () => {
    const trackNames = /Fast Path|Build the Platform|Full Journey/;
    for (const path of tracks.sitePaths) {
      expect(trackNames.test(path.name), path.name).toBe(false);
      expect(trackNames.test(path.who), path.who).toBe(false);
      expect(trackNames.test(path.whatYouGet), path.whatYouGet).toBe(false);
    }
    for (const role of tracks.roleGuidance) {
      expect(trackNames.test(role.why), role.why).toBe(false);
    }
    for (const time of tracks.timeGuidance) {
      expect(trackNames.test(time.available), time.available).toBe(false);
      expect(trackNames.test(time.basis.value), time.basis.value).toBe(false);
    }
  });
});
