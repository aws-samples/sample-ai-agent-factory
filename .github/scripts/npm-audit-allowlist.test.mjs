// Self-test for npm-audit-allowlist.mjs. Zero dependencies: `node --test npm-audit-allowlist.test.mjs`.
// Every case here is a way the gate could have passed something it must not (or failed something it must
// pass); keep this file growing with the gate.
import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  GateError,
  checkNpmResult,
  evaluate,
  ghsaOf,
  loadAllowlist,
  main,
  parseAllowlist,
  parseArgs,
  parseReport,
  resolveRoots,
  runNpmAudit,
} from "./npm-audit-allowlist.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const GHSA = "GHSA-fc5q-39xc-vjpf";
const FOREIGN = "GHSA-2222-3333-4444";
const URL_OK = `https://github.com/advisories/${GHSA}`;
const NOW = Date.parse("2026-11-18T12:00:00Z");

const advisory = (over = {}) => ({
  source: 1,
  name: "picomatch",
  dependency: "picomatch",
  title: "picomatch vulnerable to ReDoS",
  url: URL_OK,
  severity: "high",
  cwe: ["CWE-1333"],
  cvss: { score: 7.5, vectorString: null },
  range: "<=4.0.7",
  ...over,
});
const foreign = (over = {}) => advisory({ name: "evil", url: `https://github.com/advisories/${FOREIGN}`, title: "evil", ...over });
const MODERATE = advisory({ name: "minimatch", severity: "moderate", url: "https://github.com/advisories/GHSA-g3hv-75wg-m249" });
const entry = (name, severity, via, over = {}) => ({
  name,
  severity,
  isDirect: false,
  via,
  effects: [],
  range: "*",
  nodes: [`node_modules/${name}`],
  fixAvailable: false,
  ...over,
});
/** An npm `auditReportVersion: 2` report whose metadata counters match its entries. */
const report = (entries) => {
  const counters = { info: 0, low: 0, moderate: 0, high: 0, critical: 0, total: entries.length };
  for (const item of entries) counters[item.severity] += 1;
  return {
    auditReportVersion: 2,
    vulnerabilities: Object.fromEntries(entries.map((item) => [item.name, item])),
    metadata: { vulnerabilities: counters, dependencies: { prod: 1, dev: 1, optional: 0, peer: 0, peerOptional: 0, total: 2 } },
  };
};
const CLEAN = report([]);
const CHAIN = [
  entry("picomatch", "high", [advisory()]),
  entry("tinyglobby", "high", ["picomatch"]),
  entry("vite", "high", ["tinyglobby"]),
];
const ALLOW_DOC = {
  allow: [{ id: GHSA, package: "picomatch", reason: "a".repeat(40), added: "2026-11-18", expires: "2026-12-16T00:00:00Z" }],
};
const allow = parseAllowlist(ALLOW_DOC, NOW);
const run = (full, prod = CLEAN, { level = "high", allowMap = allow } = {}) =>
  evaluate({
    report: parseReport(JSON.stringify(full)),
    prodReport: parseReport(JSON.stringify(prod)),
    allow: allowMap,
    level,
  });
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

test("an allowlisted advisory exempts every package it explains, and nothing else is reported", () => {
  const { violations, exempt } = run(report(CHAIN));
  assert.deepEqual(violations, []);
  assert.deepEqual([...exempt.get(GHSA)].sort(), ["picomatch", "tinyglobby", "vite"]);
});

test("a clean report passes with no exemptions used", () => {
  const { violations, exempt } = run(CLEAN);
  assert.deepEqual(violations, []);
  assert.equal(exempt.size, 0);
});

test("diamonds and cycles that reach an advisory resolve; a rootless cycle fails", () => {
  const diamond = report([...CHAIN, entry("vitest", "high", ["tinyglobby", "picomatch"])]);
  assert.deepEqual(run(diamond).violations, []);
  const cyc = report([entry("a", "high", ["b", advisory()]), entry("b", "high", ["a"])]);
  assert.deepEqual(run(cyc).violations, []);
  const rootless = report([...CHAIN, entry("loop", "low", ["loop"])]);
  throwsGate(() => run(rootless), /resolves to no advisory/);
  const unknown = report([entry("vite", "critical", ["ghost-package"])]);
  throwsGate(() => run(unknown), /no vulnerability entry/);
});

