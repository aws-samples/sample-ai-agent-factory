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
