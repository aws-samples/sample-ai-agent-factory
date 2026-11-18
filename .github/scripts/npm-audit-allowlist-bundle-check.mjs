#!/usr/bin/env node
/**
 * Proves the premise behind every entry in .github/npm-audit-allowlist.json: the allowlisted package is
 * BUILD TOOLING and never ships. No dependencies beyond the project's own Vite.
 *
 * `npm audit --omit=dev` (run by npm-audit-allowlist.mjs) shows the package is not in the production
 * dependency tree, but that classification is declarative, not observed: application source importing a
 * dev-only transitive package is bundled all the same while the production audit stays empty. So this
 * check runs the project's own Vite build (same config file, production mode) into a scratch directory
 * with one extra plugin, and reads what Rollup actually resolved and wrote:
 *   - every output chunk's resolved module ids (`generateBundle`): a module under node_modules/<pkg>/ is
 *     attributed to <pkg> (paths normalised, query strings stripped, commonjs proxy ids included); an
 *     allowlisted package anywhere fails; a module that is neither a package, nor under the first-party
 *     root, nor a Rollup/Vite virtual module is UNKNOWN and fails closed;
 *   - every emitted asset: one with a script extension (plugin-emitted or worker code) is an opaque
 *     executable outside the graph and fails closed;
 *   - every file written to the output directory: a script must be a chunk of this build or a
 *     byte-identical copy of a file in the project's publicDir (repository-authored static asset, which
 *     is reported); symlinks fail;
 *   - every emitted HTML document: an external <script src> fails closed; a local src must name a known
 *     chunk or public copy; inline scripts are counted (they come from the project's own HTML entry).
 * Fails closed when the build emitted no chunk, or no first-party module or no npm package was seen.
 *
 * Usage: node npm-audit-allowlist-bundle-check.mjs --out <scratch dir OUTSIDE the project>
 *          [--project <dir, default cwd>] [--first-party-root <dir, default project>]
 *          [--allowlist <file>] [--now <ISO>]
 * Exit 0 = no allowlisted package is in the shipped bundle. Exit 1 = one is, or the build cannot be judged.
 * Self-test: node --test npm-audit-allowlist-bundle-check.test.mjs (same directory; needs a project with
 * Vite installed as cwd or BUNDLE_CHECK_VITE_PROJECT, because it runs real builds).
 */
import { existsSync, lstatSync, readdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, posix, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { GateError, loadAllowlist } from "./npm-audit-allowlist.mjs";

export const SCRIPT_RE = /\.(?:m?js|cjs)$/i;
const HTML_RE = /\.html?$/i;
const NODE_MODULES = "/node_modules/";
const toPosix = (path) => path.replace(/\\/g, "/");

/**
 * Classify one Rollup module id: { kind: "package", name } | { kind: "first-party" } | { kind: "virtual" } |
 * { kind: "unknown" }. Query strings (?commonjs-exports, ?url) are stripped and paths normalised before the
 * LAST node_modules/ segment names the package (nested and pnpm layouts). A NUL-prefixed id is a Rollup
 * virtual module, but one that wraps a node_modules file (commonjs proxies) still counts for its package.
 * Vite's own `vite/...` helpers, its `__vite-browser-external` stub and `virtual:` plugin modules are
 * virtual. Anything else outside the first-party root is unknown.
 */
export function classifyModule(id, firstPartyRoot) {
  if (typeof id !== "string" || id.length === 0) return { kind: "unknown" };
  const virtual = id.startsWith("\u0000");
  const path = posix.normalize(toPosix(virtual ? id.slice(1) : id).split("?")[0]);
  const index = path.lastIndexOf(NODE_MODULES);
  if (index !== -1) {
    const [first, second] = path.slice(index + NODE_MODULES.length).split("/");
    if (!first) return { kind: "unknown" };
    if (first.startsWith("@")) return second ? { kind: "package", name: `${first}/${second}` } : { kind: "unknown" };
    return { kind: "package", name: first };
  }
  // Vite's own helpers (`vite/...`), its stub for Node built-ins in browser builds
  // (`__vite-browser-external[:name]`), and plugin virtual modules following Vite's `virtual:` convention.
  if (virtual || path.startsWith("vite/") || path.startsWith("__vite-") || path.startsWith("virtual:")) return { kind: "virtual" };
  const root = posix.normalize(toPosix(firstPartyRoot)).replace(/\/+$/, "");
  if (path === root || path.startsWith(`${root}/`)) return { kind: "first-party" };
  return { kind: "unknown" };
}

/**
 * Judge the resolved graph. `chunks` is Map<fileName, moduleIds[]>, `assets` Map<fileName, anything>.
 * Returns { violations, modules, firstParty, virtual, packages: Set }.
 */
export function evaluateGraph({ chunks, assets, allow, firstPartyRoot }) {
  const allowlisted = new Set([...allow.values()].map((entry) => entry.package));
  if (chunks.size === 0) throw new GateError("the build emitted no JavaScript chunk; this is not a browser build");
  const violations = [];
  const packages = new Set();
  let modules = 0;
  let firstParty = 0;
  let virtual = 0;
  for (const [fileName, ids] of chunks) {
    if (!Array.isArray(ids) || ids.length === 0) throw new GateError(`chunk ${fileName} lists no modules; the module graph is incomplete`);
    for (const id of ids) {
      modules += 1;
      const owner = classifyModule(id, firstPartyRoot);
      if (owner.kind === "unknown") {
        throw new GateError(
          `chunk ${fileName} contains module ${JSON.stringify(id)} whose ownership cannot be established ` +
            `(not under node_modules/ and not under the first-party root ${firstPartyRoot})`,
        );
      }
      if (owner.kind === "virtual") virtual += 1;
      else if (owner.kind === "first-party") firstParty += 1;
      else {
        packages.add(owner.name);
        if (allowlisted.has(owner.name)) violations.push(`${owner.name} is bundled into ${fileName}: ${id}`);
      }
    }
  }
  for (const fileName of assets.keys()) {
    if (SCRIPT_RE.test(fileName)) {
      throw new GateError(`emitted asset ${fileName} is a script outside the module graph (plugin-emitted or worker code); refusing to judge an opaque executable`);
    }
  }
  if (firstParty === 0) throw new GateError("no first-party module in any chunk; this is not a build of this project");
  if (packages.size === 0) throw new GateError("no npm package in any chunk; module ids look rewritten, so packages cannot be identified");
  return { violations, modules, firstParty, virtual, packages };
}

/** Every regular file under `dir` as a sorted posix path relative to it; a symlink anywhere fails closed. */
export function walkOutput(dir) {
  const files = [];
  const walk = (current) => {
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const full = join(current, entry.name);
      if (entry.isSymbolicLink()) throw new GateError(`${full} is a symlink; the output directory must contain only regular files`);
      if (entry.isDirectory()) walk(full);
      else if (entry.isFile()) files.push(toPosix(posix.normalize(toPosix(full).slice(toPosix(dir).length + 1))));
      else throw new GateError(`${full} is not a regular file`);
    }
  };
  walk(dir);
  return files.sort();
}

