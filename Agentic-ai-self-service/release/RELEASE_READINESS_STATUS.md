# Release-Readiness Status

This file records the most recent production-readiness certification of the Self-Service
platform and what has changed since. It is maintained alongside `CHANGELOG.md`; the mechanics
of a certification run are in [`release/README.md`](README.md).

## Current state

- **Released to `main`:** PR #65 (`fix/self-service-production-readiness`), merged 2026-11-02.
- **Certified product SHA: `d7fd6c7`** (project tree `af7ec628`). The merge carried one further
  commit, `cd7a6fa`, that touched only the OBO certification fixture and this file; the shipped
  `backend/`, `infra/` and `frontend/` bytes were exactly the certified ones.
- **Since the certification:** PR #82 (`fix/gateway-connected-tool-targets`) changed the frontend
  Gateway editor and gallery templates only (see `CHANGELOG.md`, "a Gateway fed by connected
  canvas tools no longer carries a blank Lambda target"). It touched no backend or infrastructure
  file, so the certified backend and CDK product are unchanged; the frontend is covered by its
  unit suite and CI, not by a new live campaign.
- **Version:** every component still declares `0.1.0` (2026-07-17) with the changes above
  recorded under `[Unreleased]`. No release tag has been cut for the certified state.

## The certification (PR #65) — complete, receipt-backed

Run id `cand-d7fd6c7-us-east-1`, in the certification account (one account, bound by
`start --account-id`; the id is recorded in the run's receipts, not here).
`release_evidence.py status`: **`complete: true` — 140 active passes, 0 failures, 0 missing.**

- **Seven Regions × 3 live_region scenarios = 21/21 PASS**, every event bound to the same
  `source_commit = d7fd6c7` and `project_tree = af7ec628`. Per-Region deployment binding checked
  `SourceCommit`/`SourceProjectTree`/`SourceDirty=false`, the live template digest, and every
  Lambda `CodeSha256` against the sealed assembly.
  - us-east-1 · us-east-2 · us-west-2 · eu-central-1 · eu-west-1 · eu-west-2 · eu-west-3
- **us-east-1 home-Region full suite**: live_positive 14, live_adversarial 29, rollback 3,
  teardown_zero 4 (zero residue, including `aliasless-kms-and-log-groups-zero`), ui_cloudfront 2.
- **Offline gates: 67/67** (offline_positive 30, offline_adversarial 17, offline_mutation 16,
  exact_commit 2, clean_clone 2). Backend suite 9095 passed / 0 failed.
- **CI on the merged head:** all jobs green, including strict synthesis in all seven Regions and
  the CodeQL alert gate ("No new alerts in code changed by this pull request").

The run directory and the sealed per-Region assemblies were local build artifacts (they grew to
roughly 100 GB across the campaign) and were not retained after the merge. The durable record is
this file, the CI history on the merged commits, and the controls/traceability contracts under
`release/`. A future audit that needs receipts re-runs the campaign at the SHA under review.

## Defects found and fixed by the certification

- `d7fd6c7` — **reduced-Region Web Search refusal used an ad-hoc message** that listed Regions
  outside the seven-Region envelope. `require_managed_web_search_region()` now raises the shared
  `region_capabilities.feature_unavailable_message("managed_web_search", …)`. Caught live at the
  first reduced-feature Region; pinned by
  `test_reduced_region_features.py::test_web_search_template_is_refused_before_persistence_where_the_matrix_says_so`
  and re-confirmed in eu-west-3, eu-central-1 and eu-west-2. The new SHA invalidated prior
  evidence, so the full seven-Region cert was re-run at `d7fd6c7`.
- `cd7a6fa` — **OBO certification fixture logging** (`release/scenarios/obo_fixture_lambda.py`).
  CodeQL `py/clear-text-logging-sensitive-data` flagged a diagnostic that read the OAuth error
  code back out of the response object. The value was already one of seven constants; the fixture
  now logs from the literal `status`/`code` arguments at the point the error is built, so no logger
  reads a request or response. Harness-only: the fixture is deployed by the
  `identity.delegated_obo` scenario and is not part of any sealed product assembly.

## Deployed footprint

`acf-production` is deployed at `d7fd6c7` in all seven release Regions of the certification
account (the campaign's own deployments; they remain running and billing until torn down with
`scripts/cleanup.sh` per Region). One pre-campaign stack, `acfpr-cert-8d6cd05` in eu-west-1
(created 2026-10-08), predates this work and was left untouched.

## Operating notes for the next campaign

- The broker caps a credential session near 60 min; the core-runtime plan can exceed it.
  `regional_core.py` caches passed phases per (commit, Region, stack, template) and resumes at the
  first unpassed phase, so a re-vend resumes cleanly. Two late-teardown `ExpiredToken` failures
  passed on re-run with the functional assertions already satisfied.
- Run live certification with host sleep inhibited (`caffeinate -i -s` on macOS). One attempt
  failed on `Could not connect to the endpoint URL` because the laptop slept mid-run (process CPU
  time 390 s against 85 min of wall-clock).
- `release_evidence.py` binds a run to the exact HEAD commit and tree and refuses a dirty tree.
  Any new commit, even documentation, starts a new source identity: seal the evidence for a
  candidate before follow-up commits land, and keep docs-only follow-ups in a separate PR.
- Prune `build/release-assemblies/` and `build/release-evidence/` for superseded SHAs as the
  campaign progresses; a fresh checkout is cheapest with `git clone --filter=blob:none`.
- Every `CHANGELOG.md` edit shifts the one allowlisted `detect-secrets` finding; re-run
  `detect-secrets-hook` with the baseline and commit the refreshed line number, or CI fails.

## Open items

1. Cut a release (version bump across `backend/`, `frontend/`, `infra/`; move `[Unreleased]` to a
   dated section) for the certified state plus #82.
2. Decide the fate of the six non-home `acf-production` deployments left running by the campaign.
3. Re-enable the three Kiro Crew deny rules disabled for the campaign:
   `aws-destructive-cfn-deploy-mutate`, `aws-destructive-cfn-delete-stack`,
   `credential-exfil-s3-sync`.
