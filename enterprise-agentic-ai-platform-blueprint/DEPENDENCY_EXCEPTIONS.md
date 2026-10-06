# Dependency exceptions

`npm audit --package-lock-only` is expected to report **exactly one finding** in this
package. Everything else must be zero. If you see anything beyond the entry below,
it is new and it is yours to fix.

```
1 high severity vulnerability
```

Each exception records what was accepted, why it cannot be fixed here, what the
real exposure is, and the condition that retires the exception. Review on every
`aws-cdk-lib` bump.

---

## SEC-032 — `brace-expansion` 5.0.9 bundled inside `aws-cdk-lib`

| | |
|---|---|
| **Status** | Accepted, not fixable in this repository |
| **Advisories** | [GHSA-qhr7-859c-m2p7](https://github.com/advisories/GHSA-qhr7-859c-m2p7) / CVE-2026-102278 (high, fixed 5.0.11)<br>[GHSA-6j4f-fj2g-mc7p](https://github.com/advisories/GHSA-6j4f-fj2g-mc7p) / CVE-2026-102276 (high, fixed 5.0.10)<br>[GHSA-q2hr-2g5m-vwhr](https://github.com/advisories/GHSA-q2hr-2g5m-vwhr) / CVE-2026-102277 (medium, fixed 5.0.12) |
| **Installed** | `brace-expansion` 5.0.9, at `node_modules/aws-cdk-lib/node_modules/brace-expansion` |
| **Accepted on** | 2026-10-06, against `aws-cdk-lib` 2.272.0 |

### Why it cannot be fixed here

The vulnerable copy ships **inside the `aws-cdk-lib` tarball** as a bundled
dependency. Its lockfile entry carries `"inBundle": true` and no `resolved` URL:

```json
"node_modules/aws-cdk-lib/node_modules/brace-expansion": {
  "version": "5.0.9",
  "inBundle": true,
  ...
}
```

npm `overrides` do not apply to bundled dependencies. This was tested, not assumed:
a scoped `{"aws-cdk-lib": {"brace-expansion": "^5.0.12"}}` override was added, npm
re-installed, and the nested copy stayed at 5.0.9. The override was removed rather
than left in place as dead configuration.

`aws-cdk-lib` 2.272.0 is the latest published release, so there is no version to
upgrade to. Only an upstream `aws-cdk-lib` release can clear this.

Do **not** attempt to work around it with a `postinstall` patch. That would make
the lockfile describe a tree that is not what gets installed, which is a worse
property for a reference implementation than a documented accepted finding.

### Actual exposure

All three advisories are denial of service through brace-pattern expansion: two by
uncontrolled recursion causing stack exhaustion, one by quadratic-time expansion
burning CPU. None is a code-execution or information-disclosure issue.

Inside `aws-cdk-lib`, `brace-expansion` is reached through the glob matching used for
asset discovery during synthesis. That means:

- it executes at **`cdk synth` time, on a developer machine or in the build pipeline**,
  never in deployed infrastructure;
- the input is the **project's own glob patterns**, authored by whoever is running the
  synth, so triggering it requires supplying a hostile pattern to your own build;
- no trust or privilege boundary is crossed, and no deployed workload is reachable
  through it.

The worst realistic outcome is a build that hangs or crashes. That is tolerable for a
build-time toolchain dependency with no downstream fix available.

### Retirement condition

Clear this exception as soon as an `aws-cdk-lib` release bundles `brace-expansion`
5.0.12 or later. To check:

```bash
npm view aws-cdk-lib version
npm install aws-cdk-lib@latest
npm ls brace-expansion --all | grep -A1 aws-cdk-lib
npm audit --package-lock-only
```

Remember that an `aws-cdk-lib` bump also requires the `aws-cdk` CLI to move with it:
the library emits a cloud assembly schema the pinned CLI must be able to read, or
every `cdk synth` fails with `You need at least CLI version ... to read this manifest`.

---

## Resolved, kept for the record

### `sprintf-js` — fixed 2026-10-06, no longer an exception

Twenty moderate findings previously fanned out from a single root: `sprintf-js`, whose
advisory range is `*` with no patched release in existence. It arrived through jest's
coverage tooling:

```
jest -> babel-jest -> babel-plugin-istanbul -> @istanbuljs/load-nyc-config
     -> js-yaml@3.x -> argparse@1.x -> sprintf-js
```

It was removed from the tree entirely by pinning `argparse` forward:

```json
"overrides": { "argparse": "^2.0.1" }
```

`argparse` 2.x dropped the `sprintf-js` dependency, so the whole chain disappears.
This is safe because `js-yaml` 3.x requires `argparse` **only in its CLI binary**
(`bin/js-yaml.js`); the library code never imports it, and nothing in this repository
invokes that binary. `@istanbuljs/load-nyc-config` does not use `argparse` directly
at all, and only parses YAML when an `.nycrc.yml` or `.nycrc.yaml` file exists, which
this package does not have.

Verified after the override with `npm run build`, `npm run lint`, `npm test`
(63 suites, 759 tests), `npx jest --coverage` to exercise the coverage path that
pulls `load-nyc-config` in, and `npm run synth`. All passed.
