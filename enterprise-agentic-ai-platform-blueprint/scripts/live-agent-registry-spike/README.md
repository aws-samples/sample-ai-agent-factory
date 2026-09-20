# GA Agent Registry compatibility spike

This cleanup-first probe validates the **GA AWS Agent Registry** namespace before the blueprint replaces its DynamoDB and public-preview Registry placeholders.

The public-preview `bedrock-agentcore` Registry namespace reached its documented support deadline on 2026-09-17. New platform code must use `agent-registry-control`, `agent-registry`, `AWS::AgentRegistry::Registry`, and `AWS::AgentRegistry::RegistryRecord` instead.

> **Live proof.** Exact product commit `8e66dc3` passed the complete bounded
> `us-west-2` contract on 2026-09-19, including explicit approval, discovery,
> rollback, normal cleanup, and direct independent zero-residual inventory. See
> [`../../evidence/live/2026-09-19-agent-registry-compatibility-spike.md`](../../evidence/live/2026-09-19-agent-registry-compatibility-spike.md).

## What it proves

A successful `all` run proves, in the selected account and Region, that:

1. `AWS::AgentRegistry::Registry` and `AWS::AgentRegistry::RegistryRecord` are `LIVE` public CloudFormation types.
2. CloudFormation creates an IAM-authorized registry and a tagged `CUSTOM` governance record.
3. The fixed governance document round-trips byte-semantically through `GetRegistryRecord` without abusing the strict MCP descriptor schema.
4. A DRAFT record can be submitted and reaches `APPROVED` under `APPROVE_ALL`.
5. The approved record is visible through the `agent-registry` data plane.
6. An otherwise valid custom record targeting a pre-verified nonexistent parent Registry fails an update, CloudFormation reaches `UPDATE_ROLLBACK_COMPLETE`, the approved record survives, and the invalid record does not.
7. Stack deletion plus an independent control-plane inventory leaves zero exact-prefix registries.

It does **not** prove Workstream integration, cross-account `RegistryReaderRole`, Cedar enforcement, or EMEA regional support. Those remain later pipeline and region-matrix gates.

## Safety boundary

- Use only a disposable Platform **test** account.
- Every resource carries `agenticai:test-run` plus the platform's five required allocation tags.
- Cleanup refuses a stack, registry, or record whose ownership tags differ.
- State and evidence paths must resolve below `$KIROCREW_SCRATCH`; symlink escapes are rejected.
- Evidence contains fixed test metadata, statuses, request IDs, ARNs, and resource IDs only. Sensitive key names and JWT-shaped values are rejected recursively.
- A successful verdict requires observing `DRAFT` and calling `SubmitRegistryRecordForApproval`; creation that is already `APPROVED` fails closed rather than silently skipping the transition proof.
- The runner owns a `finally` cleanup. A stack-delete failure cannot bypass the tag-owned residual sweep; even when recovery reaches zero inventory, the compatibility verdict remains failed. Run `cleanup` again independently after `all` and confirm the original evidence still says `passed` before publishing a result.

## Environment

Use an isolated environment from exact pins:

```bash
python3 -m venv "$KIROCREW_SCRATCH/agent-registry-venv"
"$KIROCREW_SCRATCH/agent-registry-venv/bin/pip" install \
  -r scripts/live-agent-registry-spike/requirements.txt
```

Use the repository commit under test, never a later documentation-only head:

```bash
GIT_HEAD="$(git rev-parse HEAD)"
PREFIX="aiaf-ar-spike-$(date +%H%M%S)"
```

## Run order

First run the read-only preflight:

```bash
"$KIROCREW_SCRATCH/agent-registry-venv/bin/python" \
  scripts/live-agent-registry-spike/agent_registry_spike.py preflight \
  --account-id '<PLATFORM_TEST_ACCOUNT_ID>' \
  --region '<AWS_REGION>' \
  --prefix "$PREFIX" \
  --git-head "$GIT_HEAD"
```

Then run the full create/approve/discover/rollback/cleanup proof:

```bash
"$KIROCREW_SCRATCH/agent-registry-venv/bin/python" \
  scripts/live-agent-registry-spike/agent_registry_spike.py all \
  --account-id '<PLATFORM_TEST_ACCOUNT_ID>' \
  --region '<AWS_REGION>' \
  --prefix "$PREFIX" \
  --git-head "$GIT_HEAD"
```

