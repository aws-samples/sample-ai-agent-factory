# Live evidence — upgrade, no-op redeploy and interrupted deployment

- **Date:** 2026-09-25
- **Status:** PASS — Platform and Workload no-op redeploys, sampled upgrades
  in both environments, a nonproduction deployment cancelled early and late
  with zero failed sessions, and a re-run to green in both environments. The
  first sampled upgrade found a generated-agent defect (a reply with neither
  a directive nor the done marker was reported as success), fixed in agent
  1.2.0 (`13153b4`) before the formal gates ran
- **Revisions under test:** Workload `da75f6b` (agent 1.1.0), `13153b4`
  (1.2.0), `fdaa553` (1.2.1); Platform `da75f6b` (no Platform change since
  `e72c19c`)
- **Accounts / Region:** Platform (pipelines, inference Gateway) and
  Workstream test (Runtime, Memory, Tools Gateway), both `us-west-2`
- **Probes:** `scripts/live-agentcore-generated-agent-spike/deployment_continuity_probe.py`
  (samples a full governed session every 15 s through a deployment and gates
  on availability, the observed `agentVersion` sequence and, from `13153b4`,
  records the failed check names per sample) and `live_invoke_probe.py`
  (positive session and unsubscribed-tool twin)

## What the gates require

| Gate                   | Pass condition                                                                                                                                                                   |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No-op redeploy         | a pipeline run on an unchanged revision updates zero stacks and changes no resource identity                                                                                     |
| Upgrade                | a real agent revision promotes through both environments while every sampled session succeeds, the version switches exactly once, and the prior revision serves until the switch |
| Interrupted deployment | a deployment cancelled mid-flight rolls back with the prior revision serving throughout and no failed session                                                                    |
| Re-run to green        | the interrupted revision then deploys cleanly by re-running the stage                                                                                                            |

## Platform no-op redeploy — PASS

Platform run `e370a2dd` on `da75f6b` (no Platform change since `e72c19c`):
every one of the nine Nonprod and six Prod CloudFormation actions reported
`Change set PipelineChange was created with no changes` /
`executed with no change`, and all six environment stacks kept their
`LastUpdatedTime` from before the run (the Nonprod `Registry`,
`InferenceGateway` and `Guardrail` stacks and their Prod twins). The
`SecurityReview` approval was given only after that was verified.

## Upgrade 1 (2d1340f → 1.1.0) — defect found

Workload run `3d975c77` on `da75f6b` (agent `92ff4b0`, first revision to
report `agentVersion`):

- **Nonproduction:** deployed at 08:59Z (Runtime version 3 → 4, new image
  digest; the before/after identity snapshots differ only in the Runtime
  version, digest and timestamps). This switch was **not** sampled — the
  session driving the campaign was interrupted across the window — so it is
  recorded as deployed, not as a continuity pass. Post-deploy: 2/2 sessions
  pass on `1.1.0`; unsubscribed-tool twin refused.
- **Production (sampled):** 21 sessions across the switch at 09:44Z; the
  version changed exactly once (`none` → `1.1.0`), the Runtime stayed
  `READY`, and **20/21** passed. The failure is not a deployment effect:

### Root cause of the failed session

Sample 15 (09:45:08Z, `1.1.0` already serving, stack `UPDATE_COMPLETE`)
returned HTTP 200 with the success marker but made no tool call.

- The inference interceptor logged a single completion for that session
  (every passing session makes two).
- The session's Memory event holds the reply fingerprint of the model's only
  reply, and that fingerprint was reproduced exactly through the nonproduction
  Gateway: `We need to output the TOOL line.TOOL <echo tool> {...}` — the
  rated reasoning model leaked a reasoning fragment glued in front of the
  directive.
- The line-start directive grammar correctly refused to execute it, but the
  agent loop treated **any** non-directive reply as task completion, so a
  failed task was reported as a success. The adapter also replaced an empty
  reply with the done marker, which hid the same failure class.

