import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/**
 * Design lint: keeps the design system from drifting back into per-page styles.
 * Every CSS module must express colour through tokens (so the dark theme works),
 * use the pill radius token, and never redefine buttons. tokens.css is the only
 * place raw colours may appear.
 */
const SRC = join(dirname(fileURLToPath(import.meta.url)), '..');

function cssModules(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true, recursive: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith('.module.css'))
    .map((entry) => join(entry.parentPath ?? entry.path, entry.name));
}

/** Justified exceptions, each with the reason kept next to the rule in the CSS file. */
const ALLOW: Record<string, RegExp[]> = {};

const RULES: { name: string; pattern: RegExp; pagesOnly?: boolean }[] = [
  { name: 'hex colour literal (use a token)', pattern: /#[0-9a-f]{3,8}\b/i },
  { name: 'white keyword (use --color-surface or --color-on-accent)', pattern: /(?<![\w-])white(?![\w-])/ },
  { name: 'rgba() literal (use a token or color-mix)', pattern: /\brgba?\(/ },
  { name: '999px pill literal (use --radius-pill)', pattern: /\b999px\b/ },
  { name: 'raw palette background (use --color-surface, --color-surface-sunken or --color-band)', pattern: /background(?:-color)?:\s*var\(--color-(?:cloud|mist|midnight|midnight-light)\)/ },
  { name: 'button class outside Button.module.css', pattern: /\.(?:btn[A-Z]\w*|action(?:Primary|Secondary)|primary|secondary|homeLink|backButton)\b(?=[^;{]*\{)/ },
  { name: ':global(.on-dark) in a page module (use tokens)', pattern: /:global\(\.on-dark\)/, pagesOnly: true },
];

// `npm test` sets DESIGN_LINT=1; a bare `vitest run` skips this suite so component-only runs stay fast.
describe.skipIf(!process.env.DESIGN_LINT)('design lint over CSS modules', () => {
  const files = cssModules(SRC);

  it('finds the CSS modules', () => {
    expect(files.length).toBeGreaterThan(10);
  });

  for (const file of files) {
    const rel = relative(SRC, file);
    it(rel, () => {
      const css = readFileSync(file, 'utf8')
        // Drop comments so documented exceptions and explanations do not trip the rules.
        .replace(/\/\*[\s\S]*?\*\//g, '');
      const isButton = rel.endsWith('components/Button.module.css');
      const isPage = rel.startsWith('pages/');
      const problems: string[] = [];
      for (const rule of RULES) {
        if (rule.pagesOnly && !isPage) continue;
        if (isButton && rule.name.startsWith('button class')) continue;
        const allowed = ALLOW[rel] ?? [];
        for (const line of css.split('\n')) {
          if (!rule.pattern.test(line)) continue;
          if (allowed.some((ok) => ok.test(line))) continue;
          problems.push(`${rule.name}: ${line.trim()}`);
        }
      }
      expect(problems, `${rel} violates the design system`).toEqual([]);
    });
  }
});
