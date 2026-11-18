// Self-test for npm-audit-allowlist-bundle-check.mjs: `node --test npm-audit-allowlist-bundle-check.test.mjs`.
// The last cases run REAL Vite builds, so the cwd (or BUNDLE_CHECK_VITE_PROJECT) must be a project with
// Vite, react and picomatch installed (both this repository's npm projects qualify after `npm ci`).
// A missing Vite is a test FAILURE, not a skip: the real-build cases are the point.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";

import { GateError, parseAllowlist } from "./npm-audit-allowlist.mjs";
import {
  buildGraph,
  checkOutputs,
  classifyModule,
  evaluateGraph,
  main,
  parseArgs,
  resolveVite,
  walkOutput,
} from "./npm-audit-allowlist-bundle-check.mjs";

const NOW = Date.parse("2026-11-18T12:00:00Z");
const ALLOW_DOC = {
  allow: [{ id: "GHSA-fc5q-39xc-vjpf", package: "picomatch", reason: "a".repeat(40), added: "2026-11-18", expires: "2026-12-16T00:00:00Z" }],
};
const allow = parseAllowlist(ALLOW_DOC, NOW);
const ROOT = "/work/app";
const throwsGate = (fn, pattern) => assert.throws(fn, (error) => error instanceof GateError && pattern.test(error.message));
const rejectsGate = (promise, pattern) => assert.rejects(promise, (error) => error instanceof GateError && pattern.test(error.message));
const silent = { log() {}, error() {} };
const APP = [`${ROOT}/src/main.tsx`, `${ROOT}/src/App.tsx`, `${ROOT}/node_modules/react/index.js`, "\u0000vite/preload-helper.js"];
const chunks = (entries) => new Map(entries);

test("module ids are attributed to their package after normalisation, or to first-party, virtual or unknown", () => {
  const pkg = (id) => classifyModule(id, ROOT);
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
  assert.deepEqual(pkg("\u0000vite/preload-helper.js"), { kind: "virtual" });
  assert.deepEqual(pkg("\u0000commonjsHelpers.js"), { kind: "virtual" });
  assert.deepEqual(pkg("vite/modulepreload-polyfill.js"), { kind: "virtual" });
  assert.deepEqual(pkg("virtual:repo-index"), { kind: "virtual" });
  assert.deepEqual(pkg("\u0000virtual:repo-index"), { kind: "virtual" });
  // Seen in the real frontend build: Vite's empty stub for a Node built-in that a browser bundle imports.
  assert.deepEqual(pkg("__vite-browser-external"), { kind: "virtual" });
  assert.deepEqual(pkg("\u0000__vite-browser-external:path"), { kind: "virtual" });
  assert.deepEqual(pkg("/elsewhere/lib/thing.js"), { kind: "unknown" });
  assert.deepEqual(pkg(`${ROOT}/../sibling/index.js`), { kind: "unknown" });
  assert.deepEqual(pkg(`${ROOT}/node_modules/`), { kind: "unknown" });
  assert.deepEqual(pkg(`${ROOT}/node_modules/@scope`), { kind: "unknown" });
  assert.deepEqual(pkg(""), { kind: "unknown" });
  assert.deepEqual(pkg(undefined), { kind: "unknown" });
  // The first-party root is compared as a path, not a prefix string.
  assert.deepEqual(classifyModule("/work/application/src/x.js", ROOT), { kind: "unknown" });
  assert.deepEqual(classifyModule(`${ROOT}/src/x.js`, `${ROOT}/`), { kind: "first-party" });
});

