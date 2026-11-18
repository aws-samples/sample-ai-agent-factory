#!/usr/bin/env node
/**
 * `npm audit --audit-level=<level>` with a narrow, EXPIRING advisory allowlist. Zero dependencies.
 *
 * Why this exists: an advisory with no patched release (upstream fix unmerged) blocks the audit gate for
 * every PR, and the only alternatives were to stop auditing build/dev dependencies (`--omit=dev`) or to
 * add an audit-helper package. Both were refused. This keeps the FULL audit (runtime + build tooling)
 * and exempts exactly the advisories listed in the allowlist file, each with a written reason and an
 * expiry, so the exception is re-examined instead of forgotten.
 *
 * Behaviour (fail-closed everywhere):
 *   - Runs `npm audit --json` in the current directory (or reads `--input <file>` for tests).
 *   - Refuses anything but npm's `auditReportVersion: 2` shape, a report carrying `error`, or JSON that
 *     does not parse: a report we cannot read is a FAILURE, never "zero vulnerabilities".
 *   - For every package at or above <level>, resolves its ROOT advisories by following `via` chains
 *     (strings name other vulnerable packages; objects are advisories), with cycle protection. A package
 *     passes only if every root advisory at or above <level> is allowlisted by its exact GHSA id taken
 *     from the advisory URL. An unexplained severity, an unresolvable `via`, or an advisory without a
 *     GHSA id fails.
 *   - An allowlist entry past its `expires` instant (UTC) fails the run even if the advisory is gone,
 *     so the exception cannot outlive its review. An UNEXPIRED entry whose advisory is no longer
 *     reported only warns (remove it).
 *
 * Usage: node npm-audit-allowlist.mjs [--level high] [--allowlist <file>] [--input <audit.json>] [--now <ISO>]
 * Exit 0 = gate passed. Exit 1 = violations, expired entries, or an unreadable report.
 */
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SEVERITY_RANK = { info: 0, low: 1, moderate: 2, high: 3, critical: 4 };
// GHSA ids use a 23-character alphabet (no 0/1/a/b/d/e/i/k/l/n/o/s/t/u/y/z).
const GHSA_RE = /^GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}$/;
const NPM_TIMEOUT_MS = 180_000;
const NPM_MAX_BUFFER = 64 * 1024 * 1024;

function fail(message) {
  console.error(`npm-audit-allowlist: FAIL: ${message}`);
  process.exit(1);
}

function parseArgs(argv) {
  const args = { level: "high", allowlist: null, input: null, now: null };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    const name = key.startsWith("--") ? key.slice(2) : null;
    if (name === null || !(name in args) || value === undefined) fail(`bad argument ${JSON.stringify(key)}`);
    args[name] = value;
    i += 1;
  }
  if (!(args.level in SEVERITY_RANK)) fail(`unknown --level ${JSON.stringify(args.level)}`);
  return args;
}

function loadAllowlist(path, now) {
  let doc;
  try {
    doc = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    fail(`cannot read allowlist ${path}: ${error.message}`);
  }
  if (!doc || !Array.isArray(doc.allow)) fail(`allowlist ${path} must be an object with an "allow" array`);
  const entries = new Map();
  for (const entry of doc.allow) {
    if (!entry || typeof entry !== "object") fail("allowlist entries must be objects");
    const { id, reason, expires } = entry;
    if (!GHSA_RE.test(String(id))) fail(`allowlist id ${JSON.stringify(id)} is not a GHSA identifier`);
    if (typeof reason !== "string" || reason.trim().length < 20) fail(`allowlist ${id} needs a written reason`);
    const cutoff = Date.parse(String(expires));
    if (!Number.isFinite(cutoff) || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(String(expires))) {
      fail(`allowlist ${id} needs an "expires" instant like 2026-12-16T00:00:00Z`);
    }
    if (entries.has(id)) fail(`allowlist lists ${id} twice`);
    entries.set(id, { ...entry, cutoff });
    if (now >= cutoff) {
      fail(
        `allowlist entry ${id} expired at ${expires}; re-check upstream for a patched release and either ` +
          "remove the entry (fixed) or renew it with a fresh review and a new expiry",
      );
    }
  }
  return entries;
}

function runNpmAudit() {
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const result = spawnSync(npm, ["audit", "--json"], {
    encoding: "utf8",
    timeout: NPM_TIMEOUT_MS,
    maxBuffer: NPM_MAX_BUFFER,
    env: { ...process.env, NO_COLOR: "1" },
  });
  if (result.error) fail(`could not run npm audit: ${result.error.message}`);
  if (result.signal) fail(`npm audit was terminated by signal ${result.signal} (timeout ${NPM_TIMEOUT_MS} ms?)`);
  // npm exits 1 when vulnerabilities exist: expected. Anything else is a real error.
  if (result.status !== 0 && result.status !== 1) fail(`npm audit exited ${result.status}: ${result.stderr}`);
  return result.stdout;
}

