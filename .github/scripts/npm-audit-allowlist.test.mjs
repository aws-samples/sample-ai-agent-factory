// Self-test for npm-audit-allowlist.mjs. Zero dependencies: `node --test npm-audit-allowlist.test.mjs`.
// Every case here is a way the gate could have passed something it must not (or failed something it must
// pass); keep this file growing with the gate.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  GateError,
  checkNpmResult,
  evaluate,
  ghsaOf,
  parseAllowlist,
  parseReport,
  resolveRoots,
} from "./npm-audit-allowlist.mjs";

const GHSA = "GHSA-fc5q-39xc-vjpf";
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
  const foreign = advisory({ name: "evil", url: "https://github.com/advisories/GHSA-2222-3333-4444" });
  const result = run(report([entry("evil", "low", [foreign])]));
  assert.equal(result.violations.length, 1);
  assert.match(result.violations[0], /GHSA-2222-3333-4444/);
});

test("npm's aggregate severity is never trusted: a lower aggregate over an exempt root passes, an unexplained higher one fails", () => {
  // Observed in real npm output: @typescript-eslint/utils reported "moderate" while its only via was a
  // "high" package. Such a package is exempt through the allowlisted root it reaches.
  const lowAggregate = report([...CHAIN, entry("@typescript-eslint/utils", "moderate", ["vite"])]);
  const ok = run(lowAggregate);
  assert.deepEqual(ok.violations, []);
  assert.ok(ok.exempt.get(GHSA).has("@typescript-eslint/utils"));
  // A package npm calls "high" whose roots are all moderate is a finding nothing here can explain.
  const moderate = advisory({ name: "minimatch", severity: "moderate", url: "https://github.com/advisories/GHSA-g3hv-75wg-m249" });
  const unexplained = run(report([entry("minimatch", "high", [moderate])]));
  assert.equal(unexplained.violations.length, 1);
  assert.match(unexplained.violations[0], /no root advisory at or above high explains it/);
});

test("root advisories below the gate level are not violations", () => {
  const moderate = advisory({ name: "minimatch", severity: "moderate", url: "https://github.com/advisories/GHSA-g3hv-75wg-m249" });
  assert.deepEqual(run(report([entry("minimatch", "moderate", [moderate])])).violations, []);
});

test("severity values are matched as own string keys only", () => {
  throwsGate(() => parseReport(JSON.stringify(report([entry("x", "constructor", [advisory()])]))), /unknown severity/);
  throwsGate(() => resolveRoots({ x: entry("x", "high", [advisory({ severity: "constructor" })]) }), /unknown severity/);
  throwsGate(() => resolveRoots({ x: entry("x", "high", [advisory({ severity: undefined })]) }), /unknown severity/);
});

test("advisory identity requires the canonical GitHub advisory URL", () => {
  assert.equal(ghsaOf(advisory()), GHSA);
  for (const url of [
    `https://example.invalid/advisories/${GHSA}`,
    `junk/advisories/${GHSA}`,
    `http://github.com/advisories/${GHSA}`,
    `${URL_OK}?x=1`,
    `${URL_OK}#frag`,
    `https://github.com/advisories/${GHSA}/extra`,
  ]) {
    assert.equal(ghsaOf(advisory({ url })), null, url);
    const result = run(report([entry("picomatch", "high", [advisory({ url })])]));
    assert.ok(result.violations.some((v) => /cannot be allowlisted/.test(v)), url);
  }
  // No URL at all is not even a well-formed advisory: the report is rejected outright.
  assert.equal(ghsaOf(advisory({ url: undefined })), null);
  throwsGate(() => run(report([entry("picomatch", "high", [advisory({ url: undefined })])])), /has no url/);
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
