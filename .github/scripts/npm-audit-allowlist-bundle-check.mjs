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
 *   - every chunk's resolved module ids (`generateBundle`), main build and every worker sub-build alike
 *     (`worker.plugins`, one record per sub-build): a module under node_modules/<dir>/ is attributed to
 *     the package NAMED in that directory's package.json (an npm alias installs one package under another
 *     directory name); an allowlisted package anywhere fails; a module that is neither a package, nor
 *     under the first-party root, nor one of Vite's own helper modules, nor a virtual module listed in the
 *     allowlist's bundle.trustedVirtualModules, fails closed;
 *   - every chunk's final bytes (`writeBundle`): the file on disk must still hash the same, whatever its
 *     extension;
 *   - every other file in the output, judged by what a browser would do with it: a script must be a
 *     repository-authored file PINNED by sha256 in bundle.firstPartyScripts that is also a byte-identical,
 *     git-tracked copy from publicDir; an HTML document is tokenised (below); an SVG document may carry
 *     no script, handler or executable URL; text data (css, json, xml, txt, …) and binary media are inert
 *     and tripwire-scanned where textual; anything else (wasm, unknown types) is refused as opaque;
 *   - HTML documents are tokenised conservatively: comments may hold plain text only, declarations other
 *     than <!doctype> and processing instructions are refused, every "<" must start a tag and tag names
 *     end at whitespace, "/" or ">" (no "<script!"), qualified names ("s:script") are refused, start
 *     tags are scanned quote-aware with exact attribute names, raw-text elements (script, style, title,
 *     textarea, noscript, …) run to their real end tag; <iframe>/<frame>/<object>/<embed>/<applet>/<base>
 *     and SVG animation elements are refused, as are srcdoc, inline event handlers, javascript:/vbscript:
 *     URLs and non-image data: URLs anywhere; attribute semantics (type, rel, http-equiv) are read
 *     entity-decoded as the browser does; every URL (script src/href/xlink:href, script preloads, meta
 *     refresh) is resolved with the WHATWG URL parser against the document's own deployment URL and must
 *     stay same-origin under the base without percent-encoding; a <script> with a source must be a known
 *     chunk or pinned copy with an empty body; every inline script of a JavaScript type must match a pin
 *     (sha256 of its normalised text, or an anchored pattern for generated stubs), other types are inert
 *     data blocks, import maps are refused;
 *   - publicDir must lie inside the first-party root; symlinks in the output fail;
 *   - with --verify-dist, the real deployment build's output is judged the same way, so the judged graph
 *     is the shipped artifact.
 * Trust boundary, stated plainly: code authored in this repository (modules under the first-party root,
 * pinned public copies and inline scripts, the Vite configuration and its plugins) is trusted as
 * reviewed; this gate establishes what the npm dependency graph of that build contains. Data blocks and
 * text files are inert unless repository code chooses to execute them, which is that code's review. A
 * copy of one package's code embedded inside another package's published files is attributed to the
 * package that ships it (the same limitation npm audit has), so the gate's claim is about packages as
 * installed, not about code provenance inside them.
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
// XML documents (SVG included) can carry XHTML/SVG script and are judged as documents, never as inert text.
const XML_RE = /\.(?:svg|xml|xsl|xslt|rss|atom|xhtml|xht)$/i;
// Inert data a browser does not run: textual kinds are tripwire-scanned, binary kinds are accepted as is.
const INERT_TEXT_RE = /\.(?:css|json|webmanifest|txt|map|ts|md|vtt|csv)$/i;
// Images, fonts and media carry no script. PDF is deliberately absent: a PDF can embed JavaScript that a
// browser's viewer may run, so it is an opaque payload here until it has a validator of its own.
const INERT_BINARY_RE = /\.(?:png|jpe?g|gif|webp|avif|ico|woff2?|ttf|otf|eot|mp4|webm|mp3|ogg|wav)$/i;
const SHA256_RE = /^[0-9a-f]{64}$/;
const NODE_MODULES = "/node_modules/";
export const WORKER_PREFIX = "worker";
/** Virtual modules Vite itself injects into browser builds; nothing else is trusted implicitly. */
export const BUILTIN_VIRTUAL = new Set(["commonjsHelpers.js", "vite/preload-helper.js", "vite/modulepreload-polyfill.js"]);
const BROWSER_EXTERNAL_RE = /^__vite-browser-external(?::[^\s/]+)?$/;

const toPosix = (path) => path.replace(/\\/g, "/");
export const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const isPlainObject = (value) => value !== null && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype;
const isNonEmptyString = (value) => typeof value === "string" && value.length > 0;
const under = (path, root) => {
  const normalisedRoot = posix.normalize(toPosix(root)).replace(/\/+$/, "");
  const normalisedPath = posix.normalize(toPosix(path));
  return normalisedPath === normalisedRoot || normalisedPath.startsWith(`${normalisedRoot}/`);
};

/** What a browser does with an output file that is not a chunk: "html" | "xml" | "text" | "binary" | null (opaque). */
export function inertKind(fileName) {
  if (HTML_RE.test(fileName)) return "html";
  if (XML_RE.test(fileName)) return "xml";
  if (INERT_TEXT_RE.test(fileName)) return "text";
  if (INERT_BINARY_RE.test(fileName)) return "binary";
  return null;
}

const installedManifests = new Map();
/**
 * The state of a directory's package.json: { present, name } — present false when there is no such file
 * (a vendored segment), present true with name null when it exists but is unreadable or nameless (which an
 * npm installation never is). Cached per directory.
 */
export function readInstalledManifest(dir) {
  if (installedManifests.has(dir)) return installedManifests.get(dir);
  const path = join(dir, "package.json");
  let state;
  if (!existsSync(path)) state = { present: false, name: null };
  else {
    let name = null;
    try {
      const pkg = JSON.parse(readFileSync(path, "utf8"));
      if (pkg !== null && typeof pkg === "object" && isNonEmptyString(pkg.name)) name = pkg.name;
    } catch {
      name = null;
    }
    state = { present: true, name };
  }
  installedManifests.set(dir, state);
  return state;
}
/** The `name` in an installed package directory's package.json, or null. */
export const readInstalledName = (dir) => readInstalledManifest(dir).name;

const installedLocks = new Map();
/**
 * The `packages` map of the lockfile that records an install root's node_modules tree: npm's hidden
 * node_modules/.package-lock.json (the installed tree exactly as npm wrote it), else package-lock.json.
 * null when neither exists, parses, or carries a v2/v3 `packages` map. Cached per install root.
 */
export function readInstalledLock(installRoot) {
  if (installedLocks.has(installRoot)) return installedLocks.get(installRoot);
  const isPlainObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  // Every candidate is validated (a v2/v3 lockfile whose `packages` is a plain object of plain objects) and
  // the valid ones are reconciled into one map: a boundary recorded by either counts, and a boundary the two
  // record under different names is marked as a conflict (no identity). A malformed or incomplete hidden
  // lockfile can therefore neither hide an installation the regular lockfile records nor pass as complete.
  const merged = {};
  let sources = 0;
  for (const candidate of [join(installRoot, "node_modules", ".package-lock.json"), join(installRoot, "package-lock.json")]) {
    let lock;
    try {
      lock = JSON.parse(readFileSync(candidate, "utf8"));
    } catch {
      continue; // not there or not readable
    }
    if (!isPlainObject(lock) || ![2, 3].includes(lock.lockfileVersion) || !isPlainObject(lock.packages)) continue;
    if (!Object.values(lock.packages).every(isPlainObject)) continue;
    sources += 1;
    for (const [key, entry] of Object.entries(lock.packages)) {
      const name = isNonEmptyString(entry.name) ? entry.name : null;
      const seen = merged[key];
      if (seen === undefined) merged[key] = { name };
      else if (seen.name !== name) merged[key] = { conflict: [seen.name, name] };
    }
  }
  const packages = sources === 0 ? null : merged;
  if (packages !== null) installedLocks.set(installRoot, packages); // a missing lockfile is re-checked, never remembered
  return packages;
}

