# Production-readiness release evidence

This directory defines the executable release contract for the self-service project. It does **not** contain a certification result. A candidate is releasable only after a clean, exact commit has a finalized evidence run that satisfies every control and every selected Region.

| File | Purpose |
|---|---|
| `production-readiness-controls.json` | Control-to-evidence requirements: positive, adversarial, mutation, live, UI, rollback, and teardown kinds. |
| `production-readiness-traceability.json` | Exact suite commands, committed test selectors, and named live scenarios that satisfy each required kind. |
| `emea-agentcore-regions.json` | Dated AWS Documentation-derived EMEA capability matrix and the bounded production envelope. |
| `../scripts/release_evidence.py` | Append-only recorder, completeness checker, finalizer, and independent verifier. |

## What the recorder proves

A finalized run proves that:

* every event names a declared control, required evidence kind, and exact mapped suite or scenario;
* every suite mapped to an offline kind passes; one generic pass cannot stand in for missing backend, infrastructure, frontend, static, package, or synthesis gates;
* every named live scenario passes, including all seven documented EMEA core Regions for `live_region`;
* failures and blockers remain open until a later event explicitly supersedes the same control/kind/Region/account/suite/scenario tuple;
* artifacts are represented only by labels, byte counts, and SHA-256 digests;
* the run, every event, the complete event set, and the final result are digest-bound;
* every mutation came from the same clean Git commit and `Agentic-ai-self-service` subtree;
* the final result can be verified from an independent detached clean clone of that commit.

The recorder does not decide whether a test was truthful, copy raw logs, retain credentials, or create third-party/WORM attestation. The live runner must produce the underlying evidence, and reviewers must inspect the digest-addressed artifacts. A local administrator can replace an entire evidence directory; publish the final digest through the reviewed release process when an external trust anchor is required.

## Record a release candidate

**Execution context:** local clean clone of this repository; no AWS account or Region is used by these recorder commands. Commit all project changes first. Keep raw logs under ignored `build/` storage or another controlled evidence store; never put credentials or raw API responses in event details.

Run every command array declared for a suite in `production-readiness-traceability.json`, substituting only the named executable/output placeholders. Capture the combined exit status and log as one suite artifact. Because a suite command runs its complete gate, one passing suite event covers all exact selectors mapped to that suite; finalization still requires every distinct mapped suite.

```bash
python3 scripts/release_evidence.py start \
  --run-id release-YYYYMMDD-commit
```

The command prints the absolute run directory. Use that value as `RUN_DIR` for subsequent commands.

Record one bounded event after its underlying test completes:

```bash
python3 scripts/release_evidence.py record \
  --run-dir "$RUN_DIR" \
  --control safety.guardrails \
  --kind offline_positive \
  --status pass \
  --suite-id backend \
  --detail test_count=42 \
  --detail exit_code=0 \
  --artifact pytest=build/release-logs/guardrails-positive.log
```

Offline kinds require `--suite-id`; only suites mapped to that control/kind are accepted. Non-offline kinds require `--scenario-id`; generic scenario names are refused. Live evidence additionally requires both `--region` and `--account-id`, where the latter is the 12-digit account whose resources produced the result. `live_region` is accepted only for a Region selected by that control in `emea-agentcore-regions.json`.

```bash
python3 scripts/release_evidence.py record \
  --run-dir "$RUN_DIR" \
  --control regional.emea_core \
  --kind live_region \
  --status pass \
  --region eu-west-1 \
  --account-id 123456789012 \
  --scenario-id core-runtime-gateway-identity-policy-evaluation \
  --artifact probe=build/release-logs/eu-west-1-core.json
```

A failed or blocked result remains active. After fixing and rerunning the same test, append a new event with `--supersedes <event-id>`; existing files are never edited.

```bash
python3 scripts/release_evidence.py status --run-dir "$RUN_DIR"
python3 scripts/release_evidence.py finalize --run-dir "$RUN_DIR"
python3 scripts/release_evidence.py verify --run-dir "$RUN_DIR"
```

`finalize` fails if any required tuple is missing or any active failure/blocker remains. A finalized run rejects all later records.

## Independent clean-clone verification

**Execution context:** a second local clone, detached at the exact commit printed in `run.json`; no AWS account or Region is used.

1. Make the completed run directory available read-only to the second clone.
2. Check out the recorded commit in detached mode.
3. Run `python3 scripts/release_evidence.py verify --run-dir <shared-run-directory>`.
4. Compare the printed `final_digest` with the digest published by the reviewed release process.

Repository paths and branch names may differ in the second clone. Verification binds the commit, project-relative path, subtree Git object, contract bytes, events, and final seal.

## EMEA support envelope

The dated matrix cites the official [AgentCore supported Regions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html) page. The core Runtime deployment scenario must pass independently in:

| Region | Core | Memory scenario | Agent Registry scenario | Web Search scenario |
|---|---:|---:|---:|---:|
| Europe (Frankfurt), `eu-central-1` | Yes | Yes | No | No |
| Europe (Ireland), `eu-west-1` | Yes | Yes | Yes | Yes |
| Europe (London), `eu-west-2` | Yes | Yes | No | No |
| Europe (Milan), `eu-south-1` | Yes | No | No | No |
| Europe (Paris), `eu-west-3` | Yes | Yes | No | No |
| Europe (Spain), `eu-south-2` | Yes | No | No | No |
| Europe (Stockholm), `eu-north-1` | Yes | Yes | No | No |

Feature-specific scenarios run only where their matrix flag is true. Managed Harness and production A2A remain outside the production envelope for the reasons recorded in the matrix. A matrix update is a reviewed source change: it invalidates an in-progress run and requires fresh evidence.