test("an allowlisted package in any chunk is a violation; unknown ownership and opaque script assets fail closed", () => {
  const clean = evaluateGraph({ chunks: chunks([["assets/index.js", APP]]), assets: new Map([["assets/index.css", "asset"], ["index.html", "asset"]]), allow, firstPartyRoot: ROOT });
  assert.deepEqual(clean.violations, []);
  assert.deepEqual([clean.modules, clean.firstParty, clean.virtual, [...clean.packages]], [4, 2, 1, ["react"]]);
  const shipped = evaluateGraph({
    chunks: chunks([
      ["assets/index.js", APP],
      ["assets/vendor.js", [`${ROOT}/node_modules/tinyglobby/dist/index.js`, `\u0000${ROOT}/node_modules/picomatch/index.js?commonjs-proxy`, `${ROOT}/node_modules/picomatch/lib/picomatch.js`]],
    ]),
    assets: new Map(),
    allow,
    firstPartyRoot: ROOT,
  });
  assert.equal(shipped.violations.length, 2);
  assert.match(shipped.violations[0], /^picomatch is bundled into assets\/vendor\.js: /);
  throwsGate(() => evaluateGraph({ chunks: chunks([]), assets: new Map(), allow, firstPartyRoot: ROOT }), /emitted no JavaScript chunk/);
  throwsGate(() => evaluateGraph({ chunks: chunks([["a.js", []]]), assets: new Map(), allow, firstPartyRoot: ROOT }), /lists no modules/);
  throwsGate(() => evaluateGraph({ chunks: chunks([["a.js", [...APP, "/elsewhere/x.js"]]]), assets: new Map(), allow, firstPartyRoot: ROOT }), /ownership cannot be established/);
  throwsGate(() => evaluateGraph({ chunks: chunks([["a.js", APP]]), assets: new Map([["assets/worker-abc.js", "asset"]]), allow, firstPartyRoot: ROOT }), /opaque executable/);
  throwsGate(() => evaluateGraph({ chunks: chunks([["a.js", [`${ROOT}/node_modules/react/index.js`]]]), assets: new Map(), allow, firstPartyRoot: ROOT }), /no first-party module/);
  throwsGate(() => evaluateGraph({ chunks: chunks([["a.js", [`${ROOT}/src/main.tsx`]]]), assets: new Map(), allow, firstPartyRoot: ROOT }), /no npm package/);
});