const SCRIPT_TAG_RE = /<script\b([^>]*)>/gi;
const SRC_ATTR_RE = /\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))/i;
const EXTERNAL_RE = /^(?:[a-z][a-z0-9+.-]*:)?\/\//i;

/**
 * Reconcile the written output with the graph: every script must be a chunk of this build or a
 * byte-identical copy of the same relative path under `publicDir`; every HTML document may only load local
 * scripts that are known. Returns { scripts, publicCopies, documents, inline }.
 */
export function checkOutputs({ outDir, files, chunkFiles, publicDir, base }) {
  const known = new Set(chunkFiles);
  const publicCopies = [];
  for (const rel of files) {
    if (!SCRIPT_RE.test(rel) || known.has(rel)) continue;
    const copy = publicDir ? join(publicDir, rel) : null;
    const isCopy =
      copy !== null &&
      existsSync(copy) &&
      lstatSync(copy).isFile() &&
      readFileSync(copy).equals(readFileSync(join(outDir, rel)));
    if (!isCopy) {
      throw new GateError(
        `output ${rel} is a script that is neither a chunk of this build nor a byte-identical copy of ` +
          `${publicDir ? join(publicDir, rel) : "(no publicDir)"}; refusing an opaque executable`,
      );
    }
    publicCopies.push(rel);
    known.add(rel);
  }
  let documents = 0;
  let inline = 0;
  const prefix = base.endsWith("/") ? base : `${base}/`;
  for (const rel of files) {
    if (!HTML_RE.test(rel)) continue;
    documents += 1;
    const html = readFileSync(join(outDir, rel), "utf8");
    for (const tag of html.matchAll(SCRIPT_TAG_RE)) {
      const src = SRC_ATTR_RE.exec(tag[1]);
      if (!src) {
        inline += 1;
        continue;
      }
      const url = src[1] ?? src[2] ?? src[3];
      if (EXTERNAL_RE.test(url)) throw new GateError(`${rel} loads an external script ${url}; it is outside the module graph`);
      const path = url.split(/[?#]/)[0];
      const target = posix.normalize(path.startsWith(prefix) ? path.slice(prefix.length) : path.startsWith("/") ? path.slice(1) : posix.join(posix.dirname(rel), path));
      if (!known.has(target)) throw new GateError(`${rel} loads ${url}, which is not a chunk of this build nor a public copy`);
    }
  }
  return { scripts: [...known].sort(), publicCopies, documents, inline };
}

/** Locate the project's own Vite (its ESM entry and version) through the project's resolution. */
export function resolveVite(projectDir) {
  const require = createRequire(join(projectDir, "package.json"));
  let packagePath;
  try {
    packagePath = require.resolve("vite/package.json");
  } catch (error) {
    throw new GateError(`vite is not installed for ${projectDir} (run npm ci first): ${error.message}`);
  }
  const pkg = JSON.parse(readFileSync(packagePath, "utf8"));
  let target = pkg.exports?.["."] ?? pkg.exports;
  while (target !== null && typeof target === "object") target = target.import ?? target.default;
  if (typeof target !== "string") throw new GateError(`cannot find vite's ESM entry in ${packagePath}`);
  return { entry: resolve(dirname(packagePath), target), version: pkg.version };
}

/**
 * Run the project's Vite build into `outDir` with a recording plugin. Returns
 * { chunks: Map<fileName, moduleIds[]>, assets: Map<fileName, type>, config: { root, base, publicDir } }.
 */
export async function buildGraph({ projectDir, outDir, vite }) {
  const record = { chunks: new Map(), assets: new Map(), config: null };
  const plugin = {
    name: "npm-audit-allowlist:module-graph",
    enforce: "post",
    configResolved(config) {
      record.config = { root: config.root, base: config.base, publicDir: config.publicDir || null };
    },
    generateBundle(_options, bundle) {
      for (const [fileName, output] of Object.entries(bundle)) {
        if (output.type === "chunk") record.chunks.set(fileName, output.moduleIds ?? Object.keys(output.modules ?? {}));
        else record.assets.set(fileName, output.type);
      }
    },
  };
  // Configs commonly resolve paths against the current directory (loadEnv(mode, '.')), as the CLI does.
  const previousCwd = process.cwd();
  process.chdir(projectDir);
  try {
    await vite.build({
      root: projectDir,
      mode: "production",
      logLevel: "warn",
      build: { outDir, emptyOutDir: true, write: true, sourcemap: false, reportCompressedSize: false },
      plugins: [plugin],
    });
  } finally {
    process.chdir(previousCwd);
  }
  if (record.config === null) throw new GateError("the module-graph plugin never saw the resolved config; the build did not run");
  return record;
}

export function parseArgs(argv) {
  const args = { out: null, project: null, "first-party-root": null, allowlist: null, now: null };
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i];
    const value = argv[i + 1];
    const name = typeof key === "string" && key.startsWith("--") ? key.slice(2) : null;
    if (name === null || !Object.prototype.hasOwnProperty.call(args, name) || value === undefined) {
      throw new GateError(`bad argument ${JSON.stringify(key)}`);
    }
    args[name] = value;
  }
  if (args.out === null) throw new GateError("--out <scratch directory outside the project> is required; it is emptied and rebuilt");
  return args;
}

