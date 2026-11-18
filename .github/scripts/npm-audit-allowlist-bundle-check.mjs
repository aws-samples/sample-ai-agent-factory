#!/usr/bin/env node
/**
 * Proves the premise behind every entry in .github/npm-audit-allowlist.json: the allowlisted package is
 * BUILD TOOLING and never ships. No dependencies beyond the project's own Vite.
 *
 * `npm audit --omit=dev` (run by npm-audit-allowlist.mjs) shows the package is not in the production
 * dependency tree, but that classification is declarative: application source importing a dev-only
 * transitive package is bundled all the same. So this check runs the project's own Vite build (same
 * config file, production mode, same environment as the deployment build) into a scratch directory with
 * one recording plugin, and judges what Rollup actually resolved and wrote:
 *   - every chunk's resolved module ids (`generateBundle`), main build and worker sub-builds alike
 *     (`worker.plugins`): a module under node_modules/<pkg>/ is attributed to <pkg> (paths normalised,
 *     query strings stripped, commonjs proxy ids included); an allowlisted package anywhere fails; a
 *     module that is neither a package, nor under the first-party root, nor one of Vite's own helper
 *     modules, nor a virtual module listed in the allowlist's bundle.trustedVirtualModules, fails closed;
 *   - every emitted asset: a script extension (a file worker or plugin-emitted code) or any type a browser
 *     could run as a program is refused; only inert types (styles, images, fonts, data, wasm, html) pass;
 *   - every chunk's final bytes (`writeBundle`): the file on disk must still hash the same;
 *   - every script written to the output: a chunk of this build, or a repository-authored file PINNED by
 *     sha256 in bundle.firstPartyScripts that is also a byte-identical, git-tracked copy from publicDir;
 *   - every HTML document: no <base> element; a <script src> must be a known chunk or pinned copy, never
 *     external; every inline script body must match a pin (sha256 of its normalised text, or an anchored
 *     pattern for generated stubs); attributes are parsed quote-aware with exact names;
 *   - publicDir must lie inside the first-party root; symlinks in the output fail;
 *   - with --verify-dist, the real deployment build's output must consist of exactly those chunks
 *     (same bytes), pinned copies and pinned documents, so the judged graph is the shipped artifact.
 * Trust boundary, stated plainly: code authored in this repository (modules under the first-party root,
 * pinned public copies and inline scripts, the Vite configuration and its plugins) is trusted as
 * reviewed; this gate establishes what the npm dependency graph of that build contains.
 *
 * Usage: node npm-audit-allowlist-bundle-check.mjs --out <scratch dir OUTSIDE the project>
 *          [--project <dir, default cwd>] [--first-party-root <dir, default project>]
 *          [--verify-dist <the deployment build's output dir>] [--allowlist <file>] [--now <ISO>]
 * Exit 0 = no allowlisted package is in the shipped bundle. Exit 1 = one is, or the build cannot be judged.
 * Self-test: node --test npm-audit-allowlist-bundle-check.test.mjs (same directory; needs a project with
 * Vite installed as cwd or BUNDLE_CHECK_VITE_PROJECT, because it runs real builds).
 */
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, lstatSync, readdirSync, readFileSync, realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { basename, dirname, join, posix, relative, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { GateError, parseAllowlist } from "./npm-audit-allowlist.mjs";

export const SCRIPT_RE = /\.(?:m?js|cjs)$/i;
const HTML_RE = /\.html?$/i;
// Emitted (non-chunk) assets a browser does not run as a program. Anything else is refused.
const INERT_ASSET_RE = /\.(?:css|svg|png|jpe?g|gif|webp|avif|ico|woff2?|ttf|otf|eot|json|webmanifest|xml|pdf|mp4|webm|mp3|wasm|html?)$/i;
const SHA256_RE = /^[0-9a-f]{64}$/;
const NODE_MODULES = "/node_modules/";
export const WORKER_PREFIX = "worker:";
/** Virtual modules Vite itself injects into every browser build; nothing else is trusted implicitly. */
export const BUILTIN_VIRTUAL = new Set(["commonjsHelpers.js", "vite/preload-helper.js", "vite/modulepreload-polyfill.js"]);

const toPosix = (path) => path.replace(/\\/g, "/");
export const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const isPlainObject = (value) => value !== null && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype;
const isNonEmptyString = (value) => typeof value === "string" && value.length > 0;
const under = (path, root) => {
  const normalisedRoot = posix.normalize(toPosix(root)).replace(/\/+$/, "");
  const normalisedPath = posix.normalize(toPosix(path));
  return normalisedPath === normalisedRoot || normalisedPath.startsWith(`${normalisedRoot}/`);
};

/**
 * Classify one Rollup module id: { kind: "package", name } | { kind: "first-party" } | { kind: "virtual" } |
 * { kind: "unknown", why }. Query strings are stripped and the path normalised before the LAST
 * node_modules/ segment names the package (nested and pnpm layouts; a NUL-prefixed commonjs proxy of a
 * node_modules file counts for its package; a dot-prefixed entry such as .vite is a cache, not a package).
 * Virtual ids (NUL prefix, `vite/...`, `__vite-...`, `virtual:...`) are trusted only when they are Vite's
 * own helpers or listed in `trustedVirtual`. Ids under the first-party root (compared as paths) are
 * first-party. Everything else is unknown.
 */
export function classifyModule(id, firstPartyRoot, trustedVirtual = new Set()) {
  if (typeof id !== "string" || id.length === 0) return { kind: "unknown", why: "empty module id" };
  const nul = id.startsWith("\u0000");
  const bare = toPosix(nul ? id.slice(1) : id);
  const path = posix.normalize(bare.split("?")[0]);
  const index = path.lastIndexOf(NODE_MODULES);
  if (index !== -1) {
    const [first, second] = path.slice(index + NODE_MODULES.length).split("/");
    if (!first || first.startsWith(".")) return { kind: "unknown", why: `node_modules entry ${JSON.stringify(first ?? "")} is not a package name` };
    if (first.startsWith("@")) {
      return second && !second.startsWith(".") ? { kind: "package", name: `${first}/${second}` } : { kind: "unknown", why: "incomplete scoped package name" };
    }
    return { kind: "package", name: first };
  }
  if (nul || path.startsWith("vite/") || path.startsWith("__vite-") || path.startsWith("virtual:")) {
    if (BUILTIN_VIRTUAL.has(path) || path.startsWith("__vite-browser-external")) return { kind: "virtual" };
    if (trustedVirtual.has(id) || trustedVirtual.has(path) || trustedVirtual.has(`\u0000${path}`)) return { kind: "virtual" };
    return { kind: "unknown", why: "a virtual module that is not one of Vite's helpers and not listed in bundle.trustedVirtualModules" };
  }
  if (under(path, firstPartyRoot)) return { kind: "first-party" };
  return { kind: "unknown", why: `outside the first-party root ${firstPartyRoot}` };
}

/**
 * Judge the resolved graph. `chunks` is Map<key, moduleIds[]> (worker sub-build chunks keyed "worker:…"),
 * `assets` Map<fileName, type>. Returns { violations, modules, firstParty, virtual, packages: Set }.
 */
export function evaluateGraph({ chunks, assets, allow, firstPartyRoot, trustedVirtual = new Set() }) {
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
      const owner = classifyModule(id, firstPartyRoot, trustedVirtual);
      if (owner.kind === "unknown") {
        throw new GateError(`chunk ${fileName} contains module ${JSON.stringify(id)} whose ownership cannot be established: ${owner.why}`);
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
      throw new GateError(
        `emitted asset ${fileName} is a script outside the module graph (a file worker or plugin-emitted code; only inline ` +
          "workers are judged); refusing an opaque executable",
      );
    }
    if (!INERT_ASSET_RE.test(fileName)) throw new GateError(`emitted asset ${fileName} has no recognised inert type; refusing an opaque payload`);
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
      else if (entry.isFile()) files.push(toPosix(relative(dir, full)));
      else throw new GateError(`${full} is not a regular file`);
    }
  };
  walk(dir);
  return files.sort();
}

