// Self-test for npm-audit-allowlist-bundle-check.mjs: `node --test npm-audit-allowlist-bundle-check.test.mjs`.
// The real-build cases need the cwd (or BUNDLE_CHECK_VITE_PROJECT) to be a project with Vite, react and
// picomatch installed (both this repository's npm projects qualify after `npm ci`). A missing Vite is a
// test FAILURE, not a skip: the real-build cases are the point.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
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
  checkScripts,
  classifyModule,
  evaluateGraph,
  gitTracked,
  main,
  normalizeInline,
  parseArgs,
  parseAttributes,
  parseBundleTrust,
  projectKey,
  resolveVite,
  scanScripts,
  sha256,
  tripwire,
  walkOutput,
} from "./npm-audit-allowlist-bundle-check.mjs";

const NOW = Date.parse("2026-11-18T12:00:00Z");
const REASON = "a".repeat(40);
const ALLOW_ENTRY = { id: "GHSA-fc5q-39xc-vjpf", package: "picomatch", reason: REASON, added: "2026-11-18", expires: "2026-12-16T00:00:00Z" };
const EMBED = "customElements.define('x-embed', class extends HTMLElement {});\n";
const INLINE = "window.__t = 1";
/** The allowlist document the mini projects are judged with: picomatch exempt, embed.js and the inline stub pinned for project ".". */
const ALLOW_DOC = {
  allow: [ALLOW_ENTRY],
  bundle: {
    trustedVirtualModules: [{ id: "\u0000virtual:fixture", project: ".", reason: REASON }],
    firstPartyScripts: [
      { project: ".", public: "embed.js", sha256: sha256(EMBED), reason: REASON },
      { project: ".", inline: "sha256", sha256: sha256(normalizeInline(INLINE)), reason: REASON },
      { project: ".", inline: "pattern", pattern: "^window\\.location\\.replace\\('/site/[a-z/-]*' \\+ window\\.location\\.search\\);$", reason: REASON },
      { project: "other", public: "other.js", sha256: "0".repeat(64), reason: REASON },
    ],
  },
};
const allow = parseAllowlist(ALLOW_DOC, NOW);
const trust = parseBundleTrust(ALLOW_DOC, ".");
const ROOT = "/work/app";
const throwsGate = (fn, pattern) => assert.throws(fn, (error) => error instanceof GateError && pattern.test(error.message));
const rejectsGate = (promise, pattern) => assert.rejects(promise, (error) => error instanceof GateError && pattern.test(error.message));
const silent = { log() {}, error() {} };
const APP = [`${ROOT}/src/main.tsx`, `${ROOT}/src/App.tsx`, `${ROOT}/node_modules/react/index.js`, "\u0000vite/preload-helper.js"];
const chunks = (entries) => new Map(entries);
const graph = (chunkEntries, assets = [], over = {}) =>
  evaluateGraph({ chunks: chunks(chunkEntries), assets: new Map(assets.map((name) => [name, "asset"])), allow, firstPartyRoot: ROOT, ...over });

