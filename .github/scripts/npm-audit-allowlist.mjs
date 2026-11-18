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
 * The gate runs TWO audits and fails closed on both:
 *   1. The full audit (`npm audit --json`). Every package is resolved to the advisories that explain it
 *      by following `via` chains to the advisory objects (reachability, so diamonds and cycles that
 *      reach an advisory are fine, while a chain that reaches no advisory fails). Every advisory at or
 *      above <level> must be allowlisted by the exact GHSA id of its canonical GitHub advisory URL and
 *      for the exact package the allowlist names. A package's own reported severity is never used to
 *      skip its roots (npm's aggregate is not the maximum over the chain); a package reported at or
 *      above <level> whose roots are all below it is an unexplained finding and fails.
 *   2. The production audit (`npm audit --omit=dev --json`), to which the allowlist NEVER applies: an
 *      exemption is justified only while the advisory stays in build/test tooling, and this is what
 *      detects the premise changing.
 * A report that is not npm's complete `auditReportVersion: 2` shape, whose metadata counters do not match
 * its entries, that carries `error`, that does not parse, or whose `npm audit` exit status contradicts
 * its content is a FAILURE, never "zero vulnerabilities". An allowlist entry past its `expires` instant
 * (UTC) fails the run even if the advisory is gone; an unexpired entry whose advisory is no longer
 * reported only warns (remove it).
 *
 * Usage: node npm-audit-allowlist.mjs [--level high] [--allowlist <file>]
 *                                     [--input <audit.json> --prod-input <audit-omit-dev.json>] [--now <ISO>]
 * Exit 0 = gate passed. Exit 1 = violations, expired entries, or an unusable report.
 * Self-test: node --test npm-audit-allowlist.test.mjs (same directory).
 */
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const SEVERITIES = ["info", "low", "moderate", "high", "critical"];
const RANK = new Map(SEVERITIES.map((severity, index) => [severity, index]));
// GHSA ids use a 23-character alphabet (no 0/1/a/b/d/e/i/k/l/n/o/s/t/u/y/z).
export const GHSA_RE = /^GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}$/;
const ISO_INSTANT_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
export const NPM_TIMEOUT_MS = 180_000;
export const NPM_MAX_BUFFER = 64 * 1024 * 1024;

/** Any condition that must fail the gate. */
export class GateError extends Error {}

const isPlainObject = (value) =>
  value !== null && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype;
const isNonEmptyString = (value) => typeof value === "string" && value.length > 0;
const own = (object, key) => (Object.prototype.hasOwnProperty.call(object, key) ? object[key] : undefined);

export function rankOf(severity, where) {
  const rank = typeof severity === "string" ? RANK.get(severity) : undefined;
  if (rank === undefined) throw new GateError(`${where}: unknown severity ${JSON.stringify(severity)}`);
  return rank;
}

/** A canonical instant string that round-trips exactly (rejects e.g. 2026-11-31T00:00:00Z). */
function parseCanonicalInstant(value, where) {
  if (!isNonEmptyString(value) || !ISO_INSTANT_RE.test(value)) {
    throw new GateError(`${where} must be an instant like 2026-12-16T00:00:00Z`);
  }
  const millis = Date.parse(value);
  if (!Number.isFinite(millis) || new Date(millis).toISOString() !== value.replace(/Z$/, ".000Z")) {
    throw new GateError(`${where} is not a real calendar instant: ${value}`);
  }
  return millis;
}

function assertCanonicalDate(value, where) {
  if (!isNonEmptyString(value) || !ISO_DATE_RE.test(value)) throw new GateError(`${where} must be a date like 2026-11-18`);
  const millis = Date.parse(`${value}T00:00:00Z`);
  if (!Number.isFinite(millis) || new Date(millis).toISOString().slice(0, 10) !== value) {
    throw new GateError(`${where} is not a real calendar date: ${value}`);
  }
}