test("written scripts must be chunks or byte-identical public copies; HTML may only load known local scripts", () => {
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-out-"));
  try {
    const outDir = join(dir, "dist");
    const publicDir = join(dir, "public");
    mkdirSync(join(outDir, "assets"), { recursive: true });
    mkdirSync(publicDir);
    writeFileSync(join(outDir, "assets", "index-abc.js"), "console.log(1)");
    writeFileSync(join(outDir, "assets", "index-abc.css"), "body{}");
    writeFileSync(join(outDir, "embed.js"), "customElements.define('x-y', class extends HTMLElement {})");
    writeFileSync(join(publicDir, "embed.js"), "customElements.define('x-y', class extends HTMLElement {})");
    writeFileSync(
      join(outDir, "index.html"),
      '<html><head><script>window.theme=1</script><script type="module" crossorigin src="/site/assets/index-abc.js"></script>' +
        '<script src="/site/embed.js"></script></head></html>',
    );
    const files = walkOutput(outDir);
    assert.deepEqual(files, ["assets/index-abc.css", "assets/index-abc.js", "embed.js", "index.html"]);
    const chunkFiles = new Set(["assets/index-abc.js"]);
    const ok = checkOutputs({ outDir, files, chunkFiles, publicDir, base: "/site/" });
    assert.deepEqual(ok, { scripts: ["assets/index-abc.js", "embed.js"], publicCopies: ["embed.js"], documents: 1, inline: 1 });
    // A public file that differs from what was written is not a copy.
    writeFileSync(join(publicDir, "embed.js"), "customElements.define('x-y', class extends HTMLElement {}) // changed");
    throwsGate(() => checkOutputs({ outDir, files, chunkFiles, publicDir, base: "/site/" }), /neither a chunk of this build nor a byte-identical copy/);
    throwsGate(() => checkOutputs({ outDir, files, chunkFiles, publicDir: null, base: "/site/" }), /no publicDir/);
    // Scripts that are neither chunk nor copy, whatever their extension.
    writeFileSync(join(publicDir, "embed.js"), "customElements.define('x-y', class extends HTMLElement {})");
    writeFileSync(join(outDir, "assets", "vendor.mjs"), "/* picomatch */");
    throwsGate(() => checkOutputs({ outDir, files: walkOutput(outDir), chunkFiles, publicDir, base: "/site/" }), /vendor\.mjs is a script/);
    rmSync(join(outDir, "assets", "vendor.mjs"));
    // HTML: external and unknown script sources fail; relative local sources resolve against the document.
    writeFileSync(join(outDir, "index.html"), '<script src="https://cdn.example.invalid/x.js"></script>');
    throwsGate(() => checkOutputs({ outDir, files, chunkFiles, publicDir, base: "/site/" }), /external script/);
    writeFileSync(join(outDir, "index.html"), '<script src="//cdn.example.invalid/x.js"></script>');
    throwsGate(() => checkOutputs({ outDir, files, chunkFiles, publicDir, base: "/site/" }), /external script/);
    writeFileSync(join(outDir, "index.html"), '<script src="/site/assets/missing.js"></script>');
    throwsGate(() => checkOutputs({ outDir, files, chunkFiles, publicDir, base: "/site/" }), /not a chunk of this build nor a public copy/);
    writeFileSync(join(outDir, "index.html"), "<script src='./assets/index-abc.js?v=1'></script><script src=embed.js></script>");
    assert.equal(checkOutputs({ outDir, files, chunkFiles, publicDir, base: "/" }).inline, 0);
    // Symlinks anywhere in the output fail closed.
    symlinkSync(join(outDir, "assets", "index-abc.js"), join(outDir, "assets", "link.js"));
    throwsGate(() => walkOutput(outDir), /symlink/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("CLI arguments: --out is required and must lie outside the project", async () => {
  throwsGate(() => parseArgs([]), /--out/);
  throwsGate(() => parseArgs(["--out"]), /bad argument/);
  throwsGate(() => parseArgs(["--out", "x", "--bogus", "1"]), /bad argument/);
  assert.equal(parseArgs(["--out", "x", "--project", "p", "--first-party-root", "r"])["first-party-root"], "r");
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-cli-"));
  try {
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    await rejectsGate(main(["--out", join(dir, "dist"), "--project", dir, "--allowlist", join(dir, "allow.json")], silent), /must be outside the project/);
    await rejectsGate(main(["--out", join(dir, "dist"), "--project", join(dir, "nope"), "--allowlist", join(dir, "allow.json")], silent), /vite is not installed/);
    await rejectsGate(main(["--out", join(dir, "dist"), "--project", join(dir, "nope"), "--allowlist", join(dir, "allow.json"), "--now", "2027-01-01T00:00:00Z"], silent), /expired at/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

/** A project with Vite installed to run the real-build cases against: BUNDLE_CHECK_VITE_PROJECT or the cwd. */
function viteProject() {
  const projectDir = resolve(process.env.BUNDLE_CHECK_VITE_PROJECT || process.cwd());
  const { entry, version } = resolveVite(projectDir); // GateError (test failure) when Vite is absent
  const require = createRequire(join(projectDir, "package.json"));
  for (const name of ["react", "picomatch"]) require.resolve(name); // throws when the project lacks them
  return { projectDir, entry, version, nodeModules: join(projectDir, "node_modules") };
}

/**
 * Write a minimal Vite project whose entry imports the given bare specifiers. Its node_modules is a
 * symlink to the Vite project's, so Vite, react and picomatch resolve exactly as in a real install
 * (Rollup records their real paths, which is what the gate classifies).
 */
function miniProject(dir, nodeModules, imports, { publicScript = true } = {}) {
  mkdirSync(join(dir, "src"), { recursive: true });
  symlinkSync(nodeModules, join(dir, "node_modules"), "dir");
  writeFileSync(join(dir, "package.json"), JSON.stringify({ name: "mini", private: true, type: "module" }));
  writeFileSync(
    join(dir, "index.html"),
    '<!doctype html><html><head><script>window.__t = 1</script></head><body><div id="root"></div>' +
      (publicScript ? '<script src="/embed.js"></script>' : "") +
      '<script type="module" src="/src/main.js"></script></body></html>',
  );
  const lines = imports.map((file, index) => `import * as m${index} from ${JSON.stringify(file)};`);
  lines.push(`document.getElementById("root").textContent = [${imports.map((_, index) => `typeof m${index}`).join(", ")}].join(" ");`);
  writeFileSync(join(dir, "src", "main.js"), `${lines.join("\n")}\n`);
  if (publicScript) {
    mkdirSync(join(dir, "public"));
    writeFileSync(join(dir, "public", "embed.js"), "customElements.define('x-embed', class extends HTMLElement {});\n");
  }
}

test("a real Vite build that bundles only unrelated packages passes, reporting the verbatim public copy", async () => {
  const { entry, nodeModules } = viteProject();
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-real-"));
  try {
    const app = join(dir, "app");
    miniProject(app, nodeModules, ["react"]);
    writeFileSync(join(dir, "allow.json"), JSON.stringify(ALLOW_DOC));
    const lines = [];
    // react resolves through the symlinked node_modules; the mini app itself is the first-party root.
    await main(["--out", join(dir, "out"), "--project", app, "--allowlist", join(dir, "allow.json"), "--now", "2026-11-18T12:00:00Z"], {
      log: (line) => lines.push(line),
      error: (line) => lines.push(line),
    });
    assert.ok(lines.some((line) => /PASS; not in the shipped bundle: picomatch/.test(line)), lines.join("\n"));
    assert.ok(lines.some((line) => /1 verbatim public copy \(embed\.js\)/.test(line)), lines.join("\n"));
    assert.ok(lines.some((line) => /1 HTML document\(s\) with 1 inline script\(s\)/.test(line)), lines.join("\n"));
    // The graph itself, through the same plugin: react is attributed, the entry is first-party.
    const vite = await import(pathToFileURL(entry).href);
    const record = await buildGraph({ projectDir: app, outDir: join(dir, "out2"), vite });
    const graph = evaluateGraph({ chunks: record.chunks, assets: record.assets, allow, firstPartyRoot: app });
    assert.ok(graph.packages.has("react"), [...graph.packages].join(", "));
    assert.ok(!graph.packages.has("picomatch"));
    assert.ok(graph.firstParty >= 1);
    assert.equal(record.config.publicDir, join(app, "public"));
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
    await rejectsGate(
      main(["--out", join(dir, "out"), "--project", app, "--allowlist", join(dir, "allow.json"), "--now", "2026-11-18T12:00:00Z"], {
        log: (line) => lines.push(line),
        error: (line) => lines.push(line),
      }),
      /allowlist premise/,
    );
    assert.ok(lines.some((line) => /VIOLATION picomatch is bundled into /.test(line)), lines.join("\n"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a real Vite build with a script copied from publicDir that differs from the written file is refused", async () => {
  // Vite copies publicDir verbatim, so the only way to get here is a plugin or a later step altering the
  // output: checkOutputs() on a real build's output with a tampered public file must fail closed.
  const { entry, nodeModules } = viteProject();
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-real-"));
  try {
    const app = join(dir, "app");
    miniProject(app, nodeModules, ["react"]);
    const vite = await import(pathToFileURL(entry).href);
    const outDir = join(dir, "out");
    const record = await buildGraph({ projectDir: app, outDir, vite });
    writeFileSync(join(outDir, "embed.js"), "/* replaced after the build */\n");
    throwsGate(
      () => checkOutputs({ outDir, files: walkOutput(outDir), chunkFiles: new Set(record.chunks.keys()), publicDir: record.config.publicDir, base: record.config.base }),
      /neither a chunk of this build nor a byte-identical copy/,
    );
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
  assert.equal(typeof dirname, "function");
});
