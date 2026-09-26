# Live evidence — GA Agent Registry compatibility

- **Date:** 2026-09-19
- **Status:** PASS for the bounded `us-west-2` compatibility envelope below
- **Product Git HEAD:** `8e66dc3dab1115a3ada1b19dd09bfba06e72bd99`
- **SDK contract:** Boto3 and Botocore `1.43.98`
- **Topology:** one ephemeral IAM-authorized Registry and one tagged `CUSTOM` governance RegistryRecord

This is a sanitized summary. It contains no AWS account IDs, access keys,
credentials, Registry IDs, RegistryRecord IDs, ARNs, descriptor payload values,
or other account-scoped resource identifiers. Raw evidence remained in session
scratch and was not committed.

## Preconditions

The exact revision passed:

- Python and TypeScript CodeQL on the PR head;
- 30 focused Python tests against pinned Botocore `1.43.98` service models;
- Python compilation;
- `npm run build`;
- `npm run lint`;
- `npm run scrub`;
- `git diff --check` and language-server diagnostics;
- independent Critical/High review of the cleanup and approval paths; and
- credential-isolated validation of the deterministic rollback correction.

The cleanup-first preflight confirmed the expected Platform compatibility
account, both native CloudFormation resource types in `LIVE` state, and zero
exact-prefix stacks or Registries before creation.

## Live results

| Assertion | Result |
|---|---|
| Native resource types | `AWS::AgentRegistry::Registry` and `AWS::AgentRegistry::RegistryRecord` were `LIVE` |
| Registry creation | Registry reached `READY` |
| Governance record | Tagged `CUSTOM` descriptor round-tripped byte-semantically |
| Initial lifecycle state | Record was observed in `DRAFT` |
| Explicit approval API | `SubmitRegistryRecordForApproval` returned `APPROVED` |
| Final record state | Record remained `APPROVED` and the governance descriptor was unchanged |
| Data-plane discovery | Exactly one approved matching record was discoverable |
| Rollback precondition | Shape-valid sentinel parent Registry ID was proven absent before mutation |
| Failure injection | Otherwise-valid record targeting the absent parent failed as intended |
| CloudFormation rollback | Stack reached `UPDATE_ROLLBACK_COMPLETE` |
| Original record after rollback | Still `APPROVED`; governance descriptor unchanged |
| Invalid record after rollback | 0 records remained |
| Normal cleanup | Stack deletion completed without recovery mode |
| Runner final inventory | 0 stacks; 0 Registries |
| Evidence terminal state | `passed`; no unknown or failure event |

## Failed attempt and correction

The first run used product commit `98ed854` and intentionally paired an MCP
record type with a custom descriptor. AWS accepted that combination, so the
stack reached `UPDATE_COMPLETE` rather than rollback. The probe failed closed
instead of counting the update as proof. Its normal `finally` cleanup completed,
and direct CloudFormation plus Cloud Control inventory found zero residual
stacks and Registries.

Commit `8e66dc3` replaced that assumption with a deterministic service failure:
an otherwise-valid custom record references a 16-character alphanumeric parent
Registry ID that is verified absent immediately before `UpdateStack`. If that
sentinel ever exists, the runner refuses mutation. The focused test proves the
update call remains unreachable in that state.

## Cleanup and independent inventory

The passing run deleted the CloudFormation stack in `finally`; no residual
recovery path was needed. A separate direct AWS CLI inventory, independent of
the runner's AWS facade, then confirmed:

- caller identity matched the expected Platform compatibility account;
- `DescribeStacks` returned the exact stack as nonexistent;
- Cloud Control listed zero `AWS::AgentRegistry::Registry` resources; and
- a parent-scoped `AWS::AgentRegistry::RegistryRecord` inventory returned
  `ResourceNotFoundException` because the run-owned parent Registry was gone.

The host safety policy prevented a second arbitrary Python/SDK cleanup process
from inheriting brokered credentials. The agent did not bypass that control;
direct service inventory supplied the independent zero-residual check instead.

## Defects found and closed

1. Cleanup waits treated pre-delete terminal states as immediately fatal and
   could skip the residual control-plane sweep.
2. A record already `APPROVED` at the first poll could skip the explicit submit
   call while the run still passed.
3. The original descriptor/type mismatch was accepted by the live service and
   could not prove rollback.
4. Literal fake AWS access-key fixtures triggered the repository's secret hook;
   tests now construct detector sentinels at runtime without an allow rule.

Each code correction passed focused tests, build/lint/scrub, PR CI, and review
before the next live attempt. Both live attempts cleaned to independently
verified zero residue.

## Bounded conclusion and remaining gates

This proves the GA Agent Registry CloudFormation, control-plane approval,
custom-governance descriptor, data-plane discovery, update rollback, and clean
teardown contracts for pinned Boto3/Botocore `1.43.98` in one `us-west-2`
Platform compatibility account.

It does **not** yet prove:

- blue-green migration of the pipeline-owned Platform Registry;
- cross-account `RegistryReaderRole` consumption from a Workstream;
- integration with the real Workstream Gateway, PolicyEngine, Runtime, Memory,
  or generated agent;
- per-tool Cedar enforcement from GA Registry governance records;
- EMEA regional compatibility;
- load, concurrency, quota, chaos, upgrade, or long-duration soak behavior; or
- final multi-account environment teardown.

Those remain release blockers. This evidence is a prerequisite for the
pipeline-owned Workstream vertical slice, not a broad-adoption readiness claim.