/** Validate the allowlist document and return Map<ghsaId, entry>; throws GateError on any defect or expiry. */
export function parseAllowlist(doc, nowMillis) {
  if (!isPlainObject(doc) || !Array.isArray(doc.allow)) throw new GateError('allowlist must be an object with an "allow" array');
  const entries = new Map();
  doc.allow.forEach((entry, index) => {
    const where = `allowlist entry ${index}`;
    if (!isPlainObject(entry)) throw new GateError(`${where} must be an object`);
    const { id, reason, expires, added } = entry;
    if (!isNonEmptyString(id) || !GHSA_RE.test(id)) throw new GateError(`${where}: id must be a GHSA identifier string`);
    if (!isNonEmptyString(entry.package)) throw new GateError(`${where} (${id}): package must name the vulnerable package`);
    if (typeof reason !== "string" || reason.trim().length < 20) throw new GateError(`${where} (${id}): needs a written reason`);
    const cutoff = parseCanonicalInstant(expires, `${where} (${id}): expires`);
    if (added !== undefined) assertCanonicalDate(added, `${where} (${id}): added`);
    if (entries.has(id)) throw new GateError(`allowlist lists ${id} twice`);
    entries.set(id, { id, package: entry.package, reason, expires, cutoff });
    if (nowMillis >= cutoff) {
      throw new GateError(
        `allowlist entry ${id} expired at ${expires}; re-check upstream for a patched release and either ` +
          "remove the entry (fixed) or renew it with a fresh review and a new expiry",
      );
    }
  });
  return entries;
}

export function loadAllowlist(path, nowMillis) {
  let doc;
  try {
    doc = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new GateError(`cannot read allowlist ${path}: ${error.message}`);
  }
  return parseAllowlist(doc, nowMillis);
}

/** Interpret a spawnSync result for `npm audit --json`; returns {stdout, status} or throws GateError. */
export function checkNpmResult(result) {
  if (result.error) throw new GateError(`could not run npm audit: ${result.error.message}`);
  if (result.signal) throw new GateError(`npm audit was terminated by signal ${result.signal} (timeout ${NPM_TIMEOUT_MS} ms?)`);
  // npm exits 1 when vulnerabilities exist: expected. Anything else is a real error.
  if (result.status !== 0 && result.status !== 1) throw new GateError(`npm audit exited ${result.status}: ${result.stderr}`);
  if (typeof result.stdout !== "string") throw new GateError("npm audit produced no output");
  return { stdout: result.stdout, status: result.status };
}

export function runNpmAudit(extraArgs = []) {
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const result = spawnSync(npm, ["audit", "--json", ...extraArgs], {
    encoding: "utf8",
    timeout: NPM_TIMEOUT_MS,
    maxBuffer: NPM_MAX_BUFFER,
    env: { ...process.env, NO_COLOR: "1" },
  });
  return checkNpmResult(result);
}

/**
 * Parse and validate a complete npm `auditReportVersion: 2` report. `exitStatus` is npm's exit status when
 * known (null when reading a file): a clean report from exit 1, or a report with findings from exit 0,
 * is contradictory and fails.
 */
export function parseReport(text, exitStatus = null) {
  let report;
  try {
    report = JSON.parse(text);
  } catch (error) {
    throw new GateError(`npm audit output is not JSON (${error.message}); registry unreachable or truncated output`);
  }
  if (!isPlainObject(report)) throw new GateError("npm audit output is not an object");
  if (own(report, "error") !== undefined) throw new GateError(`npm audit reported an error: ${JSON.stringify(report.error)}`);
  if (report.auditReportVersion !== 2) throw new GateError(`unsupported auditReportVersion ${JSON.stringify(report.auditReportVersion)}`);
  const vulnerabilities = own(report, "vulnerabilities");
  if (!isPlainObject(vulnerabilities)) throw new GateError("report has no vulnerabilities object");
  const metadata = own(report, "metadata");
  const counters = isPlainObject(metadata) ? own(metadata, "vulnerabilities") : undefined;
  if (!isPlainObject(counters)) throw new GateError("report has no metadata.vulnerabilities");

  const counted = Object.fromEntries(SEVERITIES.map((severity) => [severity, 0]));
  for (const [name, entry] of Object.entries(vulnerabilities)) {
    if (!isPlainObject(entry)) throw new GateError(`vulnerability entry "${name}" is not an object`);
    if (entry.name !== undefined && entry.name !== name) throw new GateError(`vulnerability entry "${name}" names ${JSON.stringify(entry.name)}`);
    rankOf(entry.severity, `vulnerability entry "${name}"`);
    if (!Array.isArray(entry.via) || entry.via.length === 0) throw new GateError(`vulnerability entry "${name}" has no via chain`);
    counted[entry.severity] += 1;
  }
  const total = Object.keys(vulnerabilities).length;
  for (const key of [...SEVERITIES, "total"]) {
    const value = own(counters, key);
    if (!Number.isInteger(value) || value < 0) throw new GateError(`metadata.vulnerabilities.${key} is not a count: ${JSON.stringify(value)}`);
    const expected = key === "total" ? total : counted[key];
    if (value !== expected) {
      throw new GateError(`report metadata says ${key}=${value} but the report lists ${expected}; incomplete or inconsistent report`);
    }
  }
  if (exitStatus !== null && (exitStatus === 1) !== total > 0) {
    throw new GateError(`npm audit exited ${exitStatus} but the report lists ${total} vulnerable package(s); contradictory report`);
  }
  return report;
}