**Fix (`13153b4`, agent 1.2.0):** only a line-start directive or a reply
containing `<done/>` is a valid turn; anything else receives a plain runtime
notice as a user turn, at most twice and within the iteration bound; a buried
directive is never executed; the response reports `stopReason` and
`protocolRepairs`; the system prompt asks a decline to end with `<done/>`.
Measured through the nonproduction Gateway before commit: the notice wording
was guardrail-allowed and repaired 10/10 (a JSON wording managed 7/10),
first turns 53/53 compliant, declines now end with the marker 10/10 (none
did before), full two-turn sessions 12/12. A mutant restoring the old
stop-on-any-reply loop fails the four new unit tests.

## Upgrade 2 (1.1.0 → 1.2.0) — switch clean; every failure is the old revision

Workload run `ee987174` on `13153b4`, sampled in both environments (the
sampler now records the failed check names per sample):

|                                 | Nonproduction (11:11–11:23Z)            | Production (11:25–11:29Z)               |
| ------------------------------- | --------------------------------------- | --------------------------------------- |
| Version sequence                | `1.1.0` × 42 → `1.2.0` × 7, one switch  | `1.1.0` × 11 → `1.2.0` × 7, one switch  |
| Runtime status                  | `READY` in every sample                 | `READY` in every sample                 |
| `1.2.0` sessions                | 7/7 pass, `stopReason: done`, 0 repairs | 7/7 pass, `stopReason: done`, 0 repairs |
| `1.1.0` sessions                | 40/42 pass; 2 × `echoToolCalled` only   | 10/11 pass; 1 × `echoToolCalled` only   |
| Latency p50 (`1.1.0` / `1.2.0`) | 7.4 s / 6.6 s                           | 8.6 s / 7.3 s                           |

Every failure is the silent partial success the fix removes (HTTP 200, one
content block, no tool call, on the old revision), and the first `1.2.0`
session in each environment is the cold one (~17.6 s). The sampler's
availability gate therefore reads `passed: false` for this run by design: a
continuity gate can only pass when the revision serving before the switch
is itself correct, which is why the formal interrupted-deployment and
re-run gates below start from `1.2.0`. Unsubscribed-tool twin on `1.2.0`
(nonproduction): refused by the model allow-list, one content block.

## Interrupted deployment (1.2.0 → 1.2.1, nonproduction) — PASS, twice

Workload run `01d20227` on `fdaa553` (version-only 1.2.1). A local watcher
issued `CancelUpdateStack` exactly once on the nonproduction RuntimeMemory
stack (it refuses any other account or stack) while the continuity sampler
ran sessions every 15 s. Two interruption points were exercised, the second
through `retry-stage-execution --retry-mode ALL_ACTIONS`:

|                         | Early cancel                                                                    | Late cancel                                                                                                                                                                            |
| ----------------------- | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Trigger                 | stack `UPDATE_IN_PROGRESS` (11:48:25Z)                                          | Runtime **resource** `UPDATE_IN_PROGRESS` (11:53:55Z)                                                                                                                                  |
| What CloudFormation did | cancelled before any resource changed; `UPDATE_ROLLBACK_COMPLETE` in 6 s        | Runtime `UPDATE_FAILED: Resource update cancelled`, then restored (`UPDATE_COMPLETE` 11:54:09Z); the image scan gate's replacement id cleaned up; `UPDATE_ROLLBACK_COMPLETE` 11:54:13Z |
| Runtime versions        | none created                                                                    | v6 created with the 1.2.1 digest by the cancelled update, v7 created by the rollback with the **1.2.0 digest** (identical to v5)                                                       |
| Sessions                | 45/45 pass, all `1.2.0`                                                         | 16/16 pass: `1.2.0` × 9, `1.2.1` × 1 (during the 14-second rollback), `1.2.0` × 6                                                                                                      |
| Pipeline                | `RuntimeMemory.Deploy` Failed: `Current stack status: UPDATE_ROLLBACK_COMPLETE` | same                                                                                                                                                                                   |