test("a foreign advisory at the gate level is a violation, even when hidden under a low-severity package", () => {
  const result = run(report([entry("evil", "low", [foreign()])]));
  assert.equal(result.violations.length, 1);
  assert.match(result.violations[0], /GHSA-2222-3333-4444/);
});

test("a second copy of a root at a lower severity never hides the copy at the gate level (either order)", () => {
  // Same package and URL as the high foreign root, reported again as low: both copies are kept and judged.
  for (const roots of [
    [advisory(), foreign(), foreign({ severity: "low" })],
    [foreign({ severity: "low" }), foreign(), advisory()],
  ]) {
    const result = run(report([entry("picomatch", "high", roots)]));
    assert.equal(result.violations.length, 1, JSON.stringify(roots.map((r) => r.severity)));
    assert.match(result.violations[0], /GHSA-2222-3333-4444/);
  }
  // A lower-severity duplicate of the allowlisted root changes nothing either.
  const dup = run(report([entry("picomatch", "high", [advisory({ severity: "low" }), advisory()])]));
  assert.deepEqual(dup.violations, []);
  assert.ok(dup.exempt.get(GHSA).has("picomatch"));
});

test("npm's aggregate severity is never trusted: a lower aggregate over an exempt root passes, an unexplained higher one fails", () => {
  // Observed in real npm output: @typescript-eslint/utils reported "moderate" while its only via was a
  // "high" package. Such a package is exempt through the allowlisted root it reaches.
  const lowAggregate = report([...CHAIN, entry("@typescript-eslint/utils", "moderate", ["vite"])]);
  const ok = run(lowAggregate);
  assert.deepEqual(ok.violations, []);
  assert.ok(ok.exempt.get(GHSA).has("@typescript-eslint/utils"));
  // A package npm calls "high" whose roots are all moderate is a finding nothing here can explain.
  const unexplained = run(report([entry("minimatch", "high", [MODERATE])]));
  assert.equal(unexplained.violations.length, 1);
  assert.match(unexplained.violations[0], /no root advisory at or above high explains it/);
});

test("root advisories below the gate level are not violations", () => {
  assert.deepEqual(run(report([entry("minimatch", "moderate", [MODERATE])])).violations, []);
});

test("severity values are matched as own string keys only", () => {
  throwsGate(() => parseReport(JSON.stringify(report([entry("x", "constructor", [advisory()])]))), /unknown severity/);
  throwsGate(() => resolveRoots({ x: entry("x", "high", [advisory({ severity: "constructor" })]) }), /unknown severity/);
  throwsGate(() => resolveRoots({ x: entry("x", "high", [advisory({ severity: undefined })]) }), /unknown severity/);
});

test("every advisory must carry the exact canonical GitHub advisory URL, whatever its severity", () => {
  assert.equal(ghsaOf(advisory()), GHSA);
  for (const url of [
    `https://example.invalid/advisories/${GHSA}`,
    `junk/advisories/${GHSA}`,
    `http://github.com/advisories/${GHSA}`,
    `https://github.com:444/advisories/${GHSA}`,
    `https://user:pw@github.com/advisories/${GHSA}`,
    `https://GitHub.com/advisories/${GHSA}`,
    `https://github.com/x/../advisories/${GHSA}`,
    `https://github.com/advisories/./${GHSA}`,
    `https://github.com/advisories/${GHSA}/`,
    `https://github.com/advisories/${GHSA}?x=1`,
    `https://github.com/advisories/${GHSA}#frag`,
    `https://github.com/advisories/${GHSA}/extra`,
    `https://github.com/advisories/${GHSA.toUpperCase()}`,
    ` ${URL_OK}`,
    `${URL_OK}\n`,
  ]) {
    assert.equal(ghsaOf(advisory({ url })), null, url);
    // At the gate level, below it, and in the production report: the report is unusable, not "clean".
    throwsGate(() => run(report([entry("picomatch", "high", [advisory({ url })])])), /canonical GitHub advisory URL/);
    throwsGate(() => run(report([entry("picomatch", "low", [advisory({ url, severity: "low" })])])), /canonical GitHub advisory URL/);
    throwsGate(() => run(CLEAN, report([entry("picomatch", "low", [advisory({ url, severity: "low" })])])), /canonical GitHub advisory URL/);
  }
  assert.equal(ghsaOf(advisory({ url: undefined })), null);
  throwsGate(() => run(report([entry("picomatch", "high", [advisory({ url: undefined })])])), /has no url/);
  throwsGate(() => run(report([entry("picomatch", "high", [advisory({ url: 7 })])])), /has no url/);
});