/** The GHSA id of an advisory's canonical GitHub advisory URL, or null for anything else. */
export function ghsaOf(advisory) {
  let url;
  try {
    url = new URL(String(advisory.url));
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.hostname !== "github.com" || url.search || url.hash) return null;
  const match = /^\/advisories\/(GHSA-[^/]+)$/.exec(url.pathname);
  return match && GHSA_RE.test(match[1]) ? match[1] : null;
}

/**
 * Map<packageName, advisory[]>: every advisory object reachable from each vulnerable package through its
 * `via` chains (strings name other vulnerable packages, objects are advisories). Diamonds and cycles that
 * reach an advisory are fine; a package that reaches no advisory, or a via that names an unknown package,
 * is a GateError. Every advisory object is validated.
 */
export function resolveRoots(vulnerabilities) {
  const roots = new Map();
  for (const start of Object.keys(vulnerabilities)) {
    const found = new Map();
    const visited = new Set();
    const stack = [start];
    while (stack.length > 0) {
      const name = stack.pop();
      if (visited.has(name)) continue;
      visited.add(name);
      const entry = own(vulnerabilities, name);
      if (!isPlainObject(entry)) throw new GateError(`"${name}" is named in a via chain but has no vulnerability entry`);
      for (const via of entry.via) {
        if (typeof via === "string") {
          stack.push(via);
        } else if (isPlainObject(via)) {
          if (!isNonEmptyString(via.name)) throw new GateError(`an advisory under "${name}" has no package name`);
          rankOf(via.severity, `advisory ${via.url || via.source} under "${name}"`);
          if (!isNonEmptyString(via.url)) throw new GateError(`advisory for ${via.name} under "${name}" has no url`);
          found.set(`${via.name}\u0000${via.url}`, via);
        } else {
          throw new GateError(`"${name}" has an unrecognised via entry ${JSON.stringify(via)}`);
        }
      }
    }
    if (found.size === 0) throw new GateError(`"${start}" resolves to no advisory (rootless via chain or cycle)`);
    roots.set(start, [...found.values()]);
  }
  return roots;
}

/**
 * Evaluate the full report (with the allowlist) and the production report (without it).
 * Returns { violations: string[], exempt: Map<ghsaId, Set<packageName>> }.
 */
export function evaluate({ report, prodReport, allow, level }) {
  const threshold = rankOf(level, "--level");
  const violations = [];
  const exempt = new Map();

  const rootsByPackage = resolveRoots(report.vulnerabilities);
  for (const [name, entry] of Object.entries(report.vulnerabilities)) {
    const roots = rootsByPackage.get(name);
    const packageRank = rankOf(entry.severity, `vulnerability entry "${name}"`);
    const maxRootRank = Math.max(...roots.map((advisory) => rankOf(advisory.severity, `advisory under "${name}"`)));
    // npm's per-package severity is not the maximum over its chain (observed: a package reported
    // "moderate" whose only via is a "high" package), so it is never trusted to skip roots. The one
    // thing it must not do is exceed every root at the gate: that would be a finding plain `npm audit`
    // reports that nothing here can explain or exempt, so it fails closed.
    if (packageRank >= threshold && maxRootRank < threshold) {
      violations.push(
        `${name}: reported ${entry.severity} but no root advisory at or above ${level} explains it (roots reach ${SEVERITIES[maxRootRank]})`,
      );
    }
    // Every root at or above the gate is judged, whatever the package's own severity says.
    for (const advisory of roots) {
      if (rankOf(advisory.severity, `advisory under "${name}"`) < threshold) continue;
      const ghsa = ghsaOf(advisory);
      const label = `${advisory.name}@${advisory.range ?? "*"} ${advisory.severity} ${ghsa || advisory.url}`;
      if (!ghsa) {
        violations.push(`${name}: root advisory without a canonical GitHub advisory URL cannot be allowlisted: ${label}`);
        continue;
      }
      const allowed = allow.get(ghsa);
      if (!allowed) {
        violations.push(`${name}: ${label} - ${advisory.title ?? "(no title)"}`);
        continue;
      }
      if (allowed.package !== advisory.name) {
        violations.push(`${name}: ${ghsa} is allowlisted for package ${allowed.package}, not ${advisory.name}`);
        continue;
      }
      if (!exempt.has(ghsa)) exempt.set(ghsa, new Set());
      exempt.get(ghsa).add(name);
    }
  }

  // Production dependencies: no exemptions. An allowlisted advisory is tolerated only in build tooling.
  const prodRoots = resolveRoots(prodReport.vulnerabilities);
  for (const [name, roots] of prodRoots) {
    for (const advisory of roots) {
      if (rankOf(advisory.severity, `production advisory under "${name}"`) < threshold) continue;
      violations.push(
        `${name}: ${advisory.name} ${advisory.severity} ${ghsaOf(advisory) || advisory.url} is reachable from PRODUCTION ` +
          "dependencies; the allowlist never applies there",
      );
    }
  }
  return { violations, exempt };
}