Both sampler gates (`--expect-final-version 1.2.0`, zero failed samples)
pass. The late case shows the real semantics: a cancelled Runtime update is
rolled back by _creating a new Runtime version_ with the prior container,
and while that happens the endpoint may briefly serve the half-applied
revision — every session in that window still succeeded, and the service
converged back to the prior revision.

## Re-run to green (1.2.1)

**Nonproduction — PASS.** A second `retry-stage-execution --retry-mode
ALL_ACTIONS` on the same execution re-created both change sets and deployed
1.2.1 from `UPDATE_ROLLBACK_COMPLETE`: 17/17 sessions pass across a single
switch (`1.2.0` × 10 → `1.2.1` × 7), Runtime `READY` throughout, stage
`Succeeded`. Unsubscribed-tool twin on 1.2.1: refused by the model
allow-list, one content block.

**Production — PASS.** Approved at 12:03:00Z after the nonproduction
results above: 17/17 sessions pass across a single switch (`1.2.0` × 10 →
`1.2.1` × 7), Runtime `READY` throughout; execution `01d20227` `Succeeded`.

## Workload no-op redeploy — PASS

Workload run `df8dd79c` on the unchanged head `fdaa553`, sampled in both
environments with the stable-version gate (every sample the same version,
no stack change observed):

|                        | Nonproduction                                                                                                                                       | Production                                                |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| CloudFormation actions | all six `created with no changes` / `executed with no change` (RegistryRoles, ToolGateway, RuntimeMemory)                                           | ToolGateway and RuntimeMemory: `no changes` / `no change` |
| Sessions               | 48/48 pass, `1.2.1` throughout, 12:15–12:27Z                                                                                                        | 24/24 pass, `1.2.1` throughout, 12:26–12:31Z              |
| Identity               | before/after snapshots identical except the snapshot timestamp: Runtime versions (8 nonprod, 5 prod), image digest, Gateway targets, stack statuses | same                                                      |

A new source commit that changes only agent code is _not_ a Tool Gateway
no-op by design: the two `RegistryValidate-*` custom resources carry the
source revision and re-validate the Registry records on every new commit
(observed in Run B's production ToolGateway update); no identity changes.

## Totals across the campaign

| Revision class                  | Sampled sessions | Failed | Notes                                                                                                                                                                |
| ------------------------------- | ---------------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Before 1.2.0 (`2d1340f`, 1.1.0) | 74               | 4      | every failure is the silent partial success (`echoToolCalled` only)                                                                                                  |
| 1.2.0 and 1.2.1                 | 181              | 0      | one session (early-interrupt run, 1.2.0) needed a protocol repair: 3 content blocks, `protocolRepairs: 1`, tool called, `stopReason: done` — the fix recovering live |

## Observations and residual risks

- During a cancelled Runtime update the endpoint may serve the half-applied
  revision for the ~15 s the rollback takes (one `1.2.1` session between
  `1.2.0` sessions in the late-cancel run). Every session succeeded; a
  revision that is incompatible with its predecessor's Memory or tool
  contract would need its own compatibility gate.
- The protocol repair is bounded and measured (10/10 repaired through the
  Gateway, one live repair in 181 sessions); a model that keeps violating the
  protocol ends with `stopReason: protocol_violation`, which the positive
  gate fails, rather than a false success.
- Proven in `us-west-2` only; the EMEA matrix is a separate open gate.
- 10:47–10:49Z: a measurement harness run got seven consecutive
  `403 insufficient_scope` responses from the nonproduction inference
  Gateway after one mid-stream `5xx`; the interceptor was never invoked for
  them and a fresh token was healthy at 10:50Z. Not reproduced; recorded as a
  transient Gateway-side dependency observation.