Finally run cleanup independently with the same prefix:

```bash
"$KIROCREW_SCRATCH/agent-registry-venv/bin/python" \
  scripts/live-agent-registry-spike/agent_registry_spike.py cleanup \
  --account-id '<PLATFORM_TEST_ACCOUNT_ID>' \
  --region '<AWS_REGION>' \
  --prefix "$PREFIX" \
  --git-head "$GIT_HEAD"
```

The runner prints the evidence path. Raw evidence stays in session scratch. Only a sanitized, account-redacted exact-commit summary may be committed under `evidence/live/`.

## Offline guards

Offline tests supplement but never replace live AWS evidence:

```bash
pytest scripts/live-agent-registry-spike/test_agent_registry_spike.py -q
python3 -m py_compile \
  scripts/live-agent-registry-spike/agent_registry_spike.py \
  scripts/live-agent-registry-spike/test_agent_registry_spike.py
```

---

# Manual governance approval for the R1 pipeline Registry producer

`approve_pipeline_registry.py` is a **separate** utility from the compatibility
spike above. It performs the human governance step that the Platform pipeline
deliberately does **not** automate: given a `Nonprod-Registry` or
`Prod-Registry` stack the pipeline **already deployed**, it proves every
expected `RegistryRecord` is exactly the catalogued `DRAFT` governance record,
then submits each one, exactly once, for approval.

> **This is manual governance approval, NOT a Workstream deployment.** It
> creates nothing, never updates or deletes a record, and never touches a
> Workstream account. It submits already-deployed DRAFT records for approval and
> confirms they reach `APPROVED` and become discoverable. The pipeline still
> owns every resource. All emission continues to flow through GitHub PR + the
> blueprint's workload pipeline; there is no fast-track here.

## What it does

`verify` (read-only) and `approve` both run the same fail-closed preflight
against the pipeline-owned producer:

1. Confirm the STS caller account equals `--account-id`.
2. Require the exact stack name (`Nonprod-Registry` / `Prod-Registry`) in
   `CREATE_COMPLETE` or `UPDATE_COMPLETE`.
3. Require exactly one `AWS::AgentRegistry::Registry` stack resource and exactly
   the expected `AWS::AgentRegistry::RegistryRecord` resources — one per
   `--expected-tool-id`, no extras, no duplicates, all `*_COMPLETE`.
4. Require the registry to be named `agenticai-platform-<env>-v1`, `READY`,
   `AWS_IAM`, with auto-approval rules exactly `["APPROVE_ALL"]`.
5. Require the exact five user tags (`application-id`, `agent-id`, `tenant-id`,
   `cost-centre`, `environment`) on the registry and on every record; only
   `aws:cloudformation:*` system tags are tolerated alongside them.
6. Load the processed CloudFormation template used for the live stack and
   require its Registry and complete RegistryRecord set, tags, and immutable
   record properties to match the expected environment.
7. Require each template and live record's `CUSTOM` descriptor JSON to carry
   `schemaVersion=agenticai.tool-governance/1.0`, matching `toolId`,
   `authorization.defaultDecision=DENY`, and `desiredApprovalStatus=approved`.
8. Require every live descriptor to match the processed template exactly,
   including target ARN, MCP schema, Cedar policy, entitlements, and ownership.
9. Require **every** record to be `DRAFT`. If any expected record is missing,
   unexpected, non-`DRAFT`, or malformed, **nothing is submitted**.

`approve` additionally, only after the whole preflight passes:

10. Submits each record exactly once, in tool-id sorted order, via
   `agent-registry-control SubmitRegistryRecordForApproval`.
11. Polls each record to `APPROVED` under a bounded timeout, then re-verifies the
   governance descriptor is JSON-semantically identical to what preflight read.
12. Polls the `agent-registry` data plane until
    `ListDiscoverableRegistryRecords` returns exactly the expected approved ids.
13. Writes raw but credential-free evidence under `$KIROCREW_SCRATCH`.

It fails closed on any unrecognised SDK response shape, and the evidence writer
rejects credential- and JWT-shaped values recursively.

## Run order

Always run `verify` first — it is read-only and submits nothing:

```bash
GIT_HEAD="$(git rev-parse HEAD)"
"$KIROCREW_SCRATCH/agent-registry-venv/bin/python" \
  scripts/live-agent-registry-spike/approve_pipeline_registry.py verify \
  --account-id '<PLATFORM_ACCOUNT_ID>' \
  --region '<AWS_REGION>' \
  --environment nonprod \
  --application-id '<APPLICATION_ID>' \
  --agent-id '<AGENT_ID>' \
  --tenant-id '<TENANT_ID>' \
  --cost-centre '<COST_CENTRE>' \
  --expected-tool-id tool-echo \
  --expected-tool-id tool-ping \
  --git-head "$GIT_HEAD"
```

Then, only once `verify` passes and a human has authorised approval, run
`approve` with the identical arguments:

```bash
"$KIROCREW_SCRATCH/agent-registry-venv/bin/python" \
  scripts/live-agent-registry-spike/approve_pipeline_registry.py approve \
  --account-id '<PLATFORM_ACCOUNT_ID>' \
  --region '<AWS_REGION>' \
  --environment nonprod \
  --application-id '<APPLICATION_ID>' \
  --agent-id '<AGENT_ID>' \
  --tenant-id '<TENANT_ID>' \
  --cost-centre '<COST_CENTRE>' \
  --expected-tool-id tool-echo \
  --expected-tool-id tool-ping \
  --git-head "$GIT_HEAD"
```

Optional flags: `--evidence-file` (must resolve below `$KIROCREW_SCRATCH`;
defaults to `registry-approval/<env>-<account>/evidence.json`),
`--timeout-seconds` (30–3600, default 900), `--poll-interval-seconds` (1–60,
default 5). Repeat `--expected-tool-id` once per catalogued tool.

## Safety boundary

- Runs against a Platform account only; it never assumes into or deploys to a
  Workstream account.
- `SubmitRegistryRecordForApproval` is the **only** mutating call. It never
  updates or deletes a record or registry, and never creates CloudFormation
  resources.
- Atomic preflight: it reads every record before submitting any. A single
  missing / unexpected / non-`DRAFT` / malformed record aborts with zero
  submissions.
- **Residual risk — atomicity ends at submission.** Preflight is all-or-nothing,
  but submission is not transactional: if a later record fails to reach
  `APPROVED` (timeout or terminal status), the records already submitted in that
  run stay submitted. The run fails and the evidence names exactly which records
  were submitted; re-run `verify` to read the resulting state. Nothing is ever
  rolled back, updated, or deleted.
- Evidence resolves below `$KIROCREW_SCRATCH`; escapes are rejected. Sensitive
  key names and JWT/access-key/bearer-shaped values are rejected recursively.
- Exit codes: `0` pass, `1` runtime/contract failure (evidence records the
  failure), `2` invalid CLI input or an unusable evidence path (AWS is never
  contacted).

## Offline guards

Offline tests use in-process fakes and never import boto3, read credentials, or
reach AWS. They supplement — never replace — live approval evidence:

```bash
pytest scripts/live-agent-registry-spike/test_approve_pipeline_registry.py -q
python3 -m py_compile \
  scripts/live-agent-registry-spike/approve_pipeline_registry.py \
  scripts/live-agent-registry-spike/test_approve_pipeline_registry.py
```

The suite proves: CLI/config validation; unexpected/missing/extra/duplicate
records; wrong tags/name/status/schema/default-decision/desired-status; the
all-`DRAFT` atomic preflight (no partial submissions); exact submit count and
sorted order; processed-template-to-live descriptor equality; polling failure
and bounded timeout; eventual discovery convergence and discovery mismatch;
descriptor mutation detection; evidence redaction and scratch confinement; an
unusable evidence path exiting `2` without a traceback; that the module imports
with no AWS SDK present; and end-to-end CLI behaviour.

## What it does NOT prove

Passing offline tests or one green `approve` run is **not** evidence for the
complete migration. R1 producer deployment and explicit approvals passed in
both reference Platform environments, but the R2 Workstream consumer,
matching-validator reader path, wrong-ExternalId/session twins, consumer
rollback, teardown, and EMEA AgentCore region matrix remain outstanding. Each
utility run proves one Platform account, Region, and environment only.