test("module ids are attributed to their package after normalisation, or to first-party, virtual or unknown", () => {
  const pkg = (id) => classifyModule(id, ROOT, trust.virtual);
  assert.deepEqual(pkg(`${ROOT}/node_modules/picomatch/lib/picomatch.js`), { kind: "package", name: "picomatch" });
  assert.deepEqual(pkg(`${ROOT}/node_modules/@scope/name/dist/index.js`), { kind: "package", name: "@scope/name" });
  assert.deepEqual(pkg(`${ROOT}/node_modules/.pnpm/picomatch@4.0.7/node_modules/picomatch/index.js`), { kind: "package", name: "picomatch" });
  assert.deepEqual(pkg(`${ROOT}/node_modules/vite/node_modules/picomatch/index.js`), { kind: "package", name: "picomatch" });
  assert.deepEqual(pkg(`${ROOT}/node_modules/other/../picomatch/lib/picomatch.js`), { kind: "package", name: "picomatch" });
  assert.deepEqual(pkg(`${ROOT}/node_modules/picomatch/index.js?commonjs-exports`), { kind: "package", name: "picomatch" });
  assert.deepEqual(pkg(`\u0000${ROOT}/node_modules/picomatch/index.js?commonjs-proxy`), { kind: "package", name: "picomatch" });
  assert.deepEqual(pkg(`${ROOT}\\node_modules\\picomatch\\index.js`), { kind: "package", name: "picomatch" });
  assert.deepEqual(pkg(`${ROOT}/src/main.tsx`), { kind: "first-party" });
  assert.deepEqual(pkg(`${ROOT}/src/../index.html`), { kind: "first-party" });
  // Vite's own helpers, and the virtual module the fixture allowlist trusts explicitly.
  assert.deepEqual(pkg("\u0000vite/preload-helper.js"), { kind: "virtual" });
  assert.deepEqual(pkg("\u0000vite/modulepreload-polyfill.js"), { kind: "virtual" });
  assert.deepEqual(pkg("\u0000commonjsHelpers.js"), { kind: "virtual" });
  assert.deepEqual(pkg("__vite-browser-external"), { kind: "virtual" });
  assert.deepEqual(pkg("\u0000__vite-browser-external:path"), { kind: "virtual" });
  assert.deepEqual(pkg("\u0000virtual:fixture"), { kind: "virtual" });
  // Any other virtual module is unknown, however it is spelled; the cache namespace is not a package.
  assert.equal(pkg("\u0000virtual:matcher").kind, "unknown");
  assert.equal(pkg("virtual:matcher").kind, "unknown");
  assert.equal(pkg("\u0000plugin-helper.js").kind, "unknown");
  assert.equal(classifyModule("\u0000virtual:fixture", ROOT).kind, "unknown");
  assert.match(pkg(`${ROOT}/node_modules/.vite/deps/picomatch.js`).why, /not a package name/);
  assert.equal(pkg(`${ROOT}/node_modules/@scope/.cache/x.js`).kind, "unknown");
  assert.equal(pkg("/elsewhere/lib/thing.js").kind, "unknown");
  assert.equal(pkg(`${ROOT}/../sibling/index.js`).kind, "unknown");
  assert.equal(pkg(`${ROOT}/node_modules/`).kind, "unknown");
  assert.equal(pkg(`${ROOT}/node_modules/@scope`).kind, "unknown");
  assert.equal(pkg("").kind, "unknown");
  assert.equal(pkg(undefined).kind, "unknown");
  // The first-party root is compared as a path, not a prefix string.
  assert.equal(classifyModule("/work/application/src/x.js", ROOT).kind, "unknown");
  assert.deepEqual(classifyModule(`${ROOT}/src/x.js`, `${ROOT}/`), { kind: "first-party" });
});