/** True when `file` is tracked by the git repository containing it. */
export function gitTracked(file) {
  const result = spawnSync("git", ["-C", dirname(file), "ls-files", "--error-unmatch", "--", basename(file)], { encoding: "utf8" });
  return result.status === 0;
}

/** The git top-level directory of `dir`; a project outside version control cannot be judged. */
export function gitToplevel(dir) {
  const result = spawnSync("git", ["-C", dir, "rev-parse", "--show-toplevel"], { encoding: "utf8" });
  if (result.status !== 0 || !isNonEmptyString(result.stdout.trim())) {
    throw new GateError(`${dir} is not inside a git repository (${(result.stderr || "").trim()}); the project must be version-controlled`);
  }
  return realpathSync(result.stdout.trim());
}

/** The key under which the allowlist's bundle section names this project: its path from the git top level, or ".". */
export function projectKey(projectDir) {
  const real = realpathSync(projectDir);
  const rel = toPosix(relative(gitToplevel(real), real));
  if (rel.startsWith("..")) throw new GateError(`${projectDir} resolves outside its own git top level`);
  return rel === "" ? "." : rel;
}

/** Normalise an inline script body for pinning: lines trimmed, empty lines dropped. */
export function normalizeInline(body) {
  return body
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
    .join("\n");
}

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Fail closed when repository-authored text names an allowlisted package by word: a hand-vendored copy is outside npm audit. */
export function tripwire(label, text, packages) {
  for (const name of packages) {
    if (new RegExp(`(?:^|[^\\w@/.-])${escapeRegExp(name)}(?![\\w/.-])`, "i").test(text)) {
      throw new GateError(`${label} mentions allowlisted package ${name}; a hand-vendored copy of a dependency is outside npm audit and is refused`);
    }
  }
}