export async function main(argv = process.argv.slice(2), { log = console.log, error = console.error } = {}) {
  const args = parseArgs(argv);
  const nowMillis = args.now ? Date.parse(args.now) : Date.now();
  if (!Number.isFinite(nowMillis)) throw new GateError(`--now ${JSON.stringify(args.now)} is not a date`);
  const here = dirname(fileURLToPath(import.meta.url));
  const allow = loadAllowlist(resolve(args.allowlist || resolve(here, "..", "npm-audit-allowlist.json")), nowMillis);
  const projectDir = resolve(args.project || process.cwd());
  const outDir = resolve(args.out);
  const firstPartyRoot = resolve(args["first-party-root"] || projectDir);
  if (outDir === projectDir || outDir.startsWith(`${projectDir}${sep}`)) {
    throw new GateError(`--out ${outDir} must be outside the project ${projectDir}: it is emptied and rebuilt`);
  }

  const { entry, version } = resolveVite(projectDir);
  const vite = await import(pathToFileURL(entry).href);
  const record = await buildGraph({ projectDir, outDir, vite });
  const graph = evaluateGraph({ chunks: record.chunks, assets: record.assets, allow, firstPartyRoot });
  const outputs = checkOutputs({
    outDir,
    files: walkOutput(outDir),
    chunkFiles: new Set(record.chunks.keys()),
    publicDir: record.config.publicDir,
    base: record.config.base,
  });

  const names = [...allow.values()].map((item) => item.package).sort();
  log(
    `npm-audit-allowlist-bundle-check: vite ${version} built ${projectDir} into ${outDir}: ${record.chunks.size} chunk(s), ` +
      `${graph.modules} resolved module(s) (${graph.firstParty} first-party under ${firstPartyRoot}, ${graph.virtual} virtual, ` +
      `${graph.packages.size} npm package(s)); ${outputs.scripts.length} script(s) written, ` +
      `${outputs.publicCopies.length} verbatim public cop${outputs.publicCopies.length === 1 ? "y" : "ies"}` +
      `${outputs.publicCopies.length ? ` (${outputs.publicCopies.join(", ")})` : ""}; ${outputs.documents} HTML document(s) with ${outputs.inline} inline script(s)`,
  );
  if (graph.violations.length > 0) {
    for (const violation of graph.violations) error(`npm-audit-allowlist-bundle-check: VIOLATION ${violation}`);
    throw new GateError(
      `${graph.violations.length} bundled module(s) come from allowlisted package(s); the allowlist premise ` +
        "(build tooling only, never shipped) is false",
    );
  }
  log(`npm-audit-allowlist-bundle-check: PASS; not in the shipped bundle: ${names.join(", ") || "(allowlist is empty)"}`);
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main().catch((error) => {
    if (error instanceof GateError) {
      console.error(`npm-audit-allowlist-bundle-check: FAIL: ${error.message}`);
      process.exit(1);
    }
    throw error;
  });
}
