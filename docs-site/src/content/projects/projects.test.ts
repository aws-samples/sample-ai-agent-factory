/**
 * Provenance tests for the per-project content modules in this folder.
 *
 * Same contract as ../facts.test.ts: every cited file exists, every `quote` is
 * a verbatim substring of its file (20 to 120 characters), every `heading` is a
 * real Markdown heading, and no file here contains an em-dash or a banned
 * phrase. Plus structural checks: the package groups cover packages/ exactly,
 * the golden-path folders exist, the policy purposes match the .cedar files and
 * the manifest, and every statement of the featured policy has one note.
 */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { projects } from '../data';
import * as blueprint from './blueprint-golden-paths';
import * as evidence from './evidence';
import * as gateway from './mcp-gateway-demo';
import * as selfService from './self-service-sections';
import * as whatItIs from './what-it-is';
import * as workshop from './workshop-modules';

const DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(DIR, '..', '..', '..', '..');

const MODULES: Record<string, unknown> = { blueprint, evidence, gateway, selfService, whatItIs, workshop };

interface Located {
  path: string;
  file: string;
  heading?: string;
  quote?: string;
}

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
  for (const [key, child] of Object.entries(record)) collectPointers(child, `${path}.${key}`, out, seen);
}

const pointers: Located[] = [];
for (const [name, mod] of Object.entries(MODULES)) collectPointers(mod, name, pointers);

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

describe('project content sources', () => {
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
  it.each(quoted.map(p => [p.path, p.file, p.quote as string] as const))('%s quotes %s verbatim', (_path, file, quote) => {
    expect(quote.length, `quote too short: ${quote}`).toBeGreaterThanOrEqual(20);
    expect(quote.length, `quote too long: ${quote}`).toBeLessThanOrEqual(120);
    expect(readRepoFile(file).includes(quote), `quote not found in ${file}: ${quote}`).toBe(true);
  });

  const withHeading = pointers.filter(p => p.heading !== undefined);
  it.each(withHeading.map(p => [p.path, p.file, p.heading as string] as const))(
    '%s cites a real heading in %s',
    (_path, file, heading) => {
      expect(markdownHeadings(readRepoFile(file)), `heading "${heading}" not found in ${file}`).toContain(heading);
    },
  );
});

describe('blueprint folders', () => {
  it('package groups cover every folder under packages/ exactly once', () => {
    const onDisk = readdirSync(join(REPO_ROOT, blueprint.BLUEPRINT_PACKAGES_DIR))
      .filter(name => statSync(join(REPO_ROOT, blueprint.BLUEPRINT_PACKAGES_DIR, name)).isDirectory())
      .sort();
    const listed = blueprint.packageGroups.flatMap(g => g.packages).sort();
    expect(listed).toEqual(onDisk);
    expect(blueprint.packageGroups.length).toBeGreaterThanOrEqual(4);
    expect(blueprint.packageGroups.length).toBeLessThanOrEqual(6);
  });

  it('golden paths match the folders under blueprints/', () => {
    const onDisk = readdirSync(join(REPO_ROOT, blueprint.BLUEPRINT_TEMPLATES_DIR))
      .filter(name => statSync(join(REPO_ROOT, blueprint.BLUEPRINT_TEMPLATES_DIR, name)).isDirectory())
      .sort();
    expect(blueprint.goldenPaths.map(g => g.name).sort()).toEqual(onDisk);
  });

  it('figure sources exist', () => {
    for (const figure of Object.values(blueprint.blueprintFigures)) {
      expect(existsSync(join(REPO_ROOT, figure.drawio)), figure.drawio).toBe(true);
      expect(existsSync(join(REPO_ROOT, figure.drawio.replace(/\.drawio$/, '.svg')))).toBe(true);
    }
  });
});

