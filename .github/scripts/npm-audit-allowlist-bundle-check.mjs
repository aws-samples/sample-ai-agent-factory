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
const SVG_RE = /\.svg$/i;
// Inert data a browser does not run: textual kinds are tripwire-scanned, binary kinds are accepted as is.
const INERT_TEXT_RE = /\.(?:css|json|xml|webmanifest|txt|map|ts|md|vtt|csv)$/i;
const INERT_BINARY_RE = /\.(?:png|jpe?g|gif|webp|avif|ico|woff2?|ttf|otf|eot|pdf|mp4|webm|mp3|ogg|wav)$/i;
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

/** What a browser does with an output file that is not a chunk: "html" | "svg" | "text" | "binary" | null (opaque). */
export function inertKind(fileName) {
  if (HTML_RE.test(fileName)) return "html";
  if (SVG_RE.test(fileName)) return "svg";
  if (INERT_TEXT_RE.test(fileName)) return "text";
  if (INERT_BINARY_RE.test(fileName)) return "binary";
  return null;
}

const installedNames = new Map();
/** The `name` in an installed package directory's package.json, or null; cached. */
export function readInstalledName(dir) {
  if (installedNames.has(dir)) return installedNames.get(dir);
  let name = null;
  try {
    const pkg = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
    if (isNonEmptyString(pkg.name)) name = pkg.name;
  } catch {
    name = null;
  }
  installedNames.set(dir, name);
  return name;
}

/**
 * Classify one Rollup module id: { kind: "package", name, dir } | { kind: "first-party" } | { kind: "virtual" }
 * | { kind: "unknown", why }. Query strings are stripped and the path normalised; a module under
 * node_modules belongs to the nearest ancestor directory with a named package.json (nested, vendored and
 * pnpm layouts; a NUL-prefixed commonjs proxy of a node_modules file counts for its package; dot
 * directories such as .vite are caches, not packages), so the package's canonical name comes from its
 * own package.json, not from the directory label. Virtual ids (NUL prefix,
 * `vite/...`, `__vite-...`, `virtual:...`) are trusted only when they are Vite's own helpers or listed in
 * `trustedVirtual`. Ids under the first-party root (compared as paths) are first-party. Else unknown.
 */
