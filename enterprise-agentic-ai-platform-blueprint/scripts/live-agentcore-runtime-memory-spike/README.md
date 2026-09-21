# AgentCore Runtime + Memory compatibility spike

A cleanup-first, isolated probe of **Amazon Bedrock AgentCore Runtime** and
**Memory**. It is the Runtime/Memory sibling of
[`../live-agentcore-gateway-spike/`](../live-agentcore-gateway-spike/) and
follows the same safety conventions: an AWS-free model, a scope registry with a
static reachability guarantee, per-command scopes, an STS account gate before
any mutation, run-marker provenance, scratch-only state, bounded polling with
immediate terminal-status failure, discovery-based cleanup, and cleanup in
`finally`.

It exists to answer one narrow question against real AWS: **does the pinned SDK
create an encrypted Memory and a container Runtime that reach `ACTIVE`/`READY`,
does a deterministic `InvokeAgentRuntime` handshake return the exact expected
JSON, and does a Memory `CreateEvent` → `GetEvent` round-trip reproduce every
field — and can all of it be discovered and torn down to zero residuals?** It
proves nothing beyond what it exercises live.

> This spike is a compatibility probe, not a product path. It never deploys to a
> workstream account, never commits, and never runs itself — an operator runs
> it with explicit account/region/credentials. The blueprint's real Runtime and
> Memory still flow through the reviewed pipeline.

## What it owns (and only this)

| Resource                  | Operation                                   | Notes                                                                                |
| ------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------ |
| One AgentCore **Memory**  | `CreateMemory` → `DeleteMemory`             | Encrypted with the caller's CMK (`encryptionKeyArn`), `eventExpiryDuration` = 7 days |
| One AgentCore **Runtime** | `CreateAgentRuntime` → `DeleteAgentRuntime` | Built from the caller's **digest-pinned** container image                            |

The **caller owns** the container image, the execution role, and the CMK. The
probe **never** creates, mutates, or deletes them, nor runtime endpoints,
aliases, or KMS grants. Those inputs are validated to be in the same
account/region, and the image must be referenced by an `@sha256:<digest>`.

Every owned resource carries the five allocation tags
(`application-id`, `agent-id`, `tenant-id`, `cost-centre`, `environment`) plus a
run-specific `description` that embeds a per-run marker.

## Files