function parseArgs(argv) {
  const args = { level: "high", allowlist: null, input: null, "prod-input": null, now: null };
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i];
    const value = argv[i + 1];
    const name = typeof key === "string" && key.startsWith("--") ? key.slice(2) : null;
    if (name === null || !Object.prototype.hasOwnProperty.call(args, name) || value === undefined) {
      throw new GateError(`bad argument ${JSON.stringify(key)}`);
    }
    args[name] = value;
  }
  if (!RANK.has(args.level)) throw new GateError(`unknown --level ${JSON.stringify(args.level)}`);
  if ((args.input === null) !== (args["prod-input"] === null)) {
    throw new GateError("--input and --prod-input must be given together (full audit and --omit=dev audit)");
  }
  return args;
}

export function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  const nowMillis = args.now ? Date.parse(args.now) : Date.now();
  if (!Number.isFinite(nowMillis)) throw new GateError(`--now ${JSON.stringify(args.now)} is not a date`);
  const here = dirname(fileURLToPath(import.meta.url));
  const allow = loadAllowlist(resolve(args.allowlist || resolve(here, "..", "npm-audit-allowlist.json")), nowMillis);

  let report;
  let prodReport;
  if (args.input) {
    report = parseReport(readFileSync(args.input, "utf8"));
    prodReport = parseReport(readFileSync(args["prod-input"], "utf8"));
  } else {
    const full = runNpmAudit();
    report = parseReport(full.stdout, full.status);
    const prod = runNpmAudit(["--omit=dev"]);
    prodReport = parseReport(prod.stdout, prod.status);
  }

  const { violations, exempt } = evaluate({ report, prodReport, allow, level: args.level });
  console.log(
    `npm-audit-allowlist: full audit ${JSON.stringify(report.metadata.vulnerabilities)}; ` +
      `production audit ${JSON.stringify(prodReport.metadata.vulnerabilities)}; gate level ${args.level}`,
  );
  for (const [ghsa, packages] of exempt) {
    const entry = allow.get(ghsa);
    console.log(
      `npm-audit-allowlist: EXEMPT ${ghsa} (${entry.package}) until ${entry.expires}: ${[...packages].sort().join(", ")}\n` +
        `  reason: ${entry.reason}`,
    );
  }
  for (const ghsa of allow.keys()) {
    if (!exempt.has(ghsa)) console.warn(`npm-audit-allowlist: WARNING: allowlisted ${ghsa} is no longer reported; remove the entry`);
  }
  if (violations.length > 0) {
    for (const violation of violations) console.error(`npm-audit-allowlist: VIOLATION ${violation}`);
    throw new GateError(`${violations.length} vulnerability path(s) at or above ${args.level} are not allowlisted`);
  }
  console.log("npm-audit-allowlist: PASS");
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  try {
    main();
  } catch (error) {
    if (error instanceof GateError) {
      console.error(`npm-audit-allowlist: FAIL: ${error.message}`);
      process.exit(1);
    }
    throw error;
  }
}