test("the allowlist binds an advisory to one package", () => {
  const wrong = parseAllowlist({ allow: [{ ...ALLOW_DOC.allow[0], package: "vite" }] }, NOW);
  const result = run(report(CHAIN), CLEAN, { allowMap: wrong });
  assert.ok(result.violations.every((v) => /allowlisted for package vite, not picomatch/.test(v)));
  assert.equal(result.violations.length, 3);
});

test("the allowlist never applies to production dependencies", () => {
  const result = run(report(CHAIN), report([entry("picomatch", "high", [advisory()])]));
  assert.equal(result.violations.length, 1);
  assert.match(result.violations[0], /PRODUCTION/);
});

test("a production package reported at the gate level fails even when its roots are all below it", () => {
  for (const severity of ["high", "critical"]) {
    const result = run(report(CHAIN), report([entry("minimatch", severity, [MODERATE])]));
    assert.equal(result.violations.length, 1, severity);
    assert.match(result.violations[0], /in PRODUCTION dependencies but no root advisory at or above high explains it/);
  }
  // Below the gate on both counts: nothing to report.
  assert.deepEqual(run(report(CHAIN), report([entry("minimatch", "moderate", [MODERATE])])).violations, []);
  // Aggregate below the gate over a qualifying root: still a production violation.
  const hidden = run(report(CHAIN), report([entry("picomatch", "low", [advisory()])]));
  assert.equal(hidden.violations.length, 1);
  assert.match(hidden.violations[0], /reachable from PRODUCTION/);
});

test("incomplete, inconsistent or errored reports fail closed", () => {
  const good = report(CHAIN);
  throwsGate(() => parseReport("{"), /not JSON/);
  throwsGate(() => parseReport("[]"), /not an object/);
  throwsGate(() => parseReport(JSON.stringify({ error: { code: "ENOTFOUND" } })), /reported an error/);
  throwsGate(() => parseReport(JSON.stringify({ ...good, auditReportVersion: 1 })), /unsupported auditReportVersion/);
  throwsGate(() => parseReport(JSON.stringify({ ...good, vulnerabilities: [] })), /no vulnerabilities object/);
  throwsGate(() => parseReport(JSON.stringify({ ...good, metadata: {} })), /no metadata.vulnerabilities/);
  const lying = { ...CLEAN, metadata: { vulnerabilities: { info: 0, low: 0, moderate: 0, high: 1, critical: 0, total: 1 } } };
  throwsGate(() => parseReport(JSON.stringify(lying)), /metadata says high=1 but the report lists 0/);
  const missingTotal = { ...good, metadata: { vulnerabilities: { ...good.metadata.vulnerabilities, total: 2 } } };
  throwsGate(() => parseReport(JSON.stringify(missingTotal)), /metadata says total=2/);
  const noCounter = { ...good, metadata: { vulnerabilities: { high: 3, total: 3 } } };
  throwsGate(() => parseReport(JSON.stringify(noCounter)), /metadata.vulnerabilities.info is not a count/);
  const noVia = report([entry("x", "high", [])]);
  throwsGate(() => parseReport(JSON.stringify(noVia)), /has no via chain/);
});

test("npm's exit status must agree with the report", () => {
  const good = JSON.stringify(report(CHAIN));
  assert.ok(parseReport(good, 1));
  assert.ok(parseReport(JSON.stringify(CLEAN), 0));
  throwsGate(() => parseReport(JSON.stringify(CLEAN), 1), /contradictory report/);
  throwsGate(() => parseReport(good, 0), /contradictory report/);
});

