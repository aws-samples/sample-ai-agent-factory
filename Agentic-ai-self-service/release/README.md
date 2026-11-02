# Production-readiness release evidence

This directory defines the executable release contract for the self-service project. It does **not** contain a certification result. A candidate is releasable only after a clean, exact commit has a finalized evidence run that satisfies every control and every selected Region.

| File | Purpose |
|---|---|
| `production-readiness-controls.json` | Control-to-evidence requirements: positive, adversarial, mutation, live, UI, rollback, and teardown kinds. |
| `production-readiness-traceability.json` | Exact suite commands, committed test selectors, and named live scenarios that satisfy each required kind. |
| `agentcore-regions.json` | Dated AWS Documentation-derived schema-2 capability matrix and bounded seven-Region production envelope. |
| `../scripts/release_evidence.py` | Append-only recorder, completeness checker, finalizer, and independent verifier. |

## What the recorder proves

A finalized run proves that:

* every event names a declared control, required evidence kind, and exact mapped suite or scenario;
* every suite mapped to an offline kind passes; one generic pass cannot stand in for missing backend, infrastructure, frontend, static, package, or synthesis gates;
* every named live scenario passes, including each of the seven single-Region profiles selected by the bounded production envelope for `live_region`;
* failures and blockers remain open until a later event explicitly supersedes the same control/kind/Region/account/suite/scenario tuple;
* artifacts are represented only by labels, byte counts, and SHA-256 digests;
* the run, every event, the complete event set, and the final result are digest-bound;
* every mutation came from the same clean Git commit and `Agentic-ai-self-service` subtree;
* the final result can be verified from an independent detached clean clone of that commit.

The recorder does not decide whether a test was truthful, copy raw logs, retain credentials, or create third-party/WORM attestation. The live runner must produce the underlying evidence, and reviewers must inspect the digest-addressed artifacts. A local administrator can replace an entire evidence directory; publish the final digest through the reviewed release process when an external trust anchor is required.

## Record a release candidate

**Execution context:** local clean clone of this repository. The recorder itself never
calls AWS; live scenarios do, through the executables the contract names. Commit all
project changes first: the run binds to the exact commit, the `Agentic-ai-self-service`
tree, AND the repository's `.github/workflows/agentic-ai-self-service-ci.yml` blob, and
refuses to start or continue while either is dirty.

Evidence is executed, not asserted. There is no command that records a pass. The recorder
runs the suites and scenarios that `production-readiness-traceability.json` declares and
writes a receipt for each: the resolved argv, every tool executable by path and digest with
its reported version, exit statuses, timing, the JUnit report it parsed, the provenance a
live scenario emitted, and digests of every artifact the command produced (kept under the
run directory and re-digested on every later load). The event's status is derived from
those facts when it is written and re-derived from the stored receipt every time the run is
read, so an edited event file cannot earn credit.

```bash
python3 scripts/release_evidence.py start \
  --run-id release-YYYYMMDD-commit \
  --account-id 123456789012          # the ONE account every live receipt must name
```

The command prints the absolute run directory. Use that value as `RUN_DIR`.

Offline evidence: run a suite. The recorder executes the suite's command arrays in its
declared working directory, substituting only the tool placeholders you supply as absolute
paths (recorded by digest) and the recorder's own context placeholders. For `pytest` and
`javascript` suites it routes JUnit output to `{junit_xml}` and credits each
`(control, kind)` mapped to the suite only when every one of its mapped selectors appears in
that report as a passed test case; a `command` suite's check ids are credited only when
every command exits 0. Declared `required_tools` versions are checked against the
executables actually used.

```bash
python3 scripts/release_evidence.py run-suite \
  --run-dir "$RUN_DIR" --suite-id export-gate \
  --tool backend_python=/abs/path/backend/.venv/bin/python \
  --tool actionlint=/abs/path/actionlint
```

Live, UI, rollback, teardown, clean-clone, and exact-commit evidence: run a scenario. Every
scenario the contract names is executed through `release/scenarios/run_scenario.py`. An
unimplemented scenario exits 3 and records `blocked`; it can never record a pass. A live
scenario must write `report` and `provenance` under the artifact directory it is given;
provenance must name the run's bound account, the requested Region, a stack, and the
candidate commit and project tree it observed deployed. Provenance for another account,
another Region, or a stale deployed build is refused before anything is recorded.

```bash
python3 scripts/release_evidence.py run-scenario \
  --run-dir "$RUN_DIR" --scenario-id core-runtime-gateway-identity-policy-evaluation \
  --region eu-west-1 \
  --tool backend_python=/abs/path/backend/.venv/bin/python
```

`live_region` scenarios are accepted only for a Region selected by that control in
`agentcore-regions.json` schema 2. A failed or blocked event stays active; rerunning the
same suite or scenario (same Region) records a new event that supersedes it, and the earlier
event is never edited or deleted.

```bash
python3 scripts/release_evidence.py status --run-dir "$RUN_DIR"
python3 scripts/release_evidence.py finalize --run-dir "$RUN_DIR"
python3 scripts/release_evidence.py verify --run-dir "$RUN_DIR"
```