/**
 * Classify one Rollup module id: { kind: "package", name, dir } | { kind: "first-party" } | { kind: "virtual" }
 * | { kind: "unknown", why }. Query strings are stripped and the path normalised; a module under
 * node_modules belongs to the package at npm's installation boundary: the directory right after the LAST
 * "node_modules/" segment ("name" or "@scope/name"). Everything below that directory was installed as that
 * package, so a nested package.json inside the package's own files cannot re-attribute it, while a library
 * vendored under its own node_modules segment (…/dist/esm/node_modules/tslib/) is attributed by that
 * segment. The canonical name is the boundary's package.json name when it has one (an npm alias such as
 * node_modules/matcher holding picomatch), else the directory label; a NUL-prefixed commonjs proxy of a
 * node_modules file counts for its package; dot directories such as .vite are caches, not packages.
 * Virtual ids (NUL prefix, `vite/...`, `__vite-...`, `virtual:...`) are trusted only when they are Vite's
 * own helpers or listed in `trustedVirtual`. Ids under the first-party root (compared as paths) are
 * first-party. Else unknown.
 */
export function classifyModule(id, firstPartyRoot, { trustedVirtual = new Set(), readManifest = readInstalledManifest, readLock = readInstalledLock } = {}) {
  if (typeof id !== "string" || id.length === 0) return { kind: "unknown", why: "empty module id" };
  const nul = id.startsWith("\u0000");
  const bare = toPosix(nul ? id.slice(1) : id);
  const path = posix.normalize(bare.split("?")[0]);
  if (path.includes(NODE_MODULES)) {
    // npm's installation boundaries are what its lockfiles record, so the validated, reconciled lockfiles of
    // the install root (the directory above the first node_modules) decide. Walking the boundaries from the
    // innermost outward: a recorded boundary is an installed package whose identity is the lockfile's `name`
    // (an alias) or its directory, cross-checked against a readable package.json name (a lockfile conflict or a
    // contradicting manifest is no identity); an unrecorded boundary whose package.json has a name is a
    // vendored copy declaring itself: that name is carried as an additional identity the gate checks while the
    // walk continues outward to the recorded installation that ships it, which owns the module (so neither a
    // vendored copy nor a harmlessly named inner directory can displace the shipping package's identity); an
    // unrecorded boundary whose package.json exists but is unreadable or nameless fails closed, because npm
    // never installs such a package and never leaves an installation unrecorded; an unrecorded boundary with no
    // package.json at all is a vendored segment (…/dist/esm/node_modules/tslib/) of the package that ships it.
    // No lockfile, or no recorded installation at any boundary, fails closed; a directory label is never a name.
    const first = path.indexOf(NODE_MODULES);
    const installRoot = path.slice(0, first);
    const lock = readLock(installRoot);
    if (lock === null) return { kind: "unknown", why: `no valid lockfile (node_modules/.package-lock.json or package-lock.json, v2/v3) under ${installRoot} records the installation that ${path} belongs to` };
    // Names declared by unrecorded (vendored) manifests on the way out: additional identities the gate
    // checks, never the owner — the owner is the recorded installation that ships them, so a harmless inner
    // name can no more hide an allowlisted installation than a vendored copy can hide behind its shipper.
    const declared = [];
    let innermost = null;
    let searchEnd = path.length;
    for (;;) {
      const at = path.lastIndexOf(NODE_MODULES, searchEnd - 1);
      if (at === -1) break;
      const start = at + NODE_MODULES.length;
      const segments = path.slice(start).split("/");
      const scoped = segments[0]?.startsWith("@") ?? false;
      const label = scoped ? segments.slice(0, 2).join("/") : segments[0] ?? "";
      const labelSegments = scoped ? 2 : 1;
      const valid = label !== "" && !label.startsWith(".") && !label.includes("/.") && segments.length > labelSegments && !(scoped && !isNonEmptyString(segments[1]));
      if (valid) {
        innermost ??= label;
        const dir = path.slice(0, start) + label;
        const manifest = readManifest(dir);
        const named = isNonEmptyString(manifest.name) && !manifest.name.startsWith(".") ? manifest.name : null;
        const entry = lock[path.slice(first + 1, start + label.length)];
        if (entry !== undefined && entry !== null && typeof entry === "object") {
          if (Array.isArray(entry.conflict)) {
            return { kind: "unknown", why: `${dir} is recorded under different names by the hidden and the regular lockfile (${entry.conflict.map((n) => n ?? label).join(" vs ")})` };
          }
          const identity = isNonEmptyString(entry.name) ? entry.name : label;
          if (named !== null && named !== identity) {
            return { kind: "unknown", why: `${dir} is recorded in the lockfile as ${identity} but its package.json is named ${named}` };
          }
          // Named manifests in the directories between the module and this installation's root are vendored
          // copies declaring themselves inside the installation (…/vendor/picomatch/package.json): further
          // identities to check, while the installation stays the owner.
          for (let ancestor = posix.dirname(path); ancestor.startsWith(`${dir}/`); ancestor = posix.dirname(ancestor)) {
            const inner = readManifest(ancestor).name;
            if (isNonEmptyString(inner) && !inner.startsWith(".") && inner !== identity && !declared.includes(inner)) declared.push(inner);
          }
          return { kind: "package", name: identity, dir: label, ...(declared.length > 0 ? { declares: declared } : {}) };
        }
        if (named !== null) declared.push(named);
        else if (manifest.present) {
          return { kind: "unknown", why: `${dir} is not recorded in a lockfile and its package.json has no readable name; npm records every package it installs, so this is no installation this gate can name (${path})` };
        }
      }
      searchEnd = at;
    }
    if (innermost === null) return { kind: "unknown", why: `no installed package directory after the node_modules in ${path}` };
    if (declared.length > 0) return { kind: "unknown", why: `${innermost} declares itself ${declared.join(", ")} but no lockfile records an installation containing it (${path})` };
    return { kind: "unknown", why: `the directory ${innermost} is not recorded in a lockfile and has no package.json (${path})` };
  }
  if (nul || path.startsWith("vite/") || path.startsWith("__vite-") || path.startsWith("virtual:")) {
    if (BUILTIN_VIRTUAL.has(path) || BROWSER_EXTERNAL_RE.test(path)) return { kind: "virtual" };
    if (trustedVirtual.has(id) || trustedVirtual.has(path) || trustedVirtual.has(`\u0000${path}`)) return { kind: "virtual" };
    return { kind: "unknown", why: "a virtual module that is not one of Vite's helpers and not listed in bundle.trustedVirtualModules" };
  }
  if (under(path, firstPartyRoot)) return { kind: "first-party" };
  return { kind: "unknown", why: `outside the first-party root ${firstPartyRoot}` };
}

