# Live evidence — final multi-account teardown and orphan sweep

- **Date:** 2026-09-26
- **Status:** PASS — every deletable project resource is gone; KMS keys are in their cancellable seven-day deletion window. One AWS service-linked workload identity and nine protected buckets/keys remain by design. The temporary Platform teardown inline policy was removed and its absence verified with authoritative `GetRolePolicy == NoSuchEntity`.
- **Teardown implementation:** `9a564df` (`scripts/final_teardown.py`, `scripts/sweep_orphans.py`, `scripts/residue_inventory.py`); the files were byte-identical at the execution HEAD.
- **Regions:** `us-west-2` and `us-east-1`
- **Accounts:** Workstream, Platform, Management/Governance (identifiers omitted)

This is a sanitized summary. It contains no credentials, account identifiers,
stack IDs, resource ARNs, bucket names, key IDs, workload-identity suffixes or
log-group suffixes. Raw plans remain in session scratch; live provenance remains
in CloudFormation and CloudTrail.

## Sequence and observed result

| Scope | Action | Result |
| --- | --- | --- |
| Workstream, `us-west-2` | Delete the six exact production/nonproduction RuntimeMemory, ToolGateway and RegistryRoles stacks, then clean retained/service-created resources and image digests | Six `DELETE_COMPLETE`; retained cleanup complete; six image digests removed; one stack key scheduled for seven-day deletion; zero live matched resources |
| Workstream, `us-east-1` | Apply the reviewed historical-orphan plan | 61 log groups, two stub user pools and three empty buckets deleted; 92 keys scheduled for seven-day deletion |
| Workstream, `us-east-1` exception | Delete the last workload identity | AWS returned `ValidationException`: the service-linked identity cannot be deleted by the caller. A live owner probe found zero Gateways, Runtimes, Memories and Registries to unlink. It is retained and called out explicitly rather than reported as clean |
| Platform, `us-west-2` | Delete both pipeline roots and six stage stacks | CloudTrail records all eight exact `DeleteStack` calls by the user's Isengard session; subsequent live preflight found all eight absent |
| Platform, `us-west-2` | Apply final orphan plan | Eight service-created Lambda log groups deleted; four enabled cross-account M2M-secret keys scheduled for seven-day deletion; zero live matched resources |
| Management, `us-west-2` | Delete `Nonprod-Audit` and `Nonprod-LogArchive`, then apply final orphan plan | Two `DELETE_COMPLETE`; stack-created service log removed; one older orphan service log removed; zero live matched resources |
| Management, `us-east-1` | Audit and clean historical D03 residue | 61 log groups deleted; 42 keys scheduled for seven-day deletion after the ownership and reference checks below |

The pipeline transitions remained disabled throughout the campaign, preventing a
new execution from recreating resources during deletion. `CDKToolkit` and the
existing GitHub connection were deliberately preserved.

## Historical Management-account ownership proof

A first post-teardown inventory found 61 D03-named log groups and 42 enabled keys
in `us-east-1`, beyond the original `us-west-2` plan. They were not deleted on a
name match alone.

1. CloudFormation history contained 20 deleted stacks with the exact
   `AgenticAI-D03-PlatformCoreStack` / Workstream Gateway contracts.
2. Deleted-stack resource summaries mapped 54/61 log groups and 26/42 keys
   directly to those stack IDs. Lambda physical IDs were mapped to their exact
   `/aws/lambda/<physical-id>` groups.
3. The remaining seven logs and sixteen keys predated CloudFormation's retained
   stack-resource history (April–May). Their physical-name/description contracts
   were exact, and their creation timestamps paired with the older D03 runs.
4. All 42 candidate keys had zero aliases, zero active grants and zero references
   from surviving CloudWatch Logs, DynamoDB, S3, Secrets Manager, Lambda, ECR,
   SNS, SQS, CloudTrail or SSM resources. Every scan surface completed without an
   authorization or API error.
5. The sweep rechecked that no project stack had reappeared and recomputed the
   live predicate before deleting any log or scheduling any key.

## Terminal inventory

| Account / Region | Live project resources | Enabled project keys | Pending-deletion project keys | Sanctioned residue |
| --- | ---: | ---: | ---: | --- |
| Workstream / `us-west-2` | 0 | 0 | 11 | none |
| Workstream / `us-east-1` | one service-linked identity; nine protected buckets | 9 | 92 | eight COMPLIANCE Object Lock buckets through 2033; one legal-hold bucket; one key per bucket |
| Platform / `us-west-2` | 0 | 0 | 21 | none |
| Platform / `us-east-1` | 0 | 0 | 0 | none |
| Management / `us-west-2` | 0 | 0 | 4 | none |
| Management / `us-east-1` | 0 | 0 | 42 | none |

Every other measured surface is zero: CloudFormation stacks, CodePipeline,
CodeBuild, Lambda, Step Functions, CloudWatch log groups/delivery sources,
AgentCore Runtime/Memory/Gateway/PolicyEngine/Identity/OAuth providers, Agent
Registry, Secrets Manager, SSM, DynamoDB, Cognito, Bedrock Guardrails,
CloudWatch alarms, ECR repositories, IAM roles and unprotected project buckets.

## Protection proof

A fresh Workstream `us-east-1` plan found no deletable log groups, user pools,
buckets or enabled keys. It independently read each surviving object version:

- eight record-keeping buckets have COMPLIANCE retention ending in May or June
  2033;
- one evaluation-corpus bucket has an active legal hold;
- each of the nine enabled keys encrypts one of those protected buckets and was
  therefore excluded by the live sweep predicate.

The legal hold is a separate owner decision. No attempt was made to bypass it.

## Immutable plan provenance

| Plan | SHA-256 |
| --- | --- |
| Platform `us-west-2` final orphan plan | `9db0b2b6a166eaa01f9c9439c5874f2a11d9d05a8b0bbb1470d558e6a387db87` |
| Management `us-west-2` final orphan plan | `4cc31a678ff2cb2c587241b38b532d276576315ffb558e1e61dbcef0d553a778` |
| Management `us-east-1` historical-orphan plan | `b5b6a458f8dd4643dde6d707a531732e800d88ebe5cdae0204c08a77201894d7` |
| Workstream `us-east-1` terminal protection plan | `c14c70d0bb463585684e4e2bedf4c8fcd1c29d458532ee64ac89f1426000eb67` |
| Management stack teardown resumable plan | `5e817de62eb3bfb7166a4cb068eaa8fa0856826f40785a7217a2bb6712d393bd` |

## Closure check

The Platform CloudFormation execution role's temporary inline policy
`AgenticAI-TeardownListPolicyEntities` was removed after stack deletion. A final
`ListRolePolicies` returned an empty set and an authoritative `GetRolePolicy`
returned `NoSuchEntity`. No temporary teardown permission remains.