test("npm process failures never read as a clean audit", () => {
  throwsGate(() => checkNpmResult({ error: new Error("spawnSync npm ETIMEDOUT"), status: null, signal: "SIGTERM" }), /could not run npm audit/);
  throwsGate(() => checkNpmResult({ status: null, signal: "SIGKILL", stdout: "" }), /terminated by signal/);
  throwsGate(() => checkNpmResult({ status: 2, stdout: "", stderr: "ERR" }), /exited 2/);
  throwsGate(() => checkNpmResult({ status: 0, stdout: undefined }), /no output/);
  assert.deepEqual(checkNpmResult({ status: 1, stdout: "{}" }), { stdout: "{}", status: 1 });
});

test("runNpmAudit: a failing, hanging or flooding npm is a failure, a clean one is read back", { skip: process.platform === "win32" }, () => {
  const dir = mkdtempSync(join(tmpdir(), "fake-npm-"));
  const fakeNpm = join(dir, "npm");
  writeFileSync(
    fakeNpm,
    [
      "#!/bin/sh",
      'case "$FAKE_NPM_MODE" in',
      "  fail) echo boom >&2; exit 2 ;;",
      "  clean) printf '%s' \"$FAKE_NPM_STDOUT\"; exit 0 ;;",
      "  hang) sleep 5; exit 0 ;;",
      "  flood) i=0; while [ $i -lt 400 ]; do printf '%0512d' 0; i=$((i+1)); done; exit 0 ;;",
      "esac",
      "exit 3",
      "",
    ].join("\n"),
  );
  chmodSync(fakeNpm, 0o755);
  const saved = { PATH: process.env.PATH, FAKE_NPM_MODE: process.env.FAKE_NPM_MODE, FAKE_NPM_STDOUT: process.env.FAKE_NPM_STDOUT };
  process.env.PATH = `${dir}:${process.env.PATH}`;
  try {
    process.env.FAKE_NPM_MODE = "fail";
    throwsGate(() => runNpmAudit(), /exited 2: boom/);
    process.env.FAKE_NPM_MODE = "clean";
    process.env.FAKE_NPM_STDOUT = JSON.stringify(CLEAN);
    const ok = runNpmAudit(["--omit=dev"]);
    assert.equal(ok.status, 0);
    assert.equal(parseReport(ok.stdout, 0).metadata.vulnerabilities.total, 0);
    process.env.FAKE_NPM_MODE = "hang";
    throwsGate(() => runNpmAudit([], { timeoutMs: 300 }), /ETIMEDOUT|terminated by signal/);
    process.env.FAKE_NPM_MODE = "flood";
    throwsGate(() => runNpmAudit([], { maxBuffer: 4096 }), /ENOBUFS/);
  } finally {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    rmSync(dir, { recursive: true, force: true });
  }
});

test("allowlist schema is strict and expiry is a UTC instant with a hard cutoff", () => {
  const base = ALLOW_DOC.allow[0];
  const doc = (over) => ({ allow: [{ ...base, ...over }] });
  throwsGate(() => parseAllowlist({ allow: {} }, NOW), /"allow" array/);
  throwsGate(() => parseAllowlist(doc({ id: [GHSA] }), NOW), /GHSA identifier string/);
  throwsGate(() => parseAllowlist(doc({ id: "GHSA-0000-1111-aaaa" }), NOW), /GHSA identifier string/);
  throwsGate(() => parseAllowlist(doc({ package: "" }), NOW), /package must name/);
  throwsGate(() => parseAllowlist(doc({ package: undefined }), NOW), /package must name/);
  throwsGate(() => parseAllowlist(doc({ reason: "too short" }), NOW), /written reason/);
  throwsGate(() => parseAllowlist(doc({ expires: "2026-11-31T00:00:00Z" }), NOW), /not a real calendar instant/);
  throwsGate(() => parseAllowlist(doc({ expires: "2026-12-16T00:00:00" }), NOW), /instant like/);
  throwsGate(() => parseAllowlist(doc({ expires: "2026-12-16" }), NOW), /instant like/);
  throwsGate(() => parseAllowlist(doc({ added: "2026-02-30" }), NOW), /not a real calendar date/);
  throwsGate(() => parseAllowlist({ allow: [base, { ...base }] }, NOW), /twice/);
  throwsGate(() => parseAllowlist(doc({}), Date.parse("2026-12-16T00:00:00Z")), /expired at/);
  throwsGate(() => parseAllowlist(doc({}), Date.parse("2027-01-01T00:00:00Z")), /expired at/);
  assert.equal(parseAllowlist(doc({}), Date.parse("2026-12-15T23:59:59Z")).size, 1);
});