| Path                           | Purpose                                                                                                                                                                                                                                       |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runtime_memory_model.py`      | AWS-free model: validation, exact ownership, run-marker token derivation, status enums, provenance, exact handshake/round-trip predicates, secret-safety, service-model pins. No boto3/network/filesystem.                                    |
| `runtime_memory_spike.py`      | CLI orchestration: `deploy`, `verify`, `exercise-memory`, `cleanup`, `all`. One small wrapper per AWS call; standalone path bootstrap.                                                                                                        |
| `conftest.py`                  | Puts this dir and the sibling gateway-spike dir on `sys.path` for the shared helpers. No AWS calls at collection.                                                                                                                             |
| `test_runtime_memory_model.py` | 63 pure-function tests.                                                                                                                                                                                                                       |
| `test_runtime_memory_spike.py` | 55 conformance/orchestration tests, incl. AST reachability, full offline botocore input/output contracts, deploy/cleanup partial-create recovery, state/evidence poisoning refusal, completed-run token-reuse refusal, and collision refusal. |
| `agent/agent.py`               | Minimal `BedrockAgentCoreApp` entrypoint: a deterministic handshake only.                                                                                                                                                                     |
| `agent/Dockerfile`             | ARM64, numeric non-root UID 10001, digest-pinned AWS Lambda Python 3.13 (AL2023) base, `EXPOSE 8080`, and cleared inherited CMD.                                                                                                              |
| `agent/requirements.txt`       | The single dep the agent imports (`bedrock-agentcore`), exact-pinned.                                                                                                                                                                         |
| `requirements.txt`             | Probe deps (`boto3`, `botocore`), exact-pinned.                                                                                                                                                                                               |

The shared helpers (`SpikeError`, `Evidence`, `JsonStore`, `utc_now`,
`aws_error_code`) are reused from
`../live-agentcore-gateway-spike/gateway_spike.py`. The spike adds a safe,
idempotent `sys.path` bootstrap so it also imports standalone from a fresh
checkout with no conftest (there is a subprocess `--help` smoke test for this).

## Safety model

- **Scope registry.** `MUTATING_API_CALLS` lists every state-changing SDK call;
  `SIDE_EFFECTING_API_CALLS` lists `invoke_agent_runtime`, which is not a write
  but IS billable/side-effecting; `READ_ONLY_EXCEPTIONS` lists reads whose names
  look mutating (`get_event`). A conformance test walks the AST and fails if any
  scoped call on a spike client is unregistered.
- **`verify` reaches exactly `InvokeAgentRuntime` and no write.** An AST
  reachability test proves `verify` reaches exactly `invoke_agent_runtime` and
  no lifecycle/data mutation. `exercise-memory` reaches only `create_event`;
  `cleanup` reaches deletions but no creations and never invoke.
- **Per-command scope guard.** Each command declares the exact operations it may
  perform; the API wrapper refuses any call outside the current scope, so an
  accidental future wiring fails closed.
- **STS account gate.** `verify_identity()` confirms `sts:GetCallerIdentity`
  matches `--account-id` before any mutation or invoke — including before
  `InvokeAgentRuntime`.
- **Live ownership proof before every side-effect.** Before invoke, `CreateEvent`,
  or any delete, the probe fetches the live resource and proves: exact expected
  name (`SpikeNames.owns` is exact, never prefix-only), ID/ARN consistency,
  account/region ARN scope, run-specific description (embeds the run marker),
  and expected immutable inputs (container digest + role for Runtime; CMK +
  event-expiry for Memory). Runtime invocation uses only the ARN from that
  ownership-proven response and refuses a conflicting persisted ARN. Memory
  actor/session values are deterministically derived and any conflicting
  persisted values are refused. For **Runtime** the proof also verifies all five
  allocation tags via `ListTagsForResource`.
- **Run marker + provenance.** A 128-bit run marker is persisted atomically in
  the state header _before the first AWS mutation_. On resume, state provenance
  (schema, account, region, prefix, marker) is validated; a foreign/poisoned
  state is refused. The evidence stream is independently bound to the marker's
  fingerprint plus the same account suffix, region, prefix, and SDK versions;
  stale, foreign, or identifier-bearing evidence is refused, and state/evidence
  paths cannot name the same file. Every create/delete/event idempotency token
  and the `runtimeSessionId` are derived from that marker.
- **Deterministic tokens with strong-digest truncation.** `client_token` is
  `<op>-<sha256 slice>`; truncation always retains ≥ 32 hex digest chars, so it
  can never weaken collision resistance, and every token is ≥ 33 chars. Actor
  and session ids are derived from the marker and reused across `CreateEvent`
  retries.
- **Partial-create recovery + collision refusal.** Deploy, cleanup, and the
  residual sweep use **discovery** (`ListAgentRuntimes`, `ListMemories`), not
  only stored ids, so a create that succeeded before its response was persisted
  is recovered without a duplicate create and is still removable. A successful
  cleanup terminalizes the run: a later deploy must use fresh state/evidence
  files and a fresh marker because AgentCore may retain idempotency tokens after
  deletion. `ListMemories` summaries lack `name`, so each candidate is resolved
  with `GetMemory`. An exact-name resource whose owner/config does not match
  this run is **reported and never deleted**. Pagination is bounded with
  repeated-token and page-count guards.
- **Bounded polling, fail-fast on terminal.** Memory `ACTIVE`, Runtime `READY`,
  post-delete absence, and event eventual-consistency are each polled within a
  fixed budget; a terminal status fails immediately. Status sets are pinned to
  the official enums: Runtime terminal =
  `CREATE_FAILED`/`UPDATE_FAILED`/`DELETE_FAILED`; Memory terminal = `FAILED`.
- **Exact verification, not substrings.** The handshake is verified by parsing
  JSON and requiring the exact marker, the expected ping fingerprint, and
  `runtimeReady is True`. The memory round-trip requires `eventId`, `actorId`,
  `sessionId`, `memoryId`, and the exact payload marker to all match.
- **Scratch-only state.** `KIROCREW_SCRATCH` must be set; state and evidence
  files are forced under it.
- **Sanitized evidence.** Evidence records booleans, statuses, hashes, and an
  account suffix only. Credential/identifier-shaped keys (`token`, `arn`,
  `payload`, `session`, `requestId`, …) are refused, and error text is redacted
  for standalone 12-digit account ids, ECR URIs, ARNs, UUID/request ids,
  AgentCore resource ids, and digests. AWS request ids are **fingerprinted**,
  never persisted raw.
- **Cleanup in `finally`.** `all` tears down even if a phase raises; cleanup
  attempts **Runtime then Memory** even if the Runtime step fails, tolerates only
  `ResourceNotFoundException`, polls each resource absent, treats any inventory
  uncertainty as residue, and cannot overwrite a failed run with a clean verdict.

## Service-model pins (offline-verified at `boto3==1.43.97`)

Control plane (`bedrock-agentcore-control`):

- `CreateAgentRuntime` (required `agentRuntimeName`, `agentRuntimeArtifact`, `roleArn`; uses `description`, `networkConfiguration`, `clientToken`, `tags`)
- `GetAgentRuntime` / `DeleteAgentRuntime` (required `agentRuntimeId`)
- `ListAgentRuntimes` (paginated) and `ListTagsForResource`
- `CreateMemory` (required `name`, `eventExpiryDuration`; uses `encryptionKeyArn`, `clientToken`, `tags`)
- `GetMemory` / `DeleteMemory` (required `memoryId`), `ListMemories` (paginated)

Data plane (`bedrock-agentcore`):

- `InvokeAgentRuntime` (required `agentRuntimeArn`, `payload`; uses `runtimeSessionId`, `contentType`, `accept`)
- `CreateEvent` (required `memoryId`, `actorId`, `eventTimestamp`, `payload`;
  conversational payload uses `content: {"text": <marker>}`)
- `GetEvent` (required `memoryId`, `sessionId`, `actorId`, `eventId`)

The offline SDK tests assert each operation, its exact required and consumed
optional members, every response field the probe reads, the lifecycle status
enums, and the nested container/network and conversational-content unions. They
serialize the Runtime, Memory, event, and invocation requests through botocore
and pin exact `boto3`/`botocore` version equality. Clients are built with
explicit **dummy credentials** so collection cannot touch IMDS or any credential
provider. Drift breaks the build, not a run.

### Official API limitations honestly reflected

- `ListTagsForResource` supports AgentCore **Runtime** ARNs, not Memory, so
  Memory ownership is proven by exact name + run-specific description +
  encryption key + event-expiry, not by tags.
- `ListMemories` summaries do **not** carry `name`; discovery `GetMemory`s each
  candidate to read its name.
- An ECR **tag** (even a non-`latest` one) is mutable unless the repository has
  image-tag immutability enabled, which this probe cannot prove — so only an
  `@sha256:<digest>` reference is accepted.

## Usage

An operator runs this in a **nonproduction Platform account**, in a
documentation-supported region (see `SUPPORTED_REGIONS`), with credentials for
that account and `KIROCREW_SCRATCH` exported. State the account and region
before running.

```bash
# Example target: Platform nonproduction account 123456789012, region us-west-2.
# Run with credentials for that account. Nothing here is production.
export KIROCREW_SCRATCH="$HOME/agentcore-rm-spike-scratch"
python3 -m venv "$KIROCREW_SCRATCH/.venv"
"$KIROCREW_SCRATCH/.venv/bin/pip" install --disable-pip-version-check -r requirements.txt

