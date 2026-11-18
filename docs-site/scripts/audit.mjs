import { execFileSync } from 'node:child_process';

/**
 * The docs build is a static, prerendered site. Its development toolchain currently
 * depends on picomatch through Vite/Vitest, which has no patched release for the
 * single-branch repeated-extglob advisory. Do not suppress any other advisory.
 * Remove this exception when a patched picomatch release is available, or when
 * the exception expires and the dependency path has been reviewed again.
 */
const EXCEPTION = {
  id: 'GHSA-fc5q-39xc-vjpf',
  expires: '2027-01-31',
};

if (Date.now() > Date.parse(`${EXCEPTION.expires}T23:59:59Z`)) {
  console.error(`The audit exception for ${EXCEPTION.id} expired on ${EXCEPTION.expires}. Review the dependency before building.`);
  process.exit(1);
}

/** @typedef {{ via?: Array<string | { url?: string }>, effects?: string[] }} AuditVulnerability */
/** @type {{ vulnerabilities?: Record<string, AuditVulnerability> }} */
let report;
try {
  const output = execFileSync('npm', ['audit', '--audit-level=high', '--json'], {
    cwd: process.cwd(),
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    maxBuffer: 16 * 1024 * 1024,
  });
  report = JSON.parse(output);
} catch (error) {
  if (typeof error !== 'object' || error === null || !('stdout' in error) || typeof error.stdout !== 'string') throw error;
  report = JSON.parse(error.stdout);
}

const advisoryUrl = `https://github.com/advisories/${EXCEPTION.id}`;
const vulnerabilities = Object.entries(report.vulnerabilities ?? {});
const unexpected = vulnerabilities.filter(([name]) => {
  const seen = new Set();
  /** @returns {boolean} */
  function reachesAdvisory(/** @type {string} */ packageName) {
    if (packageName === 'picomatch') return advisoryUrl.endsWith(EXCEPTION.id);
    if (seen.has(packageName)) return false;
    seen.add(packageName);
    const vulnerability = report.vulnerabilities?.[packageName];
    if (!vulnerability) return false;
    return (Array.isArray(vulnerability.via) ? vulnerability.via : []).some(
      (/** @type {string | { url?: string }} */ source) => typeof source === 'string' && reachesAdvisory(source),
    );
  }
  return !reachesAdvisory(name);
});

if (unexpected.length > 0) {
  console.error(`Dependency audit failed: ${unexpected.length} unexpected high-severity finding(s) remain.`);
  process.exit(1);
}

console.log(`Dependency audit passed: all high-severity findings addressed except ${EXCEPTION.id} in picomatch, whose upstream release has no fix (review expires ${EXCEPTION.expires}).`);
