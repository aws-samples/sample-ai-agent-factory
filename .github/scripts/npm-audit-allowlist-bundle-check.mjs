#!/usr/bin/env node
/**
 * Proves the premise behind every entry in .github/npm-audit-allowlist.json: the allowlisted package is
 * BUILD TOOLING and never ships. Zero dependencies.
 *
 * `npm audit --omit=dev` (run by npm-audit-allowlist.mjs) shows the package is not in the production
 * dependency tree, but npm's dev/prod classification is declarative, not observed: application source that
 * imported a dev-only transitive package would be bundled into the deployed assets all the same while the
 * production audit stayed empty. This check reads the emitted bundle's own module graph instead. A build
 * with source maps (`vite build --sourcemap --outDir <dir>`) lists every module Vite/Rollup bundled, by its
 * original path, under `sources` in the `.map` files; any module under node_modules/<allowlisted package>/
 * fails the gate. It fails closed when there are no maps, a map is unreadable, or no first-party module is
 * listed (the maps did not come from a build of this project).
 *
 * Usage: node npm-audit-allowlist-bundle-check.mjs --dist <dir> [--allowlist <file>] [--now <ISO>]
 * Exit 0 = no allowlisted package is bundled. Exit 1 = one is, or the maps are unusable.
 * Self-test: node --test npm-audit-allowlist-bundle-check.test.mjs (same directory).
 */
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { GateError, loadAllowlist } from "./npm-audit-allowlist.mjs";

const NODE_MODULES = "node_modules/";

/** The npm package a bundled module path belongs to, or null for first-party and virtual modules. */
export function packageOf(source) {
  const path = source.replace(/\\/g, "/");
  const index = path.lastIndexOf(NODE_MODULES);
  if (index === -1) return null;
  const [first, second] = path.slice(index + NODE_MODULES.length).split("/");
  if (!first) return null;
  if (first.startsWith("@")) return second ? `${first}/${second}` : null;
  return first;
}

/** Vite/Rollup virtual modules carry a NUL prefix or a `virtual:` / `vite/` scheme; they are not first-party source. */
export function isVirtual(source) {
  return source.startsWith("\u0000") || source.startsWith("virtual:") || source.startsWith("vite/") || source.includes(":virtual:");
}

/** Every `.map` file under `dir`, recursively, sorted. */
export function walkMaps(dir) {
  const maps = [];
  const walk = (current) => {
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const full = join(current, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.isFile() && entry.name.endsWith(".map")) maps.push(full);
    }
  };
  walk(dir);
  return maps.sort();
}

/** The `sources` of one source map; anything but a plain v3 map with a string array is a GateError. */
export function readSources(mapPath) {
  let map;
  try {
    map = JSON.parse(readFileSync(mapPath, "utf8"));
  } catch (error) {
    throw new GateError(`source map ${mapPath} is not JSON: ${error.message}`);
  }
  if (map === null || typeof map !== "object" || Array.isArray(map)) throw new GateError(`source map ${mapPath} is not an object`);
  if (map.sections !== undefined) throw new GateError(`source map ${mapPath} is an index map (sections); not supported`);
  if (!Array.isArray(map.sources)) throw new GateError(`source map ${mapPath} has no sources array`);
  for (const source of map.sources) {
    if (typeof source !== "string") throw new GateError(`source map ${mapPath} lists a non-string source ${JSON.stringify(source)}`);
  }
  return map.sources;
}

/**
 * Judge the module graph: `sourcesByMap` is Map<mapLabel, sources[]>, `allow` is the parsed allowlist.
 * Returns { violations: string[], sourceCount, firstParty, packages: Set<string> }.
 */
export function evaluateBundle({ sourcesByMap, allow }) {
  const allowlisted = new Set([...allow.values()].map((entry) => entry.package));
  const violations = [];
  const packages = new Set();
  let sourceCount = 0;
  let firstParty = 0;
  for (const [label, sources] of sourcesByMap) {
    for (const source of sources) {
      sourceCount += 1;
      const name = packageOf(source);
      if (name === null) {
        if (!isVirtual(source)) firstParty += 1;
        continue;
      }
      packages.add(name);
      if (allowlisted.has(name)) violations.push(`${name} is bundled: ${label} lists ${source}`);
    }
  }
  if (sourceCount === 0) throw new GateError("the source maps list no bundled module at all");
  if (firstParty === 0) {
    throw new GateError("the source maps list no first-party module; this is not a source-mapped build of this project");
  }
  return { violations, sourceCount, firstParty, packages };
}

export function parseArgs(argv) {
  const args = { dist: null, allowlist: null, now: null };
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i];
    const value = argv[i + 1];
    const name = typeof key === "string" && key.startsWith("--") ? key.slice(2) : null;
    if (name === null || !Object.prototype.hasOwnProperty.call(args, name) || value === undefined) {
      throw new GateError(`bad argument ${JSON.stringify(key)}`);
    }
    args[name] = value;
  }
  if (args.dist === null) throw new GateError("--dist <directory of a source-mapped build> is required");
  return args;
}

export function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  const nowMillis = args.now ? Date.parse(args.now) : Date.now();
  if (!Number.isFinite(nowMillis)) throw new GateError(`--now ${JSON.stringify(args.now)} is not a date`);
  const here = dirname(fileURLToPath(import.meta.url));
  const allow = loadAllowlist(resolve(args.allowlist || resolve(here, "..", "npm-audit-allowlist.json")), nowMillis);
  const dist = resolve(args.dist);

  let maps;
  try {
    maps = walkMaps(dist);
  } catch (error) {
    throw new GateError(`cannot read ${dist}: ${error.message}`);
  }
  if (maps.length === 0) throw new GateError(`no source maps under ${dist}; build with --sourcemap so the module graph can be checked`);
  const sourcesByMap = new Map(maps.map((map) => [relative(dist, map), readSources(map)]));

  const { violations, sourceCount, firstParty, packages } = evaluateBundle({ sourcesByMap, allow });
  const names = [...allow.values()].map((entry) => entry.package).sort();
  console.log(
    `npm-audit-allowlist-bundle-check: ${maps.length} source map(s), ${sourceCount} bundled module(s) ` +
      `(${firstParty} first-party, ${packages.size} npm package(s)) under ${dist}`,
  );
  if (violations.length > 0) {
    for (const violation of violations) console.error(`npm-audit-allowlist-bundle-check: VIOLATION ${violation}`);
    throw new GateError(
      `${violations.length} bundled module(s) come from allowlisted package(s); the allowlist premise ` +
        "(build tooling only, never shipped) is false",
    );
  }
  console.log(`npm-audit-allowlist-bundle-check: PASS; not bundled: ${names.join(", ") || "(allowlist is empty)"}`);
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  try {
    main();
  } catch (error) {
    if (error instanceof GateError) {
      console.error(`npm-audit-allowlist-bundle-check: FAIL: ${error.message}`);
      process.exit(1);
    }
    throw error;
  }
}