function parseReport(text) {
  let report;
  try {
    report = JSON.parse(text);
  } catch (error) {
    fail(`npm audit output is not JSON (${error.message}); registry unreachable or truncated output`);
  }
  if (!report || typeof report !== "object") fail("npm audit output is not an object");
  if (report.error) fail(`npm audit reported an error: ${JSON.stringify(report.error)}`);
  if (report.auditReportVersion !== 2) fail(`unsupported auditReportVersion ${JSON.stringify(report.auditReportVersion)}`);
  if (!report.vulnerabilities || typeof report.vulnerabilities !== "object") fail("report has no vulnerabilities object");
  if (!report.metadata || !report.metadata.vulnerabilities) fail("report has no metadata.vulnerabilities");
  return report;
}

function ghsaOf(advisory) {
  const match = /\/advisories\/(GHSA-[^/?#]+)$/.exec(String(advisory.url || ""));
  return match && GHSA_RE.test(match[1]) ? match[1] : null;
}

/** Root advisory objects reachable from `name` through `via` chains; throws on anything unresolvable. */
function rootAdvisories(name, vulnerabilities, seen = new Set()) {
  if (seen.has(name)) return [];
  seen.add(name);
  const entry = vulnerabilities[name];
  if (!entry) throw new Error(`"${name}" is named in a via chain but has no vulnerability entry`);
  if (!Array.isArray(entry.via) || entry.via.length === 0) throw new Error(`"${name}" has no via chain`);
  const roots = [];
  for (const via of entry.via) {
    if (typeof via === "string") roots.push(...rootAdvisories(via, vulnerabilities, seen));
    else if (via && typeof via === "object") roots.push(via);
    else throw new Error(`"${name}" has an unrecognised via entry ${JSON.stringify(via)}`);
  }
  return roots;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const now = args.now ? Date.parse(args.now) : Date.now();
  if (!Number.isFinite(now)) fail(`--now ${JSON.stringify(args.now)} is not a date`);
  const here = dirname(fileURLToPath(import.meta.url));
  const allowlistPath = resolve(args.allowlist || resolve(here, "..", "npm-audit-allowlist.json"));
  const allow = loadAllowlist(allowlistPath, now);

  const report = parseReport(args.input ? readFileSync(args.input, "utf8") : runNpmAudit());
  const threshold = SEVERITY_RANK[args.level];
  const violations = [];
  const matched = new Map(); // ghsa -> packages exempted through it

  for (const [name, entry] of Object.entries(report.vulnerabilities)) {
    const packageRank = SEVERITY_RANK[entry.severity];
    if (packageRank === undefined) {
      violations.push(`${name}: unknown severity ${JSON.stringify(entry.severity)}`);
      continue;
    }
    if (packageRank < threshold) continue;
    let roots;
    try {
      roots = rootAdvisories(name, report.vulnerabilities);
    } catch (error) {
      violations.push(`${name}: ${error.message}`);
      continue;
    }
    const gating = roots.filter((advisory) => (SEVERITY_RANK[advisory.severity] ?? 99) >= threshold);
    if (gating.length === 0) {
      violations.push(`${name}: reported ${entry.severity} but no root advisory at or above ${args.level} explains it`);
      continue;
    }
    for (const advisory of gating) {
      const ghsa = ghsaOf(advisory);
      const label = `${advisory.name}@${advisory.range} ${advisory.severity} ${ghsa || advisory.url || advisory.source}`;
      if (!ghsa) {
        violations.push(`${name}: root advisory without a GHSA id cannot be allowlisted: ${label}`);
        continue;
      }
      const allowed = allow.get(ghsa);
      if (!allowed) {
        violations.push(`${name}: ${label} - ${advisory.title}`);
        continue;
      }
      if (allowed.package && allowed.package !== advisory.name) {
        violations.push(`${name}: ${ghsa} is allowlisted for package ${allowed.package}, not ${advisory.name}`);
        continue;
      }
      if (!matched.has(ghsa)) matched.set(ghsa, new Set());
      matched.get(ghsa).add(name);
    }
  }

  const totals = report.metadata.vulnerabilities;
  console.log(`npm-audit-allowlist: npm audit totals ${JSON.stringify(totals)}; gate level ${args.level}`);
  for (const [ghsa, packages] of matched) {
    const entry = allow.get(ghsa);
    console.log(
      `npm-audit-allowlist: EXEMPT ${ghsa} (${entry.package || "any package"}) until ${entry.expires}: ` +
        `${[...packages].sort().join(", ")}\n  reason: ${entry.reason}`,
    );
  }
  for (const ghsa of allow.keys()) {
    if (!matched.has(ghsa)) {
      console.warn(`npm-audit-allowlist: WARNING: allowlisted ${ghsa} is no longer reported; remove the entry`);
    }
  }
  if (violations.length > 0) {
    for (const violation of violations) console.error(`npm-audit-allowlist: VIOLATION ${violation}`);
    fail(`${violations.length} vulnerability path(s) at or above ${args.level} are not allowlisted`);
  }
  console.log("npm-audit-allowlist: PASS");
}

main();
