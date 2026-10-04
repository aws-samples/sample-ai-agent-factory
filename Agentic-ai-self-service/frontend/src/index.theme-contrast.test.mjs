import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

const cssPath = [
  resolve(process.cwd(), 'src/index.css'),
  resolve(process.cwd(), 'frontend/src/index.css'),
].find(existsSync);

if (!cssPath) {
  throw new Error(`Unable to locate index.css from ${process.cwd()}`);
}

const css = readFileSync(cssPath, 'utf8');

describe('dark-theme warning contrast', () => {
  it('remaps text-amber-800 to the readable dark-theme amber token', () => {
    expect(css).toMatch(
      /\.text-amber-800:not\(\.no-darkmap\),[\s\S]*?color:\s*var\(--neon-amber\)\s*!important;/,
    );
  });

  it('gives the React Flow attribution readable text in light mode', () => {
    expect(css).toMatch(
      /:root\[data-theme="light"\][\s\S]*?\.react-flow__attribution a\s*\{\s*color:\s*var\(--color-text-secondary\)\s*!important;\s*\}/,
    );
  });
});