export function classifyModule(id, firstPartyRoot, { trustedVirtual = new Set(), readName = readInstalledName } = {}) {
  if (typeof id !== "string" || id.length === 0) return { kind: "unknown", why: "empty module id" };
  const nul = id.startsWith("\u0000");
  const bare = toPosix(nul ? id.slice(1) : id);
  const path = posix.normalize(bare.split("?")[0]);
  if (path.includes(NODE_MODULES)) {
    // Node's own rule: a file belongs to the nearest ancestor directory holding a named package.json, and
    // the search never leaves node_modules. This attributes a package's vendored copy of another library
    // (…/@aws-amplify/core/dist/esm/node_modules/tslib/, no package.json of its own) to the shipping
    // package, and an npm alias (node_modules/matcher holding picomatch) to the package it really is.
    // Dot directories (.vite, .pnpm, .cache) are never packages.
    let dir = posix.dirname(path);
    while (dir.includes(NODE_MODULES) || dir.endsWith("/node_modules")) {
      const label = dir.slice(dir.lastIndexOf(NODE_MODULES) + NODE_MODULES.length);
      if (label && !label.startsWith(".") && !label.includes("/.")) {
        const name = readName(dir);
        if (isNonEmptyString(name) && !name.startsWith(".")) return { kind: "package", name, dir: label };
      }
      dir = posix.dirname(dir);
    }
    return { kind: "unknown", why: `no named package.json between ${path} and its node_modules root` };
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
export function evaluateGraph({ chunks, assets, allow, firstPartyRoot, trustedVirtual = new Set(), readName = readInstalledName }) {
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
      const owner = classifyModule(id, firstPartyRoot, { trustedVirtual, readName });
      if (owner.kind === "unknown") {
        throw new GateError(`chunk ${fileName} contains module ${JSON.stringify(id)} whose ownership cannot be established: ${owner.why}`);
      }
      if (owner.kind === "virtual") virtual += 1;
      else if (owner.kind === "first-party") firstParty += 1;
      else {
        packages.add(owner.name);
        if (allowlisted.has(owner.name)) {
          violations.push(`${owner.name} is bundled into ${fileName}: ${id}${owner.dir !== owner.name ? ` (installed as ${owner.dir})` : ""}`);
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

const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", colon: ":", tab: "\t", newline: "\n", sol: "/", nbsp: " " };
/** Decode numeric and the few named entities that matter for URL schemes. */
export function decodeEntities(text) {
  return text.replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);?/gi, (match, entity) => {
    if (entity[0] === "#") {
      const code = entity[1].toLowerCase() === "x" ? parseInt(entity.slice(2), 16) : parseInt(entity.slice(1), 10);
      return Number.isFinite(code) && code >= 0 && code <= 0x10ffff ? String.fromCodePoint(code) : match;
    }
    return ENTITIES[entity.toLowerCase()] ?? match;
  });
}
const normaliseUrl = (value) => decodeEntities(value).replace(/[\u0000- \u007f]+/g, "").toLowerCase();
// Raw-text elements: their content is never markup (noscript, noembed and noframes included, as a browser
// with scripting enabled reads them), so the only thing to find is the matching end tag.
const RAW_TEXT = new Set(["script", "style", "textarea", "title", "xmp", "noscript", "noembed", "noframes"]);
const REFUSED_TAGS = new Set(["iframe", "frame", "frameset", "object", "embed", "applet", "base", "portal", "fencedframe"]);
// SVG animation can rewrite an attribute (an anchor's href) into something executable after the fact.
const SVG_ANIMATION = new Set(["animate", "set", "animatemotion", "animatetransform", "animatecolor", "discard"]);
// Every JavaScript MIME type essence a browser executes (HTML Standard), plus "module" and the empty type.
const JS_MIME = new Set([
  "", "module", "application/ecmascript", "application/javascript", "application/x-ecmascript", "application/x-javascript", "text/ecmascript",
  "text/javascript", "text/javascript1.0", "text/javascript1.1", "text/javascript1.2", "text/javascript1.3", "text/javascript1.4", "text/javascript1.5",
  "text/jscript", "text/livescript", "text/x-ecmascript", "text/x-javascript",
]);
const SCRIPT_SOURCE_ATTRS = ["src", "href", "xlink:href"];
const DOCUMENT_ORIGIN = "https://document.invalid";

/** An attribute value as the browser reads it for semantics: entity-decoded, trimmed, lower-cased. */
const semantic = (attrs, name) => decodeEntities(attrs.get(name) ?? "").trim().toLowerCase();

/**
 * A browser's first pass over a URL attribute: entity-decode, drop tabs and newlines, trim leading and
 * trailing controls and spaces. Anything odd left over (inner whitespace, controls, a backslash) fails.
 */
export function cleanUrl(raw, label) {
  const value = decodeEntities(raw).replace(/[\t\n\r]/g, "").replace(/^[\u0000- \u007f]+|[\u0000- \u007f]+$/g, "");
  if (/[\u0000- \u007f\\]/.test(value)) throw new GateError(`${label}: URL ${JSON.stringify(raw)} contains whitespace, control characters or a backslash; refused`);
  return value;
}

/**
 * Resolve a URL the way the browser would, against the document's own deployment URL (a fixed origin,
 * the base path, the document's path). Returns { external: true } for another origin, or the
 * output-relative path for a same-origin location under the base; anything percent-encoded or outside
 * the base fails.
 */
export function resolveLocal(raw, rel, prefix, label) {
  const value = cleanUrl(raw, label);
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
    if (raw === null) continue;
    const value = normaliseUrl(raw);
    if (/^(?:javascript|vbscript|livescript|mocha):/.test(value)) throw new GateError(`${label}: <${name} ${attr}> uses an executable URL scheme; refused`);
    if (value.startsWith("data:") && !value.startsWith("data:image/")) throw new GateError(`${label}: <${name} ${attr}> is a data: URL that is not an image; refused`);
  }
}

/**
 * Tokenise an HTML (or, with svg: true, an SVG) document conservatively. Returns { scripts: [{ attrs, body }],
 * preloads: string[], refreshes: string[] }. Refuses: comments holding markup, declarations other than
 * <!doctype> (the XML prolog is allowed in svg mode), processing instructions, CDATA, a "<" that does not
 * start a tag, unterminated tags, self-closing raw-text elements, refused tags, <link rel="import">,
 * <meta http-equiv="refresh"> without a url, and every attribute refuseAttributes() rejects.
 */
export function scanDocument(text, label, { svg = false } = {}) {
  const out = { scripts: [], preloads: [], refreshes: [] };
  const lower = text.toLowerCase();
  let i = 0;
  while (i < text.length) {
    const lt = text.indexOf("<", i);
    if (lt === -1) break;
    if (lower.startsWith("<!--", lt)) {
      const end = text.indexOf("-->", lt + 4);
      if (end === -1) throw new GateError(`${label}: unterminated comment`);
      if (/[<>]/.test(text.slice(lt + 4, end))) throw new GateError(`${label}: a comment contains markup; comments may hold plain text only`);
      i = end + 3;
      continue;
    }
    if (lower.startsWith("<![cdata[", lt)) throw new GateError(`${label}: CDATA sections are refused`);
    if (text[lt + 1] === "!") {
      if (!/^<!doctype\s/i.test(text.slice(lt, lt + 10))) throw new GateError(`${label}: unsupported declaration at offset ${lt}`);
      const end = text.indexOf(">", lt);
      if (end === -1) throw new GateError(`${label}: unterminated doctype`);
      i = end + 1;
      continue;
    }
    if (text[lt + 1] === "?") {
      // Only the XML declaration itself, in SVG mode; "<?xml-stylesheet …?>" and the like are refused.
      if (!svg || !lower.startsWith("<?xml", lt) || !/\s/.test(text[lt + 5] ?? "")) throw new GateError(`${label}: processing instructions are refused`);
      const end = text.indexOf("?>", lt);
      if (end === -1) throw new GateError(`${label}: unterminated XML prolog`);
      i = end + 2;
      continue;
    }
    if (text[lt + 1] === "/") {
      const end = text.indexOf(">", lt);
      if (end === -1) throw new GateError(`${label}: unterminated end tag`);
      i = end + 1;
      continue;
    }
    const nameMatch = /^<([a-zA-Z][a-zA-Z0-9:_-]*)/.exec(text.slice(lt, lt + 80));
    if (!nameMatch) throw new GateError(`${label}: "<" at offset ${lt} does not start a tag; text must be escaped as &lt;`);
    const name = nameMatch[1].toLowerCase();
    // A browser reads the tag name up to whitespace, "/" or ">": "<script!" is an unknown element, not a
    // script with an attribute. Anything but those three delimiters after the name is refused.
    const delimiter = text[lt + 1 + nameMatch[1].length];
    if (delimiter !== undefined && !/[\s/>]/.test(delimiter)) throw new GateError(`${label}: unsupported tag name at offset ${lt} ("<${nameMatch[1]}${delimiter}"); refused`);
    // Namespace-qualified element names (an SVG "s:script") are refused in both modes.
    if (name.includes(":")) throw new GateError(`${label}: qualified element name <${name}> is refused`);
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
    const selfClosing = /\/\s*$/.test(rawAttrs);
    const attrs = parseAttributes(rawAttrs.replace(/\/\s*$/, ""), `${label}: <${name}>`);
    refuseAttributes(name, attrs, label);
    if (REFUSED_TAGS.has(name)) throw new GateError(`${label}: <${name}> is refused (an embedded or external executable context)`);
    if (name === "foreignobject") throw new GateError(`${label}: <foreignObject> embeds HTML inside SVG; refused`);
    if (SVG_ANIMATION.has(name)) throw new GateError(`${label}: SVG animation element <${name}> can rewrite attributes after validation; refused`);
    if (name === "link") {
      const rel = semantic(attrs, "rel").split(/\s+/);
      if (rel.includes("import")) throw new GateError(`${label}: <link rel="import"> is refused`);
      if (rel.includes("modulepreload") || (rel.includes("preload") && semantic(attrs, "as") === "script")) {
        const href = attrs.get("href");
        if (!isNonEmptyString(href)) throw new GateError(`${label}: a script preload without href`);
        out.preloads.push(href);
      }
    }
    if (name === "meta" && semantic(attrs, "http-equiv") === "refresh") {
      const match = /^\s*\d+\s*;\s*url\s*=\s*(.+?)\s*$/i.exec(decodeEntities(attrs.get("content") ?? ""));
      if (!match) throw new GateError(`${label}: <meta http-equiv="refresh"> without a url`);
      out.refreshes.push(match[1].replace(/^['"]|['"]$/g, ""));
    }
    i = j + 1;
    if (RAW_TEXT.has(name)) {
      if (selfClosing) throw new GateError(`${label}: a self-closing <${name}/> is not how browsers read it; refused`);
      // The end tag must be the real one: "</stylex>" does not close <style>.
      const match = new RegExp(`</${name}(?=[\\s/>])`, "i").exec(text.slice(i));
      if (!match) throw new GateError(`${label}: <${name}> without </${name}>`);
      const close = i + match.index;
      if (name === "script") out.scripts.push({ attrs, body: text.slice(i, close) });
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
    const doc = scanDocument(readFileSync(join(dir, rel), "utf8"), tag(rel));
    for (const { attrs, body } of doc.scripts) {
      // The type the browser acts on: `type` when present (even empty), else `language` as text/<language>.
      const type = attrs.has("type") ? semantic(attrs, "type") : attrs.has("language") ? `text/${semantic(attrs, "language")}` : "";
      if (type === "importmap") throw new GateError(`${tag(rel)}: import maps are refused`);
      const executable = JS_MIME.has(type);
      const sources = SCRIPT_SOURCE_ATTRS.filter((attr) => attrs.has(attr));
      if (sources.length > 1) throw new GateError(`${tag(rel)}: a <script> with several source attributes (${sources.join(", ")}) is refused`);
      if (sources.length === 1) {
        if (!executable) throw new GateError(`${tag(rel)}: a <script ${sources[0]}> with a non-script type is refused`);
        if (body.trim() !== "") throw new GateError(`${tag(rel)}: a <script ${sources[0]}> must have an empty body`);
        const url = attrs.get(sources[0]);
        if (!isNonEmptyString(url)) throw new GateError(`${tag(rel)}: <script> with an empty ${sources[0]}`);
        const resolved = resolveLocal(url, rel, prefix, tag(rel));
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
    if (kind === "svg") {
      const text = written().toString("utf8");
      const doc = scanDocument(text, `${label}${rel}`, { svg: true });
      if (doc.scripts.length > 0) throw new GateError(`${label}${rel} is an SVG document carrying a <script>; refused`);
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