/**
 * Judge the resolved graph. `chunks` is Map<key, moduleIds[]> (worker sub-build chunks keyed "workerN:…"),
 * `assets` Map<fileName, type>. Returns { violations, modules, firstParty, virtual, packages: Set }.
 */
export function evaluateGraph({ chunks, assets, allow, firstPartyRoot, trustedVirtual = new Set(), readManifest = readInstalledManifest, readLock = readInstalledLock }) {
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
      const owner = classifyModule(id, firstPartyRoot, { trustedVirtual, readManifest, readLock });
      if (owner.kind === "unknown") {
        throw new GateError(`chunk ${fileName} contains module ${JSON.stringify(id)} whose ownership cannot be established: ${owner.why}`);
      }
      if (owner.kind === "virtual") virtual += 1;
      else if (owner.kind === "first-party") firstParty += 1;
      else {
        packages.add(owner.name);
        const declares = owner.declares ?? [];
        const installedAs = owner.dir !== owner.name ? `, installed as ${owner.dir}` : "";
        if (allowlisted.has(owner.name)) {
          violations.push(`${owner.name} is bundled into ${fileName}: ${id}${owner.dir !== owner.name ? ` (installed as ${owner.dir})` : ""}${declares.length > 0 ? ` (declares itself ${declares.join(", ")})` : ""}`);
        } else {
          // A vendored copy that declares an allowlisted name is that package's code, whoever ships it.
          const hit = declares.find((name) => allowlisted.has(name));
          if (hit !== undefined) violations.push(`${hit} is bundled into ${fileName}: ${id} (a vendored copy declaring itself ${hit}, shipped inside ${owner.name}${installedAs})`);
        }
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
    if (inertKind(fileName) === null) throw new GateError(`emitted asset ${fileName} has no recognised inert type; refusing an opaque payload`);
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

// HTML's ASCII whitespace, the only whitespace that delimits tag names and attributes (U+00A0 and other
// Unicode spaces do not, so they are never accepted where a browser would read a different token).
const WS = "[ \\t\\n\\f\\r]";
const WS_RE = new RegExp(`^${WS}+|${WS}+$`, "g");
const asciiTrim = (text) => text.replace(WS_RE, "");

/** Parse the attributes of one start tag, quote-aware, exact ASCII names; duplicates or leftovers fail closed. */
export function parseAttributes(text, label) {
  const attrs = new Map();
  let rest = text;
  const ATTR = new RegExp(`^${WS}*([a-zA-Z_:][a-zA-Z0-9_:.-]*)(?:${WS}*=${WS}*(?:"([^"]*)"|'([^']*)'|([^ \\t\\n\\f\\r"'=<>\`]+)))?`);
  while (asciiTrim(rest).length > 0) {
    const match = ATTR.exec(rest);
    if (!match) throw new GateError(`${label}: cannot parse attributes near ${JSON.stringify(asciiTrim(rest).slice(0, 40))}`);
    const name = match[1].toLowerCase();
    if (attrs.has(name)) throw new GateError(`${label}: attribute ${name} is repeated`);
    attrs.set(name, match[2] ?? match[3] ?? match[4] ?? null);
    rest = rest.slice(match[0].length);
  }
  return attrs;
}

/**
 * Parse one start tag's attribute text and set the self-closing flag as the tokenizer does: a final "/"
 * self-closes only from the attribute-name or after-quoted-value states. A "/" that ends an unquoted
 * value is value data ("<svg a=b/>" opens an svg element with a="b/"), and "<svg/ >" is not self-closing
 * (the "/" is a parse error the browser drops; here it fails as unparsable).
 */
export function parseStartTag(rawAttrs, label) {
  if (!rawAttrs.endsWith("/")) return { attrs: parseAttributes(rawAttrs, label), selfClosing: false };
  const before = rawAttrs.slice(0, -1);
  // An unquoted value (possibly empty, after "=") running up to the "/" claims it: such a value ends
  // only at ASCII whitespace or ">".
  const unquotedValueTail = new RegExp(`=${WS}*[^ \\t\\n\\f\\r"'=<>\`]*$`).test(before);
  if (unquotedValueTail) return { attrs: parseAttributes(rawAttrs, label), selfClosing: false };
  return { attrs: parseAttributes(before, label), selfClosing: true };
}

// The named character references this gate models, case-sensitive as in the HTML Standard: the common
// escapes plus every name that decodes to ASCII punctuation or whitespace, i.e. everything that can change
// how a URL or a semantic attribute reads. &nbsp; is U+00A0 exactly: a browser keeps it in a URL (percent-
// encoded, so the request differs from the known chunk), and the resolver refuses such paths.
const ENTITIES = {
  amp: "&", AMP: "&", lt: "<", LT: "<", gt: ">", GT: ">", quot: '"', QUOT: '"', apos: "'", nbsp: " ", NonBreakingSpace: " ",
  Tab: "\t", NewLine: "\n", excl: "!", num: "#", dollar: "$", percnt: "%", lpar: "(", rpar: ")", ast: "*", midast: "*", plus: "+",
  comma: ",", period: ".", sol: "/", colon: ":", semi: ";", equals: "=", quest: "?", commat: "@", lsqb: "[", lbrack: "[", bsol: "\\",
  rsqb: "]", rbrack: "]", Hat: "^", lowbar: "_", UnderBar: "_", grave: "`", DiacriticalGrave: "`", lcub: "{", lbrace: "{", verbar: "|",
  vert: "|", VerticalLine: "|", rcub: "}", rbrace: "}",
};
/**
 * Decode the character references in an attribute value. Numeric references and the modelled names decode;
 * a terminated "&name;" this gate does not model is refused rather than left literal, because a browser
 * knows more than two thousand names ("&bsol;" is a backslash) and would read something this scanner did
 * not see. An unterminated unknown name ("&display=swap" in a query string) is literal to a browser too.
 */
// The HTML Standard's legacy names: the only ones a browser also decodes WITHOUT a semicolon. Every other
// name needs its ";" to be a reference at all. The modelled ones decode here; the others (Latin-1 letters
// and symbols) are refused when a browser would decode them, so a source URL is never read differently.
const LEGACY_NAMES = new Set([
  "AElig", "AMP", "Aacute", "Acirc", "Agrave", "Aring", "Atilde", "Auml", "COPY", "Ccedil", "ETH", "Eacute", "Ecirc", "Egrave", "Euml", "GT",
  "Iacute", "Icirc", "Igrave", "Iuml", "LT", "Ntilde", "Oacute", "Ocirc", "Ograve", "Oslash", "Otilde", "Ouml", "QUOT", "REG", "THORN", "Uacute",
  "Ucirc", "Ugrave", "Uuml", "Yacute", "aacute", "acirc", "acute", "aelig", "agrave", "amp", "aring", "atilde", "auml", "brvbar", "ccedil", "cedil",
  "cent", "copy", "curren", "deg", "divide", "eacute", "ecirc", "egrave", "eth", "euml", "frac12", "frac14", "frac34", "gt", "iacute", "icirc", "iexcl",
  "igrave", "iquest", "iuml", "laquo", "lt", "macr", "micro", "middot", "nbsp", "not", "ntilde", "oacute", "ocirc", "ograve", "ordf", "ordm", "oslash",
  "otilde", "ouml", "para", "plusmn", "pound", "quot", "raquo", "reg", "sect", "shy", "sup1", "sup2", "sup3", "szlig", "thorn", "times", "uacute",
  "ucirc", "ugrave", "uml", "uuml", "yacute", "yen", "yuml",
]);
// HTML's numeric-reference remapping of the C1 range (the windows-1252 code points a browser substitutes).
const C1_REMAP = {
  0x80: "€", 0x82: "‚", 0x83: "ƒ", 0x84: "„", 0x85: "…", 0x86: "†", 0x87: "‡", 0x88: "ˆ", 0x89: "‰",
  0x8a: "Š", 0x8b: "‹", 0x8c: "Œ", 0x8e: "Ž", 0x91: "‘", 0x92: "’", 0x93: "“", 0x94: "”", 0x95: "•",
  0x96: "–", 0x97: "—", 0x98: "˜", 0x99: "™", 0x9a: "š", 0x9b: "›", 0x9c: "œ", 0x9e: "ž", 0x9f: "Ÿ",
};
export function decodeEntities(text) {
  return text.replace(/&(#[xX][0-9a-fA-F]+|#[0-9]+|[a-zA-Z][a-zA-Z0-9]*)(;?)/g, (match, entity, semicolon, offset) => {
    if (entity[0] === "#") {
      // HTML's numeric character reference rules: zero, a surrogate, a value above U+10FFFF (or too large to
      // represent) become U+FFFD; 0x80–0x9F map through the windows-1252 table; the rest decode as written.
      const code = entity[1].toLowerCase() === "x" ? parseInt(entity.slice(2), 16) : parseInt(entity.slice(1), 10);
      if (!Number.isFinite(code) || code === 0 || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) return "�";
      return C1_REMAP[code] ?? String.fromCodePoint(code);
    }
    if (semicolon) {
      if (Object.hasOwn(ENTITIES, entity)) return ENTITIES[entity];
      throw new GateError(`unsupported character reference ${match} in an attribute value; refused`);
    }
    // Unterminated: only a legacy name decodes, and inside an attribute value not when "=" or an ASCII
    // alphanumeric follows it ("&amp=x", "&ampx" and "&num" stay literal, as in a browser). A legacy name a
    // browser would decode but this gate does not model ("&copy.js") is refused rather than left literal.
    if (!LEGACY_NAMES.has(entity) || /[=A-Za-z0-9]/.test(text[offset + match.length] ?? "")) return match;
    if (Object.hasOwn(ENTITIES, entity)) return ENTITIES[entity];
    throw new GateError(`unsupported character reference ${match} in an attribute value (a browser decodes this legacy name); refused`);
  });
}
const normaliseUrl = (value) => decodeEntities(value).replace(/[\u0000- \u007f]+/g, "").toLowerCase();
// Raw-text elements: their content is never markup (noscript, noembed and noframes included, as a browser
// with scripting enabled reads them), so the only thing to find is the matching end tag.
const RAW_TEXT = new Set(["script", "style", "textarea", "title", "xmp", "noscript", "noembed", "noframes"]);
const REFUSED_TAGS = new Set(["iframe", "frame", "frameset", "object", "embed", "applet", "base", "portal", "fencedframe"]);
// SVG animation can rewrite an attribute (an anchor's href) into something executable after the fact.
const SVG_ANIMATION = new Set(["animate", "set", "animatemotion", "animatetransform", "animatecolor", "discard"]);
// Start tags that make a browser leave foreign content (HTML Standard, "in foreign content": the svg
// element is popped and the tag is parsed as HTML). <font> breaks out only with color/face/size; refused whole.
const FOREIGN_BREAKOUT = new Set([
  "b", "big", "blockquote", "body", "br", "center", "code", "dd", "div", "dl", "dt", "em", "embed", "font", "h1", "h2", "h3", "h4", "h5", "h6",
  "head", "hr", "i", "img", "li", "listing", "menu", "meta", "nobr", "ol", "p", "pre", "ruby", "s", "small", "span", "strong", "strike", "sub",
  "sup", "table", "tt", "u", "ul", "var",
]);
const UTF8_FATAL = new TextDecoder("utf-8", { fatal: true });
// Every JavaScript MIME type essence a browser executes (HTML Standard), plus "module" and the empty type.
const JS_MIME = new Set([
  "", "module", "application/ecmascript", "application/javascript", "application/x-ecmascript", "application/x-javascript", "text/ecmascript",
  "text/javascript", "text/javascript1.0", "text/javascript1.1", "text/javascript1.2", "text/javascript1.3", "text/javascript1.4", "text/javascript1.5",
  "text/jscript", "text/livescript", "text/x-ecmascript", "text/x-javascript",
]);
const SCRIPT_SOURCE_ATTRS = ["src", "href", "xlink:href"];
const DOCUMENT_ORIGIN = "https://document.invalid";

/** An attribute value as the browser reads it for semantics: entity-decoded, ASCII-trimmed, lower-cased. */
const semantic = (attrs, name) => asciiTrim(decodeEntities(attrs.get(name) ?? "")).toLowerCase();

/**
 * A browser's first pass over an (already entity-decoded, exactly once) URL value: drop tabs and newlines,
 * trim leading and trailing C0 controls and spaces (DEL is not trimmed: a browser keeps and percent-encodes
 * it). Anything odd left over (inner whitespace, controls, DEL, a backslash) fails. Callers decode the
 * attribute value once, as the tokenizer does; nothing decodes twice.
 */
export function cleanUrl(raw, label) {
  const value = raw.replace(/[\t\n\r]/g, "").replace(/^[\u0000- ]+|[\u0000- ]+$/g, "");
  if (/[\u0000- \u007f\\]/.test(value)) throw new GateError(`${label}: URL ${JSON.stringify(raw)} contains whitespace, control characters or a backslash; refused`);
  return value;
}

/**
 * The URL of a <meta http-equiv="refresh"> content (already entity-decoded once), read as the HTML Standard's
 * declarative refresh steps read it: "<seconds>; url=<url>", where a URL that opens with a quote ends at the
 * matching quote ("url='https://attacker.example'@document.invalid/" refreshes to attacker.example). Forms
 * the browser also accepts but this gate does not model (no "url=", a comma, a fraction, an unterminated
 * quote, text after the closing quote) are refused.
 */
export function refreshUrl(content, label) {
  const match = /^[ \t\n\f\r]*[0-9]+[ \t\n\f\r]*;[ \t\n\f\r]*url[ \t\n\f\r]*=[ \t\n\f\r]*([\s\S]*)$/i.exec(content);
  if (!match) throw new GateError(`${label}: <meta http-equiv="refresh"> without a "<seconds>; url=…" content is refused`);
  const rest = match[1];
  const quote = rest[0] === '"' || rest[0] === "'" ? rest[0] : null;
  if (quote === null) return rest;
  const end = rest.indexOf(quote, 1);
  if (end === -1) throw new GateError(`${label}: <meta http-equiv="refresh"> url with an unterminated quote; refused`);
  if (!/^[ \t\n\f\r]*$/.test(rest.slice(end + 1))) throw new GateError(`${label}: <meta http-equiv="refresh"> url followed by text after its closing quote; refused`);
  return rest.slice(1, end);
}

/**
 * Resolve a URL the way the browser would, against the document's own deployment URL (a fixed origin,
 * the base path, the document's path). Returns { external: true } for another origin, or the
 * output-relative path for a same-origin location under the base; anything percent-encoded or outside
 * the base fails.
 */
export function resolveLocal(raw, rel, prefix, label) {
  const value = cleanUrl(raw, label);
  // The deployment origin is not known here (the base below is a placeholder), so any URL that names a
  // host or a scheme — absolute or protocol-relative — is external by construction; a local reference is
  // a path, and the placeholder host can never be matched as if it were the site.
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(value) || value.startsWith("//")) return { external: true };
  const base = new URL(`${DOCUMENT_ORIGIN}${prefix}${rel}`);
  let url;
  try {
    url = new URL(value, base);
  } catch {
    throw new GateError(`${label}: URL ${JSON.stringify(raw)} does not parse; refused`);
  }
  if (url.origin !== base.origin) return { external: true };
  if (url.pathname.includes("%")) throw new GateError(`${label}: URL ${JSON.stringify(raw)} resolves to a percent-encoded local path ${url.pathname}; refused`);
  if (!url.pathname.startsWith(prefix)) throw new GateError(`${label}: URL ${JSON.stringify(raw)} resolves to ${url.pathname}, outside the base ${prefix}; refused`);
  return { external: false, target: url.pathname.slice(prefix.length) };
}

/** Refuse attributes a browser could execute: handlers, srcdoc, executable or non-image data URL schemes. */
export function refuseAttributes(name, attrs, label) {
  for (const [attr, raw] of attrs) {
    if (attr.startsWith("on")) throw new GateError(`${label}: <${name} ${attr}> is an inline event handler; refused`);
    if (attr === "srcdoc") throw new GateError(`${label}: <${name} srcdoc> embeds a document; refused`);
    // XML Base re-roots every relative URL below the element (an SVG <script xlink:href="/known.js"> would
    // then be fetched from another origin), while this scanner resolves against the document; refused.
    if (attr === "xml:base") throw new GateError(`${label}: <${name} xml:base> changes how URLs resolve; refused`);
    if (raw === null) continue;
    const value = normaliseUrl(raw);
    if (/^(?:javascript|vbscript|livescript|mocha):/.test(value)) throw new GateError(`${label}: <${name} ${attr}> uses an executable URL scheme; refused`);
    if (value.startsWith("data:") && !value.startsWith("data:image/")) throw new GateError(`${label}: <${name} ${attr}> is a data: URL that is not an image; refused`);
  }
}

/**
 * Decode a document's bytes the only way this gate can reason about them: valid UTF-8 with no C0 control
 * character other than tab, LF, FF and CR. ESC (which ISO-2022-JP uses to re-shape ASCII into other
 * characters) and a UTF-16 byte order mark both fail, so no encoding switch can make a browser read
 * these bytes as different markup; scanDocument() additionally refuses any non-UTF-8 charset declaration.
 */
export function decodeDocument(bytes, label) {
  let text;
  try {
    text = UTF8_FATAL.decode(bytes);
  } catch {
    throw new GateError(`${label}: not valid UTF-8; refused`);
  }
  const control = /[\u0000-\u0008\u000b\u000e-\u001f]/.exec(text);
  if (control) throw new GateError(`${label}: contains the control character U+${control[0].codePointAt(0).toString(16).toUpperCase().padStart(4, "0")}; refused`);
  return text;
}

/** The charset a <meta http-equiv="content-type"> content declares, lower-cased, or null when none. */
const charsetOf = (content) => {
  const match = /charset[ \t\n\f\r]*=[ \t\n\f\r]*["']?([^"'; \t\n\f\r]*)/i.exec(content);
  return match ? match[1].toLowerCase() : null;
};

/**
 * Tokenise an HTML document (or, with xml: true, an XML document such as SVG) conservatively. Returns
 * { scripts: [{ attrs, body, foreign }], preloads: string[], refreshes: string[] }, where `foreign` marks
 * an SVG or XML script element (its type comes from `type` alone). Refuses: comments holding
 * markup, any doctype other than the plain "<!doctype html>" (no doctype at all in XML), processing
 * instructions other than the XML declaration in XML, CDATA, a "<" that does not start a tag, non-ASCII
 * tag delimiters, unterminated tags, self-closing raw-text elements, <math>, refused tags, <link
 * rel="import">, <meta http-equiv="refresh"> without a url, and every attribute refuseAttributes()
 * rejects. Raw-text elements apply only in HTML outside <svg>: in XML every "<" starts markup, and inside
 * <svg> a <style> or <textarea> is a foreign element whose children are tags, so there is no raw text and
 * every tag is a tag. The HTML integration points inside <svg> (<title>, <desc>; <foreignObject> is refused
 * anyway) switch the browser back to HTML parsing, where a raw-text element could swallow a "</svg>" this
 * open-element stack would honour; they are refused, as are the start and end tags that make a browser
 * leave foreign content, and any end tag inside <svg> that does not close the innermost open element (a
 * browser pops the svg element for an HTML ancestor's end tag). <select> is refused because a browser ignores raw-text
 * start tags inside it (so "<select><style><script>" runs the script), and a "<!--" inside a <script>
 * body is refused because the script-data escaped states let the script run on past the first "</script>".
 */
export function scanDocument(text, label, { xml = false } = {}) {
  const out = { scripts: [], preloads: [], refreshes: [] };
  // No case-folded copy of the document is ever indexed with offsets from the original: Unicode lowercasing
  // changes string length (U+0130 becomes two code units), which would misplace every marker after it.
  // Punctuation markers are compared exactly; keywords through case-insensitive regexes on slices.
  const asciiWs = /[ \t\n\f\r]/;
  // Open elements since the outermost <svg> in HTML (foreign content). A browser pops the svg element for an
  // HTML ancestor's end tag ("<div><svg></div>"), so inside <svg> every end tag must close the innermost
  // open element and anything misnested is refused; the stack is empty when the scanner is in HTML content.
  const foreign = [];
  let i = 0;
  while (i < text.length) {
    const lt = text.indexOf("<", i);
    if (lt === -1) break;
    if (text.startsWith("<!--", lt)) {
      const end = text.indexOf("-->", lt + 4);
      if (end === -1) throw new GateError(`${label}: unterminated comment`);
      if (/[<>]/.test(text.slice(lt + 4, end))) throw new GateError(`${label}: a comment contains markup; comments may hold plain text only`);
      i = end + 3;
      continue;
    }
    if (text.startsWith("<![CDATA[", lt)) throw new GateError(`${label}: CDATA sections are refused`);
    if (text[lt + 1] === "!") {
      // Only the plain HTML doctype: a quoted public or system identifier could hide ">" and markup.
      const doctype = /^<!doctype[ \t\n\f\r]+html[ \t\n\f\r]*>/i.exec(text.slice(lt, lt + 40));
      if (xml || !doctype) throw new GateError(`${label}: unsupported declaration at offset ${lt}; only "<!doctype html>" is accepted in HTML`);
      i = lt + doctype[0].length;
      continue;
    }
    if (text[lt + 1] === "?") {
      // Only the XML declaration itself, in XML mode; "<?xml-stylesheet …?>" and the like are refused.
      if (!xml || !text.startsWith("<?xml", lt) || !asciiWs.test(text[lt + 5] ?? "")) throw new GateError(`${label}: processing instructions are refused`);
      const end = text.indexOf("?>", lt);
      if (end === -1) throw new GateError(`${label}: unterminated XML prolog`);
      // The bytes were read as UTF-8; the declaration may not claim another encoding.
      const encoding = /encoding[ \t\n\f\r]*=[ \t\n\f\r]*(?:"([^"]*)"|'([^']*)')/i.exec(text.slice(lt, end));
      if (encoding && !/^utf-?8$/i.test(encoding[1] ?? encoding[2] ?? "")) {
        throw new GateError(`${label}: XML declaration encoding ${JSON.stringify(encoding[1] ?? encoding[2])}; only UTF-8 is accepted`);
      }
      i = end + 2;
      continue;
    }
    if (text[lt + 1] === "/") {
      const endTag = /^<\/([a-zA-Z][a-zA-Z0-9:_-]*)[ \t\n\f\r]*>/.exec(text.slice(lt, lt + 80));
      if (!endTag) throw new GateError(`${label}: unsupported end tag at offset ${lt}`);
      const endName = endTag[1].toLowerCase();
      if (!xml && foreign.length > 0) {
        // "</p>" and "</br>" are the two end tags that make a browser leave foreign content outright.
        if (endName === "p" || endName === "br") {
          throw new GateError(`${label}: </${endName}> inside <svg> makes a browser leave the SVG context (foreign-content breakout); refused`);
        }
        // Any other end tag must close the innermost open element: a browser processes an end tag for an
        // HTML ancestor ("<div><svg></div>") by popping the svg element as well, which this stack would
        // otherwise not follow, and an unmatched end tag inside <svg> is misnested markup either way.
        if (endName !== foreign.at(-1)) {
          throw new GateError(`${label}: </${endName}> inside <svg> does not close the innermost open <${foreign.at(-1)}>; misnested markup is refused`);
        }
        foreign.pop();
      }
      i = lt + endTag[0].length;
      continue;
    }
    const nameMatch = /^<([a-zA-Z][a-zA-Z0-9:_-]*)/.exec(text.slice(lt, lt + 80));
    if (!nameMatch) throw new GateError(`${label}: "<" at offset ${lt} does not start a tag; text must be escaped as &lt;`);
    const name = nameMatch[1].toLowerCase();
    // A browser reads the tag name up to ASCII whitespace, "/" or ">": "<script!" and "<style " are
    // unknown elements, not a script or style with an attribute. Anything else after the name is refused.
    const delimiter = text[lt + 1 + nameMatch[1].length];
    if (delimiter !== undefined && !/[ \t\n\f\r/>]/.test(delimiter)) {
      throw new GateError(`${label}: unsupported tag name at offset ${lt} ("<${nameMatch[1]}${JSON.stringify(delimiter)}"); refused`);
    }
    // Namespace-qualified element names (an SVG "s:script") and MathML are refused in both modes.
    if (name.includes(":")) throw new GateError(`${label}: qualified element name <${name}> is refused`);
    if (name === "math") throw new GateError(`${label}: <math> is refused (MathML parsing context)`);
    let j = lt + 1 + nameMatch[1].length;
    let quote = null;
    while (j < text.length) {
      const ch = text[j];
      if (quote) {
        if (ch === quote) quote = null;
      } else if (ch === '"' || ch === "'") quote = ch;
      else if (ch === ">") break;
      j += 1;
    }
    if (j >= text.length) throw new GateError(`${label}: unterminated <${name}> tag`);
    const rawAttrs = text.slice(lt + 1 + nameMatch[1].length, j);
    const { attrs, selfClosing } = parseStartTag(rawAttrs, `${label}: <${name}>`);
    refuseAttributes(name, attrs, label);
    if (REFUSED_TAGS.has(name)) throw new GateError(`${label}: <${name}> is refused (an embedded or external executable context)`);
    if (name === "foreignobject") throw new GateError(`${label}: <foreignObject> embeds HTML inside SVG; refused`);
    // In the "in select" insertion mode a browser ignores <style>, <title>, <xmp>, <noscript>, <noembed> and
    // <noframes> start tags but still runs <script>, so "<select><style><script>" is not raw text to it.
    if (name === "select") throw new GateError(`${label}: <select> is refused (a parsing context in which raw-text start tags are ignored)`);
    // <template> content has its own insertion modes ("<template><col>" enters "in column group", where a
    // <style> start tag is ignored) and "</template>" leaves them, so a script after it can run while this
    // scanner still believes it is inside raw text. Not modelled; refused. No shipped page uses it.
    if (name === "template") throw new GateError(`${label}: <template> is refused (its insertion modes are not modelled)`);
    // Inside <svg> in HTML, these start tags make the browser pop the svg element and parse HTML again
    // (foreign-content breakout), which this depth counter cannot follow; refused.
    if (!xml && foreign.length > 0 && FOREIGN_BREAKOUT.has(name)) {
      throw new GateError(`${label}: <${name}> inside <svg> makes a browser leave the SVG context (foreign-content breakout); refused`);
    }
    if (SVG_ANIMATION.has(name)) throw new GateError(`${label}: SVG animation element <${name}> can rewrite attributes after validation; refused`);
    if (name === "use") {
      // <use> clones the referenced markup, event handlers included, into this document: only a fragment of
      // the document itself may be referenced, never another resource (judged or not). In XML, attribute
      // names are case-sensitive and XLink is identified by namespace rather than by the literal "xlink:"
      // prefix, so a lower-cased reading could validate a decoy while the browser follows another attribute;
      // <use> is refused outright there (no shipped XML document carries one).
      if (xml) throw new GateError(`${label}: <use> is refused in an XML document (its reference cannot be read without namespace resolution)`);
      const refs = ["href", "xlink:href"].filter((attr) => attrs.has(attr));
      if (refs.length !== 1) throw new GateError(`${label}: <use> must carry exactly one href or xlink:href; refused`);
      const ref = asciiTrim(decodeEntities(attrs.get(refs[0]) ?? ""));
      if (!/^#[^#\s]+$/.test(ref)) throw new GateError(`${label}: <use ${refs[0]}=${JSON.stringify(ref)}> references another resource; only a same-document fragment is allowed`);
    }
    // Raw-text rules are an HTML-parser notion: not in XML, and not inside <svg>, where a <style> or a
    // <script> is an SVG element whose children are tags. There, nothing is raw and every tag is seen.
    const rawTextHere = RAW_TEXT.has(name) && !xml && foreign.length === 0;
    // <title> and <desc> inside <svg> are HTML integration points: the browser parses their children as
    // HTML again, so a raw-text element in there could hide a "</svg>" that this stack would honour.
    if (!xml && foreign.length > 0 && (name === "title" || name === "desc")) {
      throw new GateError(`${label}: <${name}> inside <svg> is an HTML integration point and is refused`);
    }
    // Enter or extend foreign content: <svg> opens it, and every non-self-closing element inside it is an
    // open element its own end tag must close. A foreign <script> is consumed with its end tag below.
    if (!xml && !selfClosing && name !== "script" && (name === "svg" || foreign.length > 0)) foreign.push(name);
    if (name === "link") {
      const rel = semantic(attrs, "rel").split(/\s+/);
      if (rel.includes("import")) throw new GateError(`${label}: <link rel="import"> is refused`);
      if (rel.includes("modulepreload") || (rel.includes("preload") && semantic(attrs, "as") === "script")) {
        const href = attrs.get("href");
        if (!isNonEmptyString(href)) throw new GateError(`${label}: a script preload without href`);
        out.preloads.push(decodeEntities(href)); // decoded exactly once, as the tokenizer's attribute value
      }
    }
    if (name === "meta") {
      // The bytes were read as UTF-8; a declaration of any other encoding would let a browser read them as
      // different markup (ISO-2022-JP re-shapes ASCII after an ESC). Only UTF-8 may be declared.
      const declared = attrs.has("charset")
        ? semantic(attrs, "charset")
        : semantic(attrs, "http-equiv") === "content-type" ? charsetOf(decodeEntities(attrs.get("content") ?? "")) : null;
      if (declared !== null && declared !== "utf-8" && declared !== "utf8") {
        throw new GateError(`${label}: <meta> declares the charset ${JSON.stringify(declared)}; only UTF-8 is accepted`);
      }
    }
    if (name === "meta" && semantic(attrs, "http-equiv") === "refresh") {
      // The content is decoded exactly once (the tokenizer's attribute value), then read as the browser's
      // declarative refresh steps read it; resolveLocal() never decodes again.
      out.refreshes.push(refreshUrl(decodeEntities(attrs.get("content") ?? ""), label));
    }
    i = j + 1;
    if (name === "script" && !rawTextHere) {
      // An SVG or XML script element: its content is markup-free text in practice, but it is a script.
      // `foreign` tells checkDocuments() that the browser decides its type from `type` alone.
      if (selfClosing) {
        out.scripts.push({ attrs, body: "", foreign: true });
        continue;
      }
      const match = /<\/script[ \t\n\f\r]*>/i.exec(text.slice(i));
      if (!match) throw new GateError(`${label}: <script> without </script>`);
      const body = text.slice(i, i + match.index);
      if (body.includes("<")) throw new GateError(`${label}: markup inside an SVG/XML <script> is refused`);
      out.scripts.push({ attrs, body, foreign: true });
      i = i + match.index + match[0].length;
      continue;
    }
    if (rawTextHere) {
      if (selfClosing) throw new GateError(`${label}: a self-closing <${name}/> is not how browsers read it; refused`);
      // The end tag must be the real one: "</stylex>" or "</style >" does not close <style>.
      const match = new RegExp(`</${name}(?=[ \\t\\n\\f\\r/>])`, "i").exec(text.slice(i));
      if (!match) throw new GateError(`${label}: <${name}> without </${name}>`);
      const close = i + match.index;
      if (name === "script") {
        const body = text.slice(i, close);
        // "<!--" switches the browser's tokeniser to the script-data escaped states, in which a "</script>"
        // that follows a nested "<script" does not end the script; the browser would run on past this point.
        if (body.includes("<!--")) throw new GateError(`${label}: a script data escape sequence ("<!--") inside <script> is refused`);
        out.scripts.push({ attrs, body, foreign: false });
      }
      if (name === "noscript") {
        // With scripting enabled the body is raw text; with scripting disabled a browser parses it as markup
        // and honours a <meta refresh> or a preload inside it. Judge the body under that model as well: its
        // refreshes and preloads count, its attributes are checked, and its scripts cannot run either way.
        const body = text.slice(i, close);
        if (body.includes("<")) {
          const inner = scanDocument(body, `${label} <noscript>`, { xml });
          out.preloads.push(...inner.preloads);
          out.refreshes.push(...inner.refreshes);
        }
      }
      i = close; // the end tag is consumed on the next iteration
    }
  }
  return out;
}

/**
 * Judge the HTML documents among `files`: tokenised by scanDocument(), external or unknown script sources
 * fail, inline bodies must match a pin, data blocks are inert, refresh targets stay under the base.
 * Returns { documents, inline, data, pinnedBySha256, pinnedByPattern }.
 */
export function checkDocuments({ dir, files, known, base, trust, packages = [], label = "" }) {
  const prefix = base.endsWith("/") ? base : `${base}/`;
  const tag = (rel) => `${label}${rel}`;
  const result = { documents: 0, inline: 0, data: 0, pinnedBySha256: 0, pinnedByPattern: 0 };
  for (const rel of files) {
    if (!HTML_RE.test(rel)) continue;
    result.documents += 1;
    const doc = scanDocument(decodeDocument(readFileSync(join(dir, rel)), tag(rel)), tag(rel));
    for (const { attrs, body, foreign } of doc.scripts) {
      // The type the browser acts on (HTML Standard "prepare the script element"): `type` when present
      // (even empty); else `language` as text/<language> when it is non-empty; else JavaScript. An SVG
      // script element (inline <svg> in HTML) has no `language` attribute: browsers read `type` alone.
      const language = !foreign && attrs.has("language") ? semantic(attrs, "language") : "";
      const type = attrs.has("type") ? semantic(attrs, "type") : language !== "" ? `text/${language}` : "";
      if (type === "importmap") throw new GateError(`${tag(rel)}: import maps are refused`);
      // Speculation rules are not inert data: a browser prefetches or prerenders the pages they list.
      if (type === "speculationrules") throw new GateError(`${tag(rel)}: speculation rules are refused (an active browser feature, not inert data)`);
      const executable = JS_MIME.has(type);
      const sources = SCRIPT_SOURCE_ATTRS.filter((attr) => attrs.has(attr));
      if (sources.length > 1) throw new GateError(`${tag(rel)}: a <script> with several source attributes (${sources.join(", ")}) is refused`);
      if (sources.length === 1) {
        if (!executable) throw new GateError(`${tag(rel)}: a <script ${sources[0]}> with a non-script type is refused`);
        if (body.trim() !== "") throw new GateError(`${tag(rel)}: a <script ${sources[0]}> must have an empty body`);
        const url = attrs.get(sources[0]);
        if (!isNonEmptyString(url)) throw new GateError(`${tag(rel)}: <script> with an empty ${sources[0]}`);
        const resolved = resolveLocal(decodeEntities(url), rel, prefix, tag(rel)); // decoded exactly once
        if (resolved.external) throw new GateError(`${tag(rel)} loads an external script ${url}; it is outside the module graph`);
        if (!known.has(resolved.target)) throw new GateError(`${tag(rel)} loads ${url}, which is not a chunk of this build nor a pinned public copy`);
        continue;
      }
      const normalised = normalizeInline(body);
      if (normalised.length === 0) continue;
      if (!executable) {
        result.data += 1;
        tripwire(`data block ${result.data} of ${tag(rel)}`, normalised, packages);
        continue;
      }
      result.inline += 1;
      tripwire(`inline script ${result.inline} of ${tag(rel)}`, normalised, packages);
      const digest = sha256(normalised);
      if (trust.inline.some((pin) => pin.sha256 === digest)) result.pinnedBySha256 += 1;
      else if (trust.inline.some((pin) => pin.pattern instanceof RegExp && pin.pattern.test(normalised))) result.pinnedByPattern += 1;
      else throw new GateError(`${tag(rel)}: inline script ${result.inline} is not pinned in bundle.firstPartyScripts (sha256 of its normalised body: ${digest})`);
    }
    for (const href of doc.preloads) {
      const resolved = resolveLocal(href, rel, prefix, tag(rel));
      if (resolved.external) throw new GateError(`${tag(rel)} preloads an external script ${href}; refused`);
      if (!known.has(resolved.target)) throw new GateError(`${tag(rel)} preloads ${href}, which is not a chunk of this build nor a pinned public copy`);
    }
    for (const url of doc.refreshes) {
      // Resolved like the browser (dot segments and %2e collapse), it must stay a same-origin path under the base.
      const resolved = resolveLocal(url, rel, prefix, `${tag(rel)} refresh`);
      if (resolved.external) throw new GateError(`${tag(rel)} refreshes to ${url}, outside this site; refused`);
    }
  }
  return result;
}

/**
 * Judge every file written to `dir`: a chunk of this build (same bytes, any extension), a pinned and
 * git-tracked public script copy, an HTML document (judged by checkDocuments), an SVG document without
 * script, inert text (tripwire-scanned) or inert binary data. Anything else is an opaque payload.
 * Returns { files, scripts, publicCopies, documents, inline, data, pinnedBySha256, pinnedByPattern }.
 */
export function judgeOutput({ dir, chunkHashes, trust, publicDir, base, packages = [], isTracked = gitTracked, label = "" }) {
  const files = walkOutput(dir);
  const known = new Set();
  const publicCopies = [];
  for (const rel of files) {
    const written = () => readFileSync(join(dir, rel));
    if (chunkHashes.has(rel)) {
      if (chunkHashes.get(rel) !== sha256(written())) throw new GateError(`${label}${rel} differs from the chunk Rollup wrote; it was rewritten after the build`);
      known.add(rel);
      continue;
    }
    if (SCRIPT_RE.test(rel)) {
      const bytes = written();
      const digest = sha256(bytes);
      const pin = trust.publicScripts.get(rel);
      if (pin === undefined) {
        throw new GateError(`${label}${rel} is a script that is not a chunk of this build and not pinned in bundle.firstPartyScripts (sha256 ${digest})`);
      }
      if (pin !== digest) throw new GateError(`${label}${rel} does not match its pin (sha256 ${digest}); review the change and re-pin it`);
      const copy = publicDir ? join(publicDir, rel) : null;
      if (copy === null || !existsSync(copy) || !lstatSync(copy).isFile() || !readFileSync(copy).equals(bytes)) {
        throw new GateError(`${label}${rel} is pinned but is not a byte-identical copy of ${copy ?? "(no publicDir)"}`);
      }
      if (!isTracked(copy)) throw new GateError(`${label}${rel} comes from ${copy}, which git does not track; a repository-authored static asset must be version-controlled`);
      tripwire(`public copy ${label}${rel}`, bytes.toString("utf8"), packages);
      publicCopies.push(rel);
      known.add(rel);
      continue;
    }
    const kind = inertKind(rel);
    if (kind === "html") continue; // judged below, once every script is known
    if (kind === "xml") {
      // SVG, XHTML, sitemaps, feeds: an XML document a browser may render natively; it may carry no script at all.
      const text = decodeDocument(written(), `${label}${rel}`);
      const doc = scanDocument(text, `${label}${rel}`, { xml: true });
      if (doc.scripts.length > 0) throw new GateError(`${label}${rel} is an XML document carrying a <script>; refused`);
      // An XHTML document can also navigate or preload on its own; nothing in this output has a reason to.
      if (doc.refreshes.length > 0) throw new GateError(`${label}${rel} is an XML document with a <meta http-equiv="refresh">; refused`);
      if (doc.preloads.length > 0) throw new GateError(`${label}${rel} is an XML document with a script preload; refused`);
      tripwire(`${label}${rel}`, text, packages);
      continue;
    }
    if (kind === "text") {
      tripwire(`${label}${rel}`, written().toString("utf8"), packages);
      continue;
    }
    if (kind === "binary") continue;
    throw new GateError(`${label}${rel} is neither a chunk of this build nor of a recognised inert type; refusing an opaque payload`);
  }
  const documents = checkDocuments({ dir, files, known, base, trust, packages, label });
  return { files: files.length, scripts: known.size, publicCopies, ...documents };
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
 * Run the project's Vite build into `outDir` with the recording plugin, one fresh recorder per worker
 * sub-build. Returns { chunks: Map<key, moduleIds[]>, assets: Map<fileName, type>, hashes: Map<fileName,
 * sha256 of the final chunk code>, workers: number, config: { root, base, publicDir } }; worker chunks are
 * keyed "worker<N>:<fileName>" so same-named outputs of different sub-builds never collide.
 */
export async function buildGraph({ projectDir, outDir, vite }) {
  const record = { chunks: new Map(), assets: new Map(), hashes: new Map(), workers: 0, config: null };
  const recorder = (prefix) => ({
    name: `npm-audit-allowlist:module-graph${prefix ? `:${prefix}` : ""}`,
    enforce: "post",
    configResolved(config) {
      if (!prefix) record.config = { root: config.root, base: config.base, publicDir: config.publicDir || null };
    },
    generateBundle(_options, bundle) {
      for (const [fileName, output] of Object.entries(bundle)) {
        const key = prefix ? `${prefix}:${fileName}` : fileName;
        if (output.type === "chunk") {
          if (record.chunks.has(key)) throw new GateError(`chunk ${key} was recorded twice; cannot judge an ambiguous graph`);
          record.chunks.set(key, output.moduleIds ?? Object.keys(output.modules ?? {}));
        } else if (!prefix) record.assets.set(fileName, output.type);
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
      worker: {
        plugins: () => {
          record.workers += 1;
          return [recorder(`${WORKER_PREFIX}${record.workers}`)];
        },
      },
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
  const judge = (dir, label) =>
    judgeOutput({ dir, chunkHashes: record.hashes, trust, publicDir: record.config.publicDir, base: record.config.base, packages: names, label });
  const built = judge(outDir, "");
  let shipped = null;
  if (args["verify-dist"]) {
    const dist = resolve(args["verify-dist"]);
    if (!existsSync(dist)) throw new GateError(`--verify-dist ${dist} does not exist; run the deployment build first`);
    shipped = judge(dist, `${dist}${sep}`);
    if (shipped.scripts === 0) throw new GateError(`--verify-dist ${dist} contains no script; this is not the deployment build's output`);
  }

  const describe = (result) =>
    `${result.files} file(s): ${result.scripts} script(s) (${result.publicCopies.length} pinned public cop${result.publicCopies.length === 1 ? "y" : "ies"}` +
    `${result.publicCopies.length ? `: ${result.publicCopies.join(", ")}` : ""}), ${result.documents} document(s) with ${result.inline} inline ` +
    `script(s) (${result.pinnedBySha256} pinned by sha256, ${result.pinnedByPattern} by pattern) and ${result.data} data block(s)`;
  log(
    `npm-audit-allowlist-bundle-check: vite ${version} built project ${key} (${projectDir}) into ${outDir}: ${record.hashes.size} chunk(s) + ` +
      `${record.chunks.size - record.hashes.size} inline worker chunk(s) from ${record.workers} worker build(s), ${graph.modules} resolved module(s) ` +
      `(${graph.firstParty} first-party under ${firstPartyRoot}, ${graph.virtual} virtual, ${graph.packages.size} npm package(s)); output: ${describe(built)}` +
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