/**
 * The allowlist file's `bundle` section for one project key. Returns { virtual: Set<id>, publicScripts:
 * Map<relativePath, sha256>, inline: [{ sha256 } | { pattern: RegExp }] }. Every entry is validated whether
 * or not it is for this project; a defective file fails every run.
 */
export function parseBundleTrust(doc, project) {
  const trust = { virtual: new Set(), publicScripts: new Map(), inline: [] };
  if (!isPlainObject(doc)) throw new GateError("allowlist must be an object");
  const section = doc.bundle;
  if (section === undefined) return trust;
  if (!isPlainObject(section)) throw new GateError('allowlist "bundle" must be an object');
  const list = (key) => {
    const value = section[key];
    if (value === undefined) return [];
    if (!Array.isArray(value)) throw new GateError(`allowlist bundle.${key} must be an array`);
    return value;
  };
  const reasoned = (entry, where) => {
    if (!isPlainObject(entry)) throw new GateError(`${where} must be an object`);
    if (!isNonEmptyString(entry.project)) throw new GateError(`${where} must name its project (path from the repository root, or ".")`);
    if (typeof entry.reason !== "string" || entry.reason.trim().length < 20) throw new GateError(`${where}: needs a written reason`);
  };
  list("trustedVirtualModules").forEach((entry, index) => {
    const where = `bundle.trustedVirtualModules[${index}]`;
    reasoned(entry, where);
    if (!isNonEmptyString(entry.id)) throw new GateError(`${where} must give the virtual module id`);
    if (entry.project === project) trust.virtual.add(entry.id);
  });
  list("firstPartyScripts").forEach((entry, index) => {
    const where = `bundle.firstPartyScripts[${index}]`;
    reasoned(entry, where);
    const isPublic = entry.public !== undefined;
    const isInline = entry.inline !== undefined;
    if (isPublic === isInline) throw new GateError(`${where} must pin exactly one of "public" (a relative path) or "inline" ("sha256" | "pattern")`);
    if (isPublic) {
      if (!isNonEmptyString(entry.public) || entry.public.startsWith("/") || entry.public.split("/").includes("..") || !SCRIPT_RE.test(entry.public)) {
        throw new GateError(`${where}: public must be a relative script path inside the output`);
      }
      if (!isNonEmptyString(entry.sha256) || !SHA256_RE.test(entry.sha256)) throw new GateError(`${where}: sha256 must be 64 lowercase hex digits`);
      if (entry.project === project) {
        if (trust.publicScripts.has(entry.public)) throw new GateError(`${where}: ${entry.public} is pinned twice for ${project}`);
        trust.publicScripts.set(entry.public, entry.sha256);
      }
      return;
    }
    let pin;
    if (entry.inline === "sha256") {
      if (!isNonEmptyString(entry.sha256) || !SHA256_RE.test(entry.sha256)) throw new GateError(`${where}: sha256 must be 64 lowercase hex digits`);
      pin = { sha256: entry.sha256 };
    } else if (entry.inline === "pattern") {
      if (!isNonEmptyString(entry.pattern) || !entry.pattern.startsWith("^") || !entry.pattern.endsWith("$")) {
        throw new GateError(`${where}: pattern must be an anchored regular expression (^...$)`);
      }
      try {
        pin = { pattern: new RegExp(entry.pattern) };
      } catch (error) {
        throw new GateError(`${where}: pattern does not compile: ${error.message}`);
      }
    } else {
      throw new GateError(`${where}: inline must be "sha256" or "pattern"`);
    }
    if (entry.project === project) trust.inline.push(pin);
  });
  return trust;
}