RM="$KIROCREW_SCRATCH/.venv/bin/python"
COMMON=(--account-id 123456789012 --region us-west-2 --prefix aiaf-rm-spike \
  --container-uri 123456789012.dkr.ecr.us-west-2.amazonaws.com/aiaf-rm-spike@sha256:<digest> \
  --runtime-role-arn arn:aws:iam::123456789012:role/<existing-exec-role> \
  --memory-kms-key-arn arn:aws:kms:us-west-2:123456789012:key/<existing-key>)

# Full run: deploy -> verify -> exercise-memory -> cleanup (cleanup in finally).
"$RM" runtime_memory_spike.py all "${COMMON[@]}"

# Or step by step:
"$RM" runtime_memory_spike.py deploy          "${COMMON[@]}"
"$RM" runtime_memory_spike.py verify          "${COMMON[@]}"
"$RM" runtime_memory_spike.py exercise-memory "${COMMON[@]}"
"$RM" runtime_memory_spike.py cleanup         "${COMMON[@]}"
```

The container image must already exist in the target account's ECR and be
referenced by digest. Build it from `agent/` for `linux/arm64` and push it
yourself; the probe never builds, pushes, or mutates an image.

## Running the tests (offline, no AWS)

```bash
# Reuses the pinned sibling venv (boto3/botocore 1.43.97 + pytest).
../live-agentcore-gateway-spike/.venv/bin/python -m pytest -q \
  test_runtime_memory_model.py test_runtime_memory_spike.py
```

All 118 tests run offline: boto3 clients are constructed with dummy credentials
for the service-model contract tests (never touching IMDS), every consumed input
and output field is pinned, Runtime/Memory/event/invocation requests serialize
through botocore, and every orchestration test drives a fake API recorder.

## Honest limitations

- **No live evidence is produced by this code.** The probe records only what a
  live run exercises; running the tests proves the _contract_, not live AWS
  behaviour. A live run's evidence file is the only live proof.
- **Region list is documentation-derived**, not a live-verified matrix. The
  EMEA subset is called out explicitly (`EMEA_REGIONS`) but each region must be
  proven by an actual run before any regional claim is made.
- **Integrations are the next pipeline slice.** The agent imports only
  `bedrock-agentcore` and answers a deterministic handshake. `LiteLLMModel` (LLM
  Gateway), `MCPClient` (Tools Gateway), and AgentCore Memory wiring are
  deliberately **not** present — their packages are intentionally absent from
  `agent/requirements.txt` so the image cannot imply an integration this probe
  does not exercise.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