`finalize` fails if any required tuple is missing, any active failure or blocker remains, or
live receipts name more than one account or deployed build. A finalized run rejects all
later executions. `verify` re-runs every check -- identity, contract fingerprints, receipt
re-derivation, artifact digests -- from a clean clone of the same commit.

## Downloadable-assets control

`delivery.downloadable_assets` is releasable only when every mapped evidence kind passes. Its offline suite runs `backend/tests/test_downloadable_asset_contract.py`, the standalone Python exporter tests, CloudFormation export and deployability tests, staging/isolation tests, this documentation contract, and CI workflow checks. The suite pins cfn-lint 1.57.0, Checkov 3.3.1, Terraform 1.14.6, ShellCheck 0.11.0, and actionlint 1.7.12.

The live evidence cannot call a generator directly. It must download both archives through an authenticated API or real CloudFront UI backed by the deployed candidate, verify the response archive digest, `artifact-manifest.json`, `SHA256SUMS`, and `verify-artifact.py`, and then use a clean recipient workspace with no repository imports. For each of the seven selected Regions, the CloudFormation path must deploy, invoke, update, roll back, tear down, and finish with zero project resources in direct service inventories. The standalone Python path must install the exact lock, run `pip check` plus the installed-closure verifier, compile, start, and invoke. Mandatory adversarial scenarios reject archive or manifest tampering, a missing or incompatible Python dependency, a wrong AWS account or Region, and CI/CD examples whose deployment or image-publication defaults become active. Clean-clone, exact-commit, real-browser UI, rollback, and `teardown_zero` events are separate required evidence, not implications of an offline pass.

This is a bounded claim about the exact commit, archives, tool versions, seven Region profiles, and recorded scenarios. Exact package versions and the installed dependency closure are enforced, but Python distribution wheel bytes are not hash-locked. The contract does not guarantee future package-index bytes, future AWS behavior, or defect-free operation.

## Independent clean-clone verification

**Execution context:** a second local clone, detached at the exact commit printed in `run.json`; no AWS account or Region is used.

1. Make the completed run directory available read-only to the second clone.
2. Check out the recorded commit in detached mode and install only from the committed
   backend/infra/npm locks.
3. Rebuild Lambda dependencies, all 11 AgentCore bundles, frontend, wheel/source
   distribution, and the strict production CDK assembly. Require byte-identical Runtime
   ZIPs, Lambda tree digest, wheel, source distribution, template, and asset manifest.
4. Rerun every offline suite from `production-readiness-traceability.json` with zero
   missing-tool or generated-artifact skips.
5. Run `python3 scripts/release_evidence.py verify --run-dir <shared-run-directory>`.
6. Compare the printed `final_digest` with the digest published by the reviewed release process.

Repository paths and branch names may differ in the second clone. Verification binds the commit, project-relative path, subtree Git object, contract bytes, events, and final seal.

## Selected Region support envelope

The schema-2 matrix cites the official [AgentCore supported Regions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html) page. One installation deploys into exactly one selected Runtime home Region; a Region is certified only once a finalized evidence run names it. Production supports Runtime only. Managed Harness and A2A remain excluded even where AWS exposes those capabilities.

| Region | Production Runtime | Profile | Memory | AWS Agent Registry federation | Managed Web Search |
|---|---:|---|---:|---:|---:|
| US East (N. Virginia), `us-east-1` | **Yes** | Full | Yes | Yes | Yes |
| US East (Ohio), `us-east-2` | **Yes** | Full | Yes | Yes | Yes |
| US West (Oregon), `us-west-2` | **Yes** | Full | Yes | Yes | Yes |
| Europe (Ireland), `eu-west-1` | **Yes** | Full | Yes | Yes | Yes |
| Europe (Frankfurt), `eu-central-1` | **Yes** | Reduced | Yes | Yes | **Not supported** |
| Europe (London), `eu-west-2` | **Yes** | Reduced | Yes | Yes | **Not supported** |
| Europe (Paris), `eu-west-3` | **Yes** | Reduced | Yes | **Not supported** | **Not supported** |
| US West (N. California), `us-west-1` | No | Excluded | No | No | No |

Frankfurt and London retain AWS Agent Registry federation and the built-in DynamoDB registry but omit managed Web Search. Paris retains the built-in DynamoDB registry but omits both managed Web Search and AWS Agent Registry federation. The UI and API fail closed when a workflow requests an unavailable feature, and IAM permissions for that feature are omitted. Milan, Spain, Stockholm, N. California, and every unlisted Region remain outside this certification set. Feature-specific scenarios run only where the corresponding matrix flag is true.

The `strict-synth` suite runs one production `cdk synth --strict` plus cdk-nag gate for each of the seven selected candidate Regions, including `us-east-1`, `us-east-2`, and `us-west-2`. The `regional.certified_core` control then requires independent `live_region` evidence for every selected Region. Updating the matrix is a reviewed source change that invalidates an in-progress run and requires fresh evidence.
