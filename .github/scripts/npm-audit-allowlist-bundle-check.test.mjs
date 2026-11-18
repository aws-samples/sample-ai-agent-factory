// Self-test for npm-audit-allowlist-bundle-check.mjs. Zero dependencies:
// `node --test npm-audit-allowlist-bundle-check.test.mjs`.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { GateError, parseAllowlist } from "./npm-audit-allowlist.mjs";
import { evaluateBundle, isVirtual, main, packageOf, parseArgs, readSources, walkMaps } from "./npm-audit-allowlist-bundle-check.mjs";

const NOW = Date.parse("2026-11-18T12:00:00Z");
const ALLOW_DOC = {
  allow: [{ id: "GHSA-fc5q-39xc-vjpf", package: "picomatch", reason: "a".repeat(40), added: "2026-11-18", expires: "2026-12-16T00:00:00Z" }],
};
const allow = parseAllowlist(ALLOW_DOC, NOW);
const throwsGate = (fn, pattern) => assert.throws(fn, (error) => error instanceof GateError && pattern.test(error.message));
const quiet = (fn) => {
  const saved = { log: console.log, warn: console.warn, error: console.error };
  console.log = console.warn = console.error = () => {};
  try {
    return fn();
  } finally {
    Object.assign(console, saved);
  }
};
const APP = ["../src/main.tsx", "../src/App.tsx", "../node_modules/react/index.js", "../node_modules/@xyflow/react/dist/esm/index.js"];

test("bundled module paths resolve to their npm package, including scoped, nested and pnpm layouts", () => {
  assert.equal(packageOf("../node_modules/picomatch/lib/picomatch.js"), "picomatch");
  assert.equal(packageOf("../../node_modules/@scope/name/dist/index.js"), "@scope/name");
  assert.equal(packageOf("node_modules/.pnpm/picomatch@4.0.7/node_modules/picomatch/index.js"), "picomatch");
  assert.equal(packageOf("../node_modules/vite/node_modules/picomatch/index.js"), "picomatch");
  assert.equal(packageOf("..\\node_modules\\picomatch\\index.js"), "picomatch");
  assert.equal(packageOf("../src/main.tsx"), null);
  assert.equal(packageOf("node_modules/"), null);
  assert.equal(packageOf("node_modules/@scope"), null);
  assert.ok(isVirtual("\u0000vite/preload-helper.js"));
  assert.ok(isVirtual("vite/modulepreload-polyfill.js"));
  assert.ok(!isVirtual("../src/main.tsx"));
});

test("an allowlisted package anywhere in the module graph is a violation; its absence passes", () => {
  const clean = evaluateBundle({ sourcesByMap: new Map([["assets/index.js.map", APP]]), allow });
  assert.deepEqual(clean.violations, []);
  assert.equal(clean.firstParty, 2);
  assert.deepEqual([...clean.packages].sort(), ["@xyflow/react", "react"]);
  const shipped = evaluateBundle({
    sourcesByMap: new Map([
      ["assets/index.js.map", APP],
      ["assets/vendor.js.map", ["../node_modules/tinyglobby/dist/index.js", "../node_modules/picomatch/lib/picomatch.js"]],
    ]),
    allow,
  });
  assert.equal(shipped.violations.length, 1);
  assert.match(shipped.violations[0], /^picomatch is bundled: assets\/vendor\.js\.map lists /);
});

test("maps that cannot prove anything fail closed", () => {
  throwsGate(() => evaluateBundle({ sourcesByMap: new Map([["a.map", []]]), allow }), /no bundled module at all/);
  throwsGate(
    () => evaluateBundle({ sourcesByMap: new Map([["a.map", ["../node_modules/react/index.js", "\u0000vite/preload-helper.js"]]]), allow }),
    /no first-party module/,
  );
});

test("source map files are read strictly and discovered recursively", () => {
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-"));
  try {
    mkdirSync(join(dir, "assets"));
    writeFileSync(join(dir, "assets", "index-abc.js.map"), JSON.stringify({ version: 3, sources: APP, mappings: "" }));
    writeFileSync(join(dir, "assets", "index-abc.js"), "// bundle");
    assert.deepEqual(walkMaps(dir), [join(dir, "assets", "index-abc.js.map")]);
    assert.deepEqual(readSources(join(dir, "assets", "index-abc.js.map")), APP);
    writeFileSync(join(dir, "bad.map"), "{");
    throwsGate(() => readSources(join(dir, "bad.map")), /not JSON/);
    writeFileSync(join(dir, "bad.map"), "[]");
    throwsGate(() => readSources(join(dir, "bad.map")), /not an object/);
    writeFileSync(join(dir, "bad.map"), JSON.stringify({ version: 3, mappings: "" }));
    throwsGate(() => readSources(join(dir, "bad.map")), /no sources array/);
    writeFileSync(join(dir, "bad.map"), JSON.stringify({ version: 3, sources: [1], mappings: "" }));
    throwsGate(() => readSources(join(dir, "bad.map")), /non-string source/);
    writeFileSync(join(dir, "bad.map"), JSON.stringify({ version: 3, sections: [] }));
    throwsGate(() => readSources(join(dir, "bad.map")), /index map/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("the CLI requires --dist, fails on an empty or missing directory, and judges a real layout end to end", () => {
  throwsGate(() => parseArgs([]), /--dist/);
  throwsGate(() => parseArgs(["--dist"]), /bad argument/);
  throwsGate(() => parseArgs(["--dist", "x", "--bogus", "1"]), /bad argument/);
  const dir = mkdtempSync(join(tmpdir(), "bundle-check-cli-"));
  try {
    const allowPath = join(dir, "allow.json");
    writeFileSync(allowPath, JSON.stringify(ALLOW_DOC));
    const dist = join(dir, "dist");
    mkdirSync(join(dist, "assets"), { recursive: true });
    const argv = ["--dist", dist, "--allowlist", allowPath, "--now", "2026-11-18T12:00:00Z"];
    throwsGate(() => quiet(() => main(argv)), /no source maps/);
    throwsGate(() => quiet(() => main(["--dist", join(dir, "missing"), "--allowlist", allowPath])), /cannot read/);
    writeFileSync(join(dist, "assets", "index.js.map"), JSON.stringify({ version: 3, sources: APP, mappings: "" }));
    quiet(() => main(argv));
    writeFileSync(
      join(dist, "assets", "vendor.js.map"),
      JSON.stringify({ version: 3, sources: ["../node_modules/picomatch/lib/picomatch.js"], mappings: "" }),
    );
    throwsGate(() => quiet(() => main(argv)), /allowlist premise/);
    throwsGate(() => quiet(() => main(["--dist", dist, "--allowlist", allowPath, "--now", "2027-01-01T00:00:00Z"])), /expired at/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