test("the committed allowlist is valid for the gate as of its 'added' date", () => {
  const entries = loadAllowlist(resolve(here, "..", "npm-audit-allowlist.json"), NOW);
  assert.ok(entries.size >= 1);
  for (const item of entries.values()) assert.equal(typeof item.cutoff, "number");
});

test("CLI arguments are validated", () => {
  assert.deepEqual(parseArgs([]), { level: "high", allowlist: null, input: null, "prod-input": null, now: null });
  assert.equal(parseArgs(["--level", "critical"]).level, "critical");
  throwsGate(() => parseArgs(["--level"]), /bad argument/);
  throwsGate(() => parseArgs(["--bogus", "1"]), /bad argument/);
  throwsGate(() => parseArgs(["--level=high"]), /bad argument/);
  throwsGate(() => parseArgs(["high"]), /bad argument/);
  throwsGate(() => parseArgs(["--__proto__", "x"]), /bad argument/);
  throwsGate(() => parseArgs(["--level", "severe"]), /unknown --level/);
  throwsGate(() => parseArgs(["--input", "a.json"]), /must be given together/);
  throwsGate(() => parseArgs(["--prod-input", "b.json"]), /must be given together/);
});

test("the CLI runs end to end on report files, and fails on an expired entry or a production finding", () => {
  const dir = mkdtempSync(join(tmpdir(), "npm-audit-allowlist-"));
  try {
    const allowPath = join(dir, "allow.json");
    const fullPath = join(dir, "full.json");
    const prodPath = join(dir, "prod.json");
    writeFileSync(allowPath, JSON.stringify(ALLOW_DOC));
    writeFileSync(fullPath, JSON.stringify(report(CHAIN)));
    writeFileSync(prodPath, JSON.stringify(CLEAN));
    const argv = ["--allowlist", allowPath, "--input", fullPath, "--prod-input", prodPath];
    quiet(() => main([...argv, "--now", "2026-11-18T12:00:00Z"]));
    throwsGate(() => quiet(() => main([...argv, "--now", "2026-12-16T00:00:00Z"])), /expired at/);
    throwsGate(() => quiet(() => main([...argv, "--now", "not-a-date"])), /is not a date/);
    writeFileSync(prodPath, JSON.stringify(report([entry("picomatch", "high", [advisory()])])));
    throwsGate(() => quiet(() => main([...argv, "--now", "2026-11-18T12:00:00Z"])), /not allowlisted/);
    writeFileSync(prodPath, "{");
    throwsGate(() => quiet(() => main([...argv, "--now", "2026-11-18T12:00:00Z"])), /not JSON/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("real npm reports from each captured npm version parse, reconcile with their exit status, and resolve as expected", () => {
  const root = resolve(here, "fixtures", "npm-audit");
  const versions = readdirSync(root).sort();
  // The npm versions CI actually runs: Node 20 (frontend job) ships npm 10, Node 24 (docs job) ships npm 11.
  for (const major of ["npm-10.", "npm-11."]) assert.ok(versions.some((version) => version.startsWith(major)), `no captured ${major}x reports`);
  for (const version of versions) {
    for (const project of ["frontend", "docs-site"]) {
      const where = `${version}/${project}`;
      const full = readFileSync(join(root, version, `${project}-full.json`), "utf8");
      const prod = readFileSync(join(root, version, `${project}-prod.json`), "utf8");
      // npm exits 1 for the full report (findings) and 0 for the production report (none).
      const result = evaluate({ report: parseReport(full, 1), prodReport: parseReport(prod, 0), allow, level: "high" });
      assert.deepEqual(result.violations, [], where);
      assert.ok(result.exempt.get(GHSA).has("picomatch"), where);
      // Without the allowlist the same reports fail: the exemption is doing real work, not masking a no-op.
      const bare = evaluate({ report: parseReport(full, 1), prodReport: parseReport(prod, 0), allow: new Map(), level: "high" });
      assert.ok(bare.violations.length > 0, `${where} without allowlist`);
      assert.ok(bare.violations.every((v) => v.includes(GHSA)), `${where}: only the picomatch advisory is present`);
    }
  }
});