export function loadAllowlistDocument(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new GateError(`cannot read allowlist ${path}: ${error.message}`);
  }
}

/** Parse the attributes of one start tag, quote-aware, exact names; duplicates or leftovers fail closed. */
export function parseAttributes(text, label) {
  const attrs = new Map();
  let rest = text;
  const ATTR = /^\s*([^\s"'=<>/`]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/;
  while (rest.trim().length > 0) {
    const match = ATTR.exec(rest);
    if (!match) throw new GateError(`${label}: cannot parse attributes near ${JSON.stringify(rest.trim().slice(0, 40))}`);
    const name = match[1].toLowerCase();
    if (attrs.has(name)) throw new GateError(`${label}: attribute ${name} is repeated`);
    attrs.set(name, match[2] ?? match[3] ?? match[4] ?? null);
    rest = rest.slice(match[0].length);
  }
  return attrs;
}

/**
 * Every <script> element of an HTML document in order: { attrs: Map, body }. The start tag is scanned
 * quote-aware (a ">" inside a quoted value does not end it) and the body runs to the matching </script>,
 * so a "<script" inside a script body is not a tag. Unterminated markup fails closed.
 */
export function scanScripts(html, label) {
  const scripts = [];
  const lower = html.toLowerCase();
  let from = 0;
  for (;;) {
    const at = lower.indexOf("<script", from);
    if (at === -1) break;
    const after = lower[at + 7];
    if (after !== undefined && !/[\s/>]/.test(after)) {
      from = at + 7;
      continue;
    }
    let i = at + 7;
    let quote = null;
    while (i < html.length) {
      const ch = html[i];
      if (quote) {
        if (ch === quote) quote = null;
      } else if (ch === '"' || ch === "'") quote = ch;
      else if (ch === ">") break;
      i += 1;
    }
    if (i >= html.length) throw new GateError(`${label}: unterminated <script> start tag`);
    const attrs = parseAttributes(html.slice(at + 7, i).replace(/\/\s*$/, ""), `${label}: <script>`);
    const close = lower.indexOf("</script", i + 1);
    if (close === -1) throw new GateError(`${label}: <script> without </script>`);
    scripts.push({ attrs, body: html.slice(i + 1, close) });
    from = close + 8;
  }
  return scripts;
}

const EXTERNAL_RE = /^(?:[a-z][a-z0-9+.-]*:)?\/\//i;

/**
 * Judge the HTML documents among `files`: no <base>; external or unknown script sources fail; inline
 * bodies must match a pin. Returns { documents, inline, pinnedBySha256, pinnedByPattern }.
 */
export function checkDocuments({ dir, files, known, base, trust, packages = [], label = "" }) {
  const prefix = base.endsWith("/") ? base : `${base}/`;
  const tag = (rel) => `${label}${rel}`;
  let documents = 0;
  let inline = 0;
  let pinnedBySha256 = 0;
  let pinnedByPattern = 0;
  for (const rel of files) {
    if (!HTML_RE.test(rel)) continue;
    documents += 1;
    const html = readFileSync(join(dir, rel), "utf8");
    if (/<base[\s/>]/i.test(html)) throw new GateError(`${tag(rel)} declares a <base> element; document-relative resolution is refused`);
    for (const { attrs, body } of scanScripts(html, tag(rel))) {
      if (!attrs.has("src")) {
        const normalised = normalizeInline(body);
        if (normalised.length === 0) continue;
        inline += 1;
        tripwire(`inline script ${inline} of ${tag(rel)}`, normalised, packages);
        const digest = sha256(normalised);
        if (trust.inline.some((pin) => pin.sha256 === digest)) pinnedBySha256 += 1;
        else if (trust.inline.some((pin) => pin.pattern instanceof RegExp && pin.pattern.test(normalised))) pinnedByPattern += 1;
        else throw new GateError(`${tag(rel)}: inline script ${inline} is not pinned in bundle.firstPartyScripts (sha256 of its normalised body: ${digest})`);
        continue;
      }
      const url = attrs.get("src");
      if (!isNonEmptyString(url)) throw new GateError(`${tag(rel)}: <script> with an empty src`);
      if (EXTERNAL_RE.test(url)) throw new GateError(`${tag(rel)} loads an external script ${url}; it is outside the module graph`);
      const path = url.split(/[?#]/)[0];
      const target = posix.normalize(path.startsWith(prefix) ? path.slice(prefix.length) : path.startsWith("/") ? path.slice(1) : posix.join(posix.dirname(rel), path));
      if (!known.has(target)) throw new GateError(`${tag(rel)} loads ${url}, which is not a chunk of this build nor a pinned public copy`);
    }
  }
  return { documents, inline, pinnedBySha256, pinnedByPattern };
}

/**
 * Judge the scripts among `files` in `dir`: a chunk of this build (same bytes as Rollup wrote), or a pinned,
 * git-tracked, byte-identical copy from publicDir. Returns { known: Set<rel>, publicCopies: string[] }.
 */
export function checkScripts({ dir, files, chunkHashes, trust, publicDir, packages = [], isTracked = gitTracked, label = "" }) {
  const known = new Set();
  const publicCopies = [];
  for (const rel of files) {
    if (!SCRIPT_RE.test(rel)) continue;
    const written = readFileSync(join(dir, rel));
    const digest = sha256(written);
    if (chunkHashes.has(rel)) {
      if (chunkHashes.get(rel) !== digest) throw new GateError(`${label}${rel} differs from the chunk Rollup wrote; it was rewritten after the build`);
      known.add(rel);
      continue;
    }
    const pin = trust.publicScripts.get(rel);
    if (pin === undefined) {
      throw new GateError(`${label}${rel} is a script that is not a chunk of this build and not pinned in bundle.firstPartyScripts (sha256 ${digest})`);
    }
    if (pin !== digest) throw new GateError(`${label}${rel} does not match its pin (sha256 ${digest}); review the change and re-pin it`);
    const copy = publicDir ? join(publicDir, rel) : null;
    if (copy === null || !existsSync(copy) || !lstatSync(copy).isFile() || !readFileSync(copy).equals(written)) {
      throw new GateError(`${label}${rel} is pinned but is not a byte-identical copy of ${copy ?? "(no publicDir)"}`);
    }
    if (!isTracked(copy)) throw new GateError(`${label}${rel} comes from ${copy}, which git does not track; a repository-authored static asset must be version-controlled`);
    tripwire(`public copy ${label}${rel}`, written.toString("utf8"), packages);
    publicCopies.push(rel);
    known.add(rel);
  }
  return { known, publicCopies };
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
 * Run the project's Vite build into `outDir` with the recording plugin (also installed into worker
 * sub-builds). Returns { chunks: Map<key, moduleIds[]>, assets: Map<fileName, type>, hashes:
 * Map<fileName, sha256 of the final chunk code>, config: { root, base, publicDir } }.
 */
export async function buildGraph({ projectDir, outDir, vite }) {
  const record = { chunks: new Map(), assets: new Map(), hashes: new Map(), config: null };
  const recorder = (prefix) => ({
    name: `npm-audit-allowlist:module-graph${prefix ? ":worker" : ""}`,
    enforce: "post",
    configResolved(config) {
      if (!prefix) record.config = { root: config.root, base: config.base, publicDir: config.publicDir || null };
    },
    generateBundle(_options, bundle) {
      for (const [fileName, output] of Object.entries(bundle)) {
        if (output.type === "chunk") record.chunks.set(`${prefix}${fileName}`, output.moduleIds ?? Object.keys(output.modules ?? {}));
        else if (!prefix) record.assets.set(fileName, output.type);
      }
    },
    writeBundle(_options, bundle) {
      if (prefix) return;
      for (const [fileName, output] of Object.entries(bundle)) {
        if (output.type === "chunk") record.hashes.set(fileName, sha256(output.code));
      }
    },
  });
  // Configs commonly resolve paths against the current directory (loadEnv(mode, '.')), as the CLI does.
  const previousCwd = process.cwd();
  process.chdir(projectDir);
  try {
    await vite.build({
      root: projectDir,
      mode: "production",
      logLevel: "warn",
      build: { outDir, emptyOutDir: true, write: true, sourcemap: false, reportCompressedSize: false },
      plugins: [recorder("")],
      worker: { plugins: () => [recorder(WORKER_PREFIX)] },
    });
  } finally {
    process.chdir(previousCwd);
  }
  if (record.config === null) throw new GateError("the module-graph plugin never saw the resolved config; the build did not run");
  for (const key of record.chunks.keys()) {
    if (!key.startsWith(WORKER_PREFIX) && !record.hashes.has(key)) throw new GateError(`chunk ${key} was generated but never written`);
  }
  return record;
}

export function parseArgs(argv) {
  const args = { out: null, project: null, "first-party-root": null, "verify-dist": null, allowlist: null, now: null };
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

/** Real path of a location that may not exist yet: its first existing ancestor is resolved, the rest re-appended. */
export function realpathDeep(path) {
  let current = resolve(path);
  const tail = [];
  while (!existsSync(current)) {
    tail.unshift(basename(current));
    const parent = dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return join(realpathSync(current), ...tail);
}

export async function main(argv = process.argv.slice(2), { log = console.log, error = console.error } = {}) {
  const args = parseArgs(argv);
  const nowMillis = args.now ? Date.parse(args.now) : Date.now();
  if (!Number.isFinite(nowMillis)) throw new GateError(`--now ${JSON.stringify(args.now)} is not a date`);
  const here = dirname(fileURLToPath(import.meta.url));
  const projectDir = realpathSync(resolve(args.project || process.cwd()));
  const outDir = realpathDeep(args.out);
  const firstPartyRoot = realpathSync(resolve(args["first-party-root"] || projectDir));
  if (outDir === projectDir || outDir.startsWith(`${projectDir}${sep}`)) {
    throw new GateError(`--out ${outDir} must be outside the project ${projectDir}: it is emptied and rebuilt`);
  }
  const key = projectKey(projectDir);
  const doc = loadAllowlistDocument(resolve(args.allowlist || resolve(here, "..", "npm-audit-allowlist.json")));
  const allow = parseAllowlist(doc, nowMillis);
  const trust = parseBundleTrust(doc, key);
  const names = [...allow.values()].map((item) => item.package).sort();

  const { entry, version } = resolveVite(projectDir);
  const vite = await import(pathToFileURL(entry).href);
  const record = await buildGraph({ projectDir, outDir, vite });
  // A publicDir that does not exist contributed nothing; one that exists must be repository-authored.
  if (record.config.publicDir !== null && existsSync(record.config.publicDir) && !under(realpathSync(record.config.publicDir), firstPartyRoot)) {
    throw new GateError(`publicDir ${record.config.publicDir} lies outside the first-party root ${firstPartyRoot}; its files cannot be called repository-authored`);
  }
  const graph = evaluateGraph({ chunks: record.chunks, assets: record.assets, allow, firstPartyRoot, trustedVirtual: trust.virtual });

  const judge = (dir, label) => {
    const files = walkOutput(dir);
    const { known, publicCopies } = checkScripts({ dir, files, chunkHashes: record.hashes, trust, publicDir: record.config.publicDir, packages: names, label });
    const documents = checkDocuments({ dir, files, known, base: record.config.base, trust, packages: names, label });
    return { files: files.length, scripts: known.size, publicCopies, ...documents };
  };
  const built = judge(outDir, "");
  let shipped = null;
  if (args["verify-dist"]) {
    const dist = resolve(args["verify-dist"]);
    if (!existsSync(dist)) throw new GateError(`--verify-dist ${dist} does not exist; run the deployment build first`);
    shipped = judge(dist, `${dist}${sep}`);
    if (shipped.scripts === 0) throw new GateError(`--verify-dist ${dist} contains no script; this is not the deployment build's output`);
  }

  const mainChunks = record.hashes.size;
  const describe = (result) =>
    `${result.scripts} script(s) (${result.publicCopies.length} pinned public cop${result.publicCopies.length === 1 ? "y" : "ies"}` +
    `${result.publicCopies.length ? `: ${result.publicCopies.join(", ")}` : ""}), ${result.documents} document(s) with ${result.inline} inline ` +
    `script(s) (${result.pinnedBySha256} pinned by sha256, ${result.pinnedByPattern} by pattern)`;
  log(
    `npm-audit-allowlist-bundle-check: vite ${version} built project ${key} (${projectDir}) into ${outDir}: ${mainChunks} chunk(s) + ` +
      `${record.chunks.size - mainChunks} inline worker chunk(s), ${graph.modules} resolved module(s) (${graph.firstParty} first-party under ` +
      `${firstPartyRoot}, ${graph.virtual} virtual, ${graph.packages.size} npm package(s)); output: ${describe(built)}` +
      (shipped ? `; deployment build ${args["verify-dist"]}: ${describe(shipped)}, every script byte-identical to this build's chunks or pinned` : ""),
  );
  if (graph.violations.length > 0) {
    for (const violation of graph.violations) error(`npm-audit-allowlist-bundle-check: VIOLATION ${violation}`);
    throw new GateError(
      `${graph.violations.length} bundled module(s) come from allowlisted package(s); the allowlist premise ` +
        "(build tooling only, never shipped) is false",
    );
  }
  log(`npm-audit-allowlist-bundle-check: PASS; no shipped script resolves code from: ${names.join(", ") || "(allowlist is empty)"}`);
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
