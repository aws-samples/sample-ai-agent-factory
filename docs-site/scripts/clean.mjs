#!/usr/bin/env node
/**
 * Cross-platform clean script for generated files.
 * Only removes known generated paths - never recursive wildcards.
 */
import { rm } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = join(__dirname, '..');

// Known generated paths only - no wildcards
const GENERATED_PATHS = [
  'dist',
  'dist-ssr',
  'coverage',
  'test-results',
  'playwright-report',
  'node_modules/.vite',
  'tsconfig.node.tsbuildinfo',
  'vite.config.js',
  'vite.config.d.ts',
  'vitest.config.js',
  'vitest.config.d.ts',
];

async function clean() {
  for (const rel of GENERATED_PATHS) {
    const abs = join(root, rel);
    try {
      await rm(abs, { recursive: true, force: true });
      console.log(`Removed: ${rel}`);
    } catch {
      // Ignore if doesn't exist
    }
  }
  console.log('Clean complete.');
}

clean();