test("an allowlisted package in any chunk is a violation; unknown ownership and non-inert assets fail closed", () => {
  const clean = graph([["assets/index.js", APP]], ["assets/index.css", "index.html", "assets/logo.svg", "assets/font.woff2"]);
  assert.deepEqual(clean.violations, []);
  assert.deepEqual([clean.modules, clean.firstParty, clean.virtual, [...clean.packages]], [4, 2, 1, ["react"]]);
  const shipped = graph([
    ["assets/index.js", APP],
    [`${WORKER_PREFIX}assets/w.js`, [`${ROOT}/src/w.js`, `\u0000${ROOT}/node_modules/picomatch/index.js?commonjs-proxy`, `${ROOT}/node_modules/picomatch/lib/picomatch.js`]],
  ]);
  assert.equal(shipped.violations.length, 2);
  assert.match(shipped.violations[0], /^picomatch is bundled into worker:assets\/w\.js: /);
  throwsGate(() => graph([]), /emitted no JavaScript chunk/);
  throwsGate(() => graph([["a.js", []]]), /lists no modules/);
  throwsGate(() => graph([["a.js", [...APP, "/elsewhere/x.js"]]]), /ownership cannot be established: outside the first-party root/);
  throwsGate(() => graph([["a.js", [...APP, "\u0000virtual:matcher"]]]), /not listed in bundle\.trustedVirtualModules/);
  throwsGate(() => graph([["a.js", [...APP, `${ROOT}/node_modules/.vite/deps/picomatch.js`]]]), /not a package name/);
  throwsGate(() => graph([["a.js", APP]], ["assets/worker-abc.js"]), /opaque executable/);
  throwsGate(() => graph([["a.js", APP]], ["assets/matcher.txt"]), /no recognised inert type/);
  throwsGate(() => graph([["a.js", APP]], ["assets/payload"]), /no recognised inert type/);
  throwsGate(() => graph([["a.js", [`${ROOT}/node_modules/react/index.js`]]]), /no first-party module/);
  throwsGate(() => graph([["a.js", [`${ROOT}/src/main.tsx`]]]), /no npm package/);
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

test("script tags are parsed quote-aware with exact attribute names; <base> is refused", () => {
  const one = (html) => scanScripts(html, "doc");
  assert.deepEqual(one('<script type="module" crossorigin src="/a.js"></script>')[0].attrs.get("src"), "/a.js");
  assert.deepEqual(one("<SCRIPT SRC=a.js></SCRIPT>")[0].attrs.get("src"), "a.js");
  assert.equal(one('<script data-src="/a.js">x</script>')[0].attrs.has("src"), false);
  assert.equal(one('<script data-src="/a.js" src="https://cdn.example.invalid/x.js"></script>')[0].attrs.get("src"), "https://cdn.example.invalid/x.js");
  assert.equal(one(`<script title="a > b" src='/a.js'></script>`)[0].attrs.get("src"), "/a.js");
  assert.equal(one('<script type="text/plain">var s = "<script>"; </script>').length, 1);
  assert.equal(one("<scripts></scripts><script>x</script>").length, 1);
  assert.equal(one("<script>a</script><script>b</script>")[1].body, "b");
  assert.equal(one("<script />x</script>")[0].attrs.size, 0);
  throwsGate(() => one("<script/>"), /without <\/script>/); // a script element always needs its end tag
  throwsGate(() => one('<script src="/a.js" src="/b.js"></script>'), /repeated/);
  throwsGate(() => one('<script src="/a.js"'), /unterminated/);
  throwsGate(() => one('<script src="/a.js">no end'), /without <\/script>/);
  throwsGate(() => one("<script =x></script>"), /cannot parse attributes/);
  throwsGate(() => parseAttributes('a="1" a="2"', "t"), /repeated/);
  assert.deepEqual([...parseAttributes(` async defer src = "x.js" type='module' nomodule`, "t")], [["async", null], ["defer", null], ["src", "x.js"], ["type", "module"], ["nomodule", null]]);
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-doc-"));
  try {
    const known = new Set(["assets/index-abc.js", "embed.js"]);
    const judge = (html, over = {}) => {
      writeFileSync(join(dir, "index.html"), html);
      return checkDocuments({ dir, files: ["index.html"], known, base: "/site/", trust, packages: ["picomatch"], ...over });
    };
    assert.deepEqual(judge(`<script>${INLINE}</script><script type="module" src="/site/assets/index-abc.js"></script><script src="/site/embed.js"></script>`), { documents: 1, inline: 1, pinnedBySha256: 1, pinnedByPattern: 0 });
    assert.deepEqual(judge("<script>window.location.replace('/site/start/which-project/' + window.location.search);</script>"), { documents: 1, inline: 1, pinnedBySha256: 0, pinnedByPattern: 1 });
    assert.deepEqual(judge(`<script>\n  ${INLINE}\n\n</script><script></script>`).inline, 1); // normalised body; empty inline ignored
    throwsGate(() => judge("<script>window.__t = 2</script>"), /inline script 1 is not pinned .*\(sha256 of its normalised body: [0-9a-f]{64}\)/);
    throwsGate(() => judge("<script>window.location.replace('https://evil.example/' + window.location.search);</script>"), /not pinned/);
    throwsGate(() => judge("<script>/* picomatch */</script>"), /mentions allowlisted package picomatch/);
    throwsGate(() => judge('<script src="https://cdn.example.invalid/x.js"></script>'), /external script/);
    throwsGate(() => judge('<script src="//cdn.example.invalid/x.js"></script>'), /external script/);
    throwsGate(() => judge('<script data-src="/site/assets/index-abc.js" src="https://cdn.example.invalid/x.js"></script>'), /external script/);
    throwsGate(() => judge('<script src="/site/assets/missing.js"></script>'), /not a chunk of this build nor a pinned public copy/);
    throwsGate(() => judge('<script src=""></script>'), /empty src/);
    throwsGate(() => judge("<script src></script>"), /empty src/);
    throwsGate(() => judge('<head><base href="https://cdn.example.invalid/"></head><script src="/site/embed.js"></script>'), /<base> element/);
    throwsGate(() => judge("<base/>"), /<base> element/);
    assert.equal(judge("<script src='./assets/index-abc.js?v=1'></script><script src=embed.js></script>", { base: "/" }).inline, 0);
    assert.equal(judge('<script src="/assets/index-abc.js"></script>', { base: "/" }).documents, 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("written scripts must be this build's chunks (same bytes) or pinned, tracked, byte-identical public copies", () => {
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-out-"));
  try {
    const outDir = join(dir, "dist");
    const publicDir = join(dir, "public");
    mkdirSync(join(outDir, "assets"), { recursive: true });
    mkdirSync(publicDir);
    writeFileSync(join(outDir, "assets", "index-abc.js"), "console.log(1)");
    writeFileSync(join(outDir, "assets", "index-abc.css"), "body{}");
    writeFileSync(join(outDir, "embed.js"), EMBED);
    writeFileSync(join(publicDir, "embed.js"), EMBED);
    const chunkHashes = new Map([["assets/index-abc.js", sha256("console.log(1)")]]);
    assert.deepEqual(walkOutput(outDir), ["assets/index-abc.css", "assets/index-abc.js", "embed.js"]);
    const check = (over = {}) => checkScripts({ dir: outDir, files: walkOutput(outDir), chunkHashes, trust, publicDir, packages: ["picomatch"], isTracked: () => true, ...over });
    const ok = check();
    assert.deepEqual([[...ok.known].sort(), ok.publicCopies], [["assets/index-abc.js", "embed.js"], ["embed.js"]]);
    // A chunk rewritten after the build no longer matches the bytes Rollup wrote.
    writeFileSync(join(outDir, "assets", "index-abc.js"), "console.log(1);/* appended */");
    throwsGate(() => check(), /rewritten after the build/);
    writeFileSync(join(outDir, "assets", "index-abc.js"), "console.log(1)");
    // A public copy must match its pin, be byte-identical to the publicDir file, and be tracked.
    writeFileSync(join(outDir, "embed.js"), `${EMBED}// changed\n`);
    writeFileSync(join(publicDir, "embed.js"), `${EMBED}// changed\n`);
    throwsGate(() => check(), /does not match its pin/);
    writeFileSync(join(outDir, "embed.js"), EMBED);
    throwsGate(() => check(), /not a byte-identical copy/);
    writeFileSync(join(publicDir, "embed.js"), EMBED);
    throwsGate(() => check({ publicDir: null }), /no publicDir/);
    throwsGate(() => check({ isTracked: () => false }), /git does not track/);
    assert.equal(gitTracked(join(publicDir, "embed.js")), false); // a temp dir is not a git repository
    // Unpinned scripts fail whatever their extension, and the message carries the hash needed to pin them.
    writeFileSync(join(outDir, "assets", "vendor.mjs"), "/* vendored */");
    throwsGate(() => check(), /vendor\.mjs is a script that is not a chunk of this build and not pinned .* \(sha256 [0-9a-f]{64}\)/);
    rmSync(join(outDir, "assets", "vendor.mjs"));
    // Tripwire: a pinned public copy that names the allowlisted package by word still fails.
    const vendored = "// bundled copy of picomatch 4.0.7\n";
    const trustVendored = parseBundleTrust({ allow: [], bundle: { firstPartyScripts: [{ project: ".", public: "embed.js", sha256: sha256(vendored), reason: REASON }] } }, ".");
    writeFileSync(join(outDir, "embed.js"), vendored);
    writeFileSync(join(publicDir, "embed.js"), vendored);
    throwsGate(() => check({ trust: trustVendored }), /public copy embed\.js mentions allowlisted package picomatch/);
    assert.throws(() => tripwire("x", "import('@scope/name')", ["@scope/name"]), GateError);
    tripwire("x", "scope/name @scope/named @scope/name.js mypicomatchlib", ["@scope/name", "picomatch"]); // only word matches count
    // Symlinks anywhere in the output fail closed.
    symlinkSync(join(outDir, "assets", "index-abc.js"), join(outDir, "assets", "link.js"));
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
 * Write a minimal Vite project (its own git repository) whose entry imports the given bare specifiers. Its
 * node_modules is a symlink to the Vite project's, so Vite, react and picomatch resolve exactly as in a
 * real install (Rollup records their real paths, which is what the gate classifies).
 */
function miniProject(dir, nodeModules, imports, { publicScript = true, inline = INLINE } = {}) {
  mkdirSync(join(dir, "src"), { recursive: true });
  symlinkSync(nodeModules, join(dir, "node_modules"), "dir");
  writeFileSync(join(dir, "package.json"), JSON.stringify({ name: "mini", private: true, type: "module" }));
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

/** Add an inline (`?worker&inline`) or file (`?worker`) web worker whose code imports the given specifiers. */
function addWorker(dir, imports, { inline }) {
  const lines = imports.map((file, index) => `import * as w${index} from ${JSON.stringify(file)};`);
  lines.push(`self.onmessage = () => self.postMessage([${imports.map((_, index) => `typeof w${index}`).join(", ")}].join(" "));`);
  writeFileSync(join(dir, "src", "worker.js"), `${lines.join("\n")}\n`);
  const entry = join(dir, "src", "main.js");
  writeFileSync(entry, `import W from "./worker.js?worker${inline ? "&inline" : ""}";\nnew W().postMessage(1);\n${readFileSync(entry, "utf8")}`);
}

const run = (argv, lines) => main(argv, { log: (line) => lines.push(line), error: (line) => lines.push(line) });

test("a real Vite build that bundles only unrelated packages passes, reporting the pinned public copy and inline script", async () => {
  const { entry, nodeModules } = viteProject();
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-real-"));
  try {
    const app = join(dir, "app");
    miniProject(app, nodeModules, ["react"]);
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    const lines = [];
    await run(["--out", join(dir, "out"), "--project", app, "--allowlist", join(dir, "allow.json"), "--now", "2026-11-18T12:00:00Z"], lines);
    const summary = lines.join("\n");
    assert.match(summary, /PASS; no shipped script resolves code from: picomatch/);
    assert.match(summary, /1 pinned public copy: embed\.js/);
    assert.match(summary, /1 document\(s\) with 1 inline script\(s\) \(1 pinned by sha256, 0 by pattern\)/);
    // The graph itself, through the same plugin: react is attributed, the entry is first-party, chunks are hashed.
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
    await rejectsGate(run(["--out", join(dir, "out"), "--project", app, "--allowlist", join(dir, "allow.json"), "--now", "2026-11-18T12:00:00Z"], lines), /allowlist premise/);
    assert.ok(lines.some((line) => /VIOLATION picomatch is bundled into /.test(line)), lines.join("\n"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a real Vite build whose INLINE worker imports picomatch FAILS the gate; a file worker is an opaque executable", async () => {
  const { nodeModules } = viteProject();
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-real-"));
  try {
    const inlineApp = join(dir, "inline");
    miniProject(inlineApp, nodeModules, ["react"], { publicScript: false });
    addWorker(inlineApp, ["picomatch"], { inline: true });
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    const lines = [];
    await rejectsGate(run(["--out", join(dir, "out"), "--project", inlineApp, "--allowlist", join(dir, "allow.json"), "--now", "2026-11-18T12:00:00Z"], lines), /allowlist premise/);
    assert.ok(lines.some((line) => new RegExp(`VIOLATION picomatch is bundled into ${WORKER_PREFIX}`).test(line)), lines.join("\n"));
    const fileApp = join(dir, "file");
    miniProject(fileApp, nodeModules, ["react"], { publicScript: false });
    addWorker(fileApp, ["react"], { inline: false });
    await rejectsGate(run(["--out", join(dir, "out2"), "--project", fileApp, "--allowlist", join(dir, "allow.json"), "--now", "2026-11-18T12:00:00Z"], []), /opaque executable/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("the deployment build's output must equal this build's chunks and pins (--verify-dist), and public copies must stay tracked and unchanged", async () => {
  const { nodeModules } = viteProject();
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-real-"));
  try {
    const app = join(dir, "app");
    miniProject(app, nodeModules, ["react"]);
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    const out = join(dir, "out");
    const dist = join(dir, "dist");
    const argv = (...extra) => ["--out", out, "--project", app, "--allowlist", join(dir, "allow.json"), "--now", "2026-11-18T12:00:00Z", ...extra];
    await run(argv(), []);
    cpSync(out, dist, { recursive: true }); // the deployment build: identical by construction here
    const lines = [];
    await run(argv("--verify-dist", dist), lines);
    assert.match(lines.join("\n"), /deployment build .*: .* every script byte-identical to this build's chunks or pinned/);
    await rejectsGate(run(argv("--verify-dist", join(dir, "nope")), []), /does not exist/);
    // A chunk altered in dist, an extra unpinned script, an unpinned inline script: each fails closed.
    const chunk = walkOutput(dist).find((rel) => /^assets\/.*\.js$/.test(rel));
    const original = readFileSync(join(dist, chunk));
    writeFileSync(join(dist, chunk), Buffer.concat([original, Buffer.from("\n/* appended */\n")]));
    await rejectsGate(run(argv("--verify-dist", dist), []), /rewritten after the build/);
    writeFileSync(join(dist, chunk), original);
    writeFileSync(join(dist, "extra.js"), "console.log('extra')");
    await rejectsGate(run(argv("--verify-dist", dist), []), /extra\.js is a script that is not a chunk of this build and not pinned/);
    rmSync(join(dist, "extra.js"));
    const html = readFileSync(join(dist, "index.html"), "utf8");
    writeFileSync(join(dist, "index.html"), html.replace("</body>", "<script>window.__late = 1</script></body>"));
    await rejectsGate(run(argv("--verify-dist", dist), []), /inline script 2 is not pinned/);
    writeFileSync(join(dist, "index.html"), html);
    // Public copies: tampered after the build, or untracked in public/, are refused.
    writeFileSync(join(dist, "embed.js"), "/* replaced after the build */\n");
    await rejectsGate(run(argv("--verify-dist", dist), []), /embed\.js does not match its pin/);
    writeFileSync(join(dist, "embed.js"), EMBED);
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