describe('gateway policies', () => {
  const policyDir = join(REPO_ROOT, gateway.GATEWAY_POLICIES_DIR);
  const cedarFiles = readdirSync(policyDir)
    .filter(name => name.endsWith('.cedar'))
    .map(name => name.replace(/\.cedar$/, ''))
    .sort();
  const manifest = JSON.parse(readFileSync(join(REPO_ROOT, gateway.GATEWAY_MANIFEST), 'utf8')) as {
    validationMode: string;
    policies: { name: string }[];
    disabledPolicies: { name: string }[];
  };

  it('has one purpose line per .cedar file', () => {
    expect(gateway.policyPurposes.map(p => p.name).sort()).toEqual(cedarFiles);
  });

  it('active flags match manifest.json', () => {
    const active = new Set(manifest.policies.map(p => p.name));
    const disabled = new Set(manifest.disabledPolicies.map(p => p.name));
    for (const purpose of gateway.policyPurposes) {
      expect(active.has(purpose.name) || disabled.has(purpose.name), `${purpose.name} is in the manifest`).toBe(true);
      expect(purpose.active, `${purpose.name} active flag`).toBe(active.has(purpose.name));
    }
    expect(manifest.validationMode).toBe('IGNORE_ALL_FINDINGS');
  });

  it('every statement of the featured policy has exactly one note, in file order', () => {
    const text = readFileSync(join(policyDir, gateway.FEATURED_POLICY_FILE), 'utf8');
    const statements = gateway.splitCedarStatements(text);
    expect(statements.length).toBe(gateway.featuredPolicyNotes.length);
    statements.forEach((statement, index) => {
      const note = gateway.featuredPolicyNotes[index];
      expect(note.effect, `statement ${index + 1} effect`).toBe(statement.effect);
      expect(note.action, `statement ${index + 1} action`).toBe(statement.action);
    });
    const keys = gateway.featuredPolicyNotes.map(n => `${n.effect}:${n.action}`);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('demo table has queries A to F', () => {
    expect(gateway.demoQueries.map(q => q.id)).toEqual(['A', 'B', 'C', 'D', 'E', 'F']);
    expect(gateway.requestFlow).toHaveLength(6);
    expect(gateway.claims.filter(c => c.reachesCedar).map(c => c.claim)).toEqual(['sub', 'username', 'scope']);
    expect(gateway.claims.filter(c => !c.reachesCedar).map(c => c.claim)).toEqual(['email', 'custom:role']);
  });
});

describe('workshop and self-service content', () => {
  it('lists the five workshop modules', () => {
    expect(workshop.workshopModules.map(m => m.name)).toEqual(['Module 1', 'Module 2', 'Module 3a', 'Module 3b', 'Module 4']);
  });

  it('describes three LiteLLM shapes and links the rendered README anchor', () => {
    expect(selfService.liteLlmShapes).toHaveLength(3);
    expect(selfService.BRING_YOUR_OWN_LITELLM_ANCHOR).toBe('bring-your-own-litellm');
    expect(existsSync(join(REPO_ROOT, selfService.selfServiceFigures.architecture.drawio))).toBe(true);
    expect(existsSync(join(REPO_ROOT, selfService.MCP_CATALOG_PATH))).toBe(true);
  });
});

describe('project content copy rules', () => {
  const files = readdirSync(DIR)
    .filter(name => name.endsWith('.ts') && !name.endsWith('.test.ts'))
    .sort();

  // Patterns are assembled from pieces so this file never contains the banned text.
  const banned: Array<[string, RegExp]> = [
    ['em-dash character (U+2014)', /\u2014/],
    ['publication-status phrase', new RegExp(['not', 'yet', 'published'].join(' '), 'i')],
    ['copy-of-another-repo word (m...)', new RegExp('mir' + 'ror', 'i')],
    ['copy-of-another-repo word (u...)', new RegExp('up' + 'stream', 'i')],
    ['other repository slug', new RegExp('low' + 'code', 'i')],
  ];

  it.each(files)('%s contains no banned text', file => {
    const text = readFileSync(join(DIR, file), 'utf8');
    for (const [label, pattern] of banned) {
      const match = pattern.exec(text);
      expect(
        match,
        `${file} contains ${label} near: ${match ? text.slice(Math.max(0, match.index - 40), match.index + 40) : ''}`,
      ).toBeNull();
    }
  });

  it('workshop track names do not appear outside the workshop module', () => {
    const trackNames = /Fast Path|Build the Platform|Full Journey/;
    for (const file of files.filter(f => !f.startsWith('workshop'))) {
      expect(trackNames.test(readFileSync(join(DIR, file), 'utf8')), file).toBe(false);
    }
  });
});

describe('evidence', () => {
  it('every project has an intro, a sourced live statement and at least two sourced items with unique ids', () => {
    for (const [id, block] of Object.entries(evidence.evidence)) {
      expect(block.intro.length, id).toBeGreaterThan(20);
      expect(block.live.text.length, id).toBeGreaterThan(20);
      expect(block.live.sources.length, id).toBeGreaterThan(0);
      expect(block.items.length, id).toBeGreaterThanOrEqual(2);
      const ids = block.items.map(item => item.id);
      expect(new Set(ids).size, id).toBe(ids.length);
      for (const item of block.items) {
        expect(item.sources.length, `${id} ${item.id}`).toBeGreaterThan(0);
        expect(item.proves.length, `${id} ${item.id}`).toBeGreaterThan(20);
        if (item.list) expect(item.list.length, `${id} ${item.id}`).toBeGreaterThan(1);
      }
    }
  });

  it('every command line is a verbatim line of one of the cited files', () => {
    for (const [id, block] of Object.entries(evidence.evidence)) {
      for (const item of block.items) {
        if (!item.command) continue;
        const texts = [...new Set(item.sources.flatMap(source => (source.file ? [source.file] : [])))].map(readRepoFile);
        for (const line of item.command.split('\n').filter(line => line.trim().length > 0)) {
          expect(texts.some(text => text.includes(line)), `${id} ${item.id}: ${line}`).toBe(true);
        }
      }
    }
  });

  it('list entries are verbatim lines of one of the cited files', () => {
    for (const [id, block] of Object.entries(evidence.evidence)) {
      for (const item of block.items) {
        if (!item.list) continue;
        const texts = [...new Set(item.sources.flatMap(source => (source.file ? [source.file] : [])))].map(readRepoFile);
        for (const entry of item.list) {
          expect(texts.some(text => text.includes(entry)), `${id} ${item.id}: ${entry}`).toBe(true);
        }
      }
    }
  });
});

describe('what it is', () => {
  it('kept plus dropped bullets are exactly the project feature bullets, each kept bullet with a source', () => {
    for (const project of projects) {
      const items = whatItIs.whatItIs[project.id];
      const kept = items.map(item => item.text);
      const dropped = whatItIs.droppedFeatures[project.id] ?? [];
      expect([...kept, ...dropped].sort(), project.id).toEqual([...project.features].sort());
      for (const item of items) expect(item.sources.length, `${project.id}: ${item.text}`).toBeGreaterThan(0);
    }
  });

  it('the blueprint bullets no longer all point at 10.1', () => {
    const headings = whatItIs.whatItIs.blueprint.flatMap(item => item.sources.map(source => source.heading ?? source.file));
    expect(new Set(headings).size).toBeGreaterThan(1);
  });
});
