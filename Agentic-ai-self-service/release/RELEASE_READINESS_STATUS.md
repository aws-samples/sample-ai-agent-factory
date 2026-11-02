# PR #65 Release-Readiness Status

Branch: `fix/self-service-production-readiness`. **Certified product SHA: `d7fd6c7`** (project tree
`af7ec628`). The PR head is `d7fd6c7` plus exactly one harness-only commit (this file and
`release/scenarios/obo_fixture_lambda.py`); `git diff d7fd6c7 HEAD -- backend/src infra frontend`
is empty. Every `acf-production` stack reports `SourceCommit = d7fd6c7…`,
`SourceProjectTree = af7ec628…`, `SourceDirty = false`, in all seven release Regions,
account 048834207240.

**Status: MERGE-READY.** Omar undrafts and merges; the agent does not.

## Clean-room cert at ONE SHA — complete, receipt-backed

Run dir: `build/release-evidence/cand-d7fd6c7-us-east-1/`.
`release_evidence.py status`: **`complete: true` — 140 active passes, 0 failures, 0 missing.**

- **Seven Regions × 3 live_region scenarios = 21/21 PASS**, every event bound to the SAME
  `source_commit = d7fd6c7` and `project_tree = af7ec628`. Per-Region deployment binding checked
  `SourceCommit`/`SourceProjectTree`/`SourceDirty=false`, the live template digest, and every
  Lambda `CodeSha256` against the sealed assembly.
  - us-east-1 · us-east-2 · us-west-2 · eu-central-1 · eu-west-1 · eu-west-2 · eu-west-3
- **us-east-1 home-Region full suite**: live_positive 14, live_adversarial 29, rollback 3,
  teardown_zero 4 (zero residue, including `aliasless-kms-and-log-groups-zero`), ui_cloudfront 2.
- **Offline gates: 67/67** (offline_positive 30, offline_adversarial 17, offline_mutation 16,
  exact_commit 2, clean_clone 2). Backend suite 9095 passed / 0 failed.
- Sealed assemblies: `build/release-assemblies/d7fd6c7…/<region>/` for all seven Regions.

## Product defect found + fixed this arc (committed as d7fd6c7)

`d7fd6c7` — **Converge managed Web Search Region refusal on the shared release-contract message**
(`backend/src/app/services/managed_web_search.py`, +5/-3). `require_managed_web_search_region()`
built an ad-hoc "unavailable" message that listed uncertified Regions via raw `{region!r}`; it now
raises the shared `feature_unavailable_message("managed_web_search", region)` from
`region_capabilities.py`. Caught at a reduced-feature Region by
`test_reduced_region_features.py::…_web_search_template_is_refused_before_persistence…`;
re-confirmed PASS in eu-west-3, eu-central-1 and eu-west-2. The new SHA invalidated prior
evidence, so the full seven-Region cert was re-run at `d7fd6c7`.

## Harness-only follow-up on top of d7fd6c7 (CodeQL alert #117)

GitHub code scanning raised ONE new alert on the PR, `py/clear-text-logging-sensitive-data`
(high) at `release/scenarios/obo_fixture_lambda.py::_log_rejection`. Analysis of the SARIF paths:
all ten flows start at the ten `_oauth_error(...)` call sites (CodeQL treats each call's return
as a sensitive source) and reach the log through `response → body → "error"`. The logged value
was already one of seven constant OAuth codes, never request data — a **false positive** — but
the diagnostic did read back from the response object.

Fix: `_oauth_error` now logs the rejection itself from its literal `status`/`code` arguments; the
route (method, path) comes from a per-invocation `ContextVar` set by `handler`. No logger reads a
request or response any more. Behaviour is unchanged: one JSON line per rejection, silence on 200.
Verified by seven behavioural checks (every code logged verbatim, 401 challenge preserved,
header/body/token values provably absent from the log, exception path 503). This fixture is deployed
only by the `identity.delegated_obo` live scenario and is **not part of any sealed product assembly**.

## Residue sweep

- Deleted the one pre-campaign orphan log group
  `/aws/bedrock-agentcore/runtimes/it_widget_c8d9268db5bf_acbb40aa-lps9Es6t1w-DEFAULT`
  (created 2026-10-30, before the 2026-11-01 campaign start; its runtime no longer existed) after
  which `aliasless-kms-and-log-groups-zero` passed.
- Swept this arc's own scratch left by a host-sleep-aborted attempt in us-west-2: recipient stack
  `it-gateway-214f494aede943f8` (DELETE_COMPLETE) and bucket
  `acfpr-recipient-us-west-2-fdfdbc8f5599`.

## Environmental notes (not defects)

- The broker caps a credential session near 60 min; the core-runtime plan can exceed it.
  `regional_core.py` caches passed phases per (commit, Region, stack, template) and resumes at the
  first unpassed phase. Two late-teardown `ExpiredToken` failures (us-west-2 core-runtime,
  eu-west-1 CFN-recipient) passed on re-run; the functional assertions had already succeeded.
- One us-west-2 CFN-recipient attempt failed on `Could not connect to the endpoint URL`: the host
  slept mid-run (process CPU time 390 s vs 85 min wall-clock). Re-run under `caffeinate` passed.
  Run live certification with sleep inhibited.
- `release_evidence.py` binds a run to the exact HEAD commit and tree and refuses a dirty tree; any
  new commit (even docs) starts a new source identity. Evidence for a candidate must be sealed
  before follow-up commits land.

## Remaining path (operator)

1. Confirm CI is green on the PR head (offline gates mirror the local 67/67).
2. **Omar undrafts + merges PR #65.** The agent never merges/undrafts.
3. **Re-enable the three Kiro Crew deny rules** disabled for this campaign:
   `aws-destructive-cfn-deploy-mutate`, `aws-destructive-cfn-delete-stack`,
   `credential-exfil-s3-sync`.
4. Optional, policy-dependent: if the release policy requires evidence bound to the exact PR head
   rather than to the certified product SHA, re-run the campaign at HEAD (product bytes identical;
   ~1–1.5 days).
