# Live evidence — dependency-ordered teardown of the generated-agent revision

- **Date:** 2026-09-24
- **Status:** PASS — zero residue for this revision in both accounts; two teardown defects found live and fixed
- **Torn-down revision:** the deployment proven in [`2026-09-23-pipeline-generated-agent.md`](2026-09-23-pipeline-generated-agent.md) and the rollback campaign (`e5760e5`)
- **Fix commits (this campaign):** `6c42395` (provider-secret `DeleteSecret` grant), `0994732` (teardown by-name dependency guard), `8ecc88e` (fail-closed stranded-stack recovery)
- **Region:** `us-west-2`
- **Scope:** Workstream test account — both environments' `RuntimeMemory`, `ToolGateway` and `RegistryRoles` stacks plus every service-side residue; Platform account — the Workload pipeline root only. The Platform pipeline and its per-environment Registry / InferenceGateway / Guardrail stacks are shared platform and were deliberately left in place.

This is a sanitized summary. It contains no AWS account IDs, credentials,
stack IDs, Gateway/target/identity/provider IDs, KMS key IDs, bucket names,
log-group suffixes or other account-scoped physical identifiers. Raw evidence
remained in CloudFormation stack events, CloudTrail and session scratch.

## Sequence as it actually happened

| Step                                                                                                                                                                    | Actor                     | Result                                                                                                                                                                                                                                                                             |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Baseline inventory (stacks, Runtimes, Memories, Gateways, identities, providers, managed secrets, CMKs, log groups, six agent image digests incl. the rejected fixture) | agent, read-only          | recorded before any deletion                                                                                                                                                                                                                                                       |
| `delete-stack` RuntimeMemory ×2, ToolGateway ×2, RegistryRoles ×2, issued together                                                                                      | operator                  | RegistryRoles `DELETE_COMPLETE`; RuntimeMemory `DELETE_FAILED`; ToolGateway delete **cancelled** by CloudFormation (`Cannot delete export … in use by …-RuntimeMemory`)                                                                                                            |
| Diagnose RuntimeMemory                                                                                                                                                  | agent                     | only `InferenceCredProvider` failed: `DeleteOauth2CredentialProvider … not authorized to perform: secretsmanager:DeleteSecret` (defect 1). Runtime, Memory, scan gate and the nonproduction CMK had already deleted; the production CMK was `DELETE_SKIPPED` (`RETAIN`, by design) |
| Repair the stack-owned provider role in place with the exact fixed statement, retry RuntimeMemory ×2                                                                    | operator                  | both `DELETE_COMPLETE`; the provider **and its managed secret** deleted as the caller                                                                                                                                                                                              |
| Retry ToolGateway ×2                                                                                                                                                    | operator                  | `DELETE_IN_PROGRESS` for one hour, then `DELETE_FAILED`: `CloudFormation did not receive a response from your Custom Resource`                                                                                                                                                     |
| Diagnose ToolGateway                                                                                                                                                    | agent                     | the custom-resource execution roles had been deleted **with RegistryRoles** (defect 2): the Lambda invoke failed before the handler ran (no log stream, no `cfn-response`), targets still `READY` in the service                                                                   |
| Delete the four orphaned targets and two Gateways directly through the service API, after ownership checks                                                              | agent                     | control plane at zero Gateways / identities                                                                                                                                                                                                                                        |
| Recreate the four execution roles with delete-path grants, retry, remove roles                                                                                          | operator (hand-run block) | failed again: the role lacked `bedrock-agentcore:DeleteGatewayTarget`, which the retried `DELETE_FAILED` target resources call first; the block also removed the roles despite the failed wait                                                                                     |
| `scripts/recover-stranded-toolgateway.sh` (fail-closed)                                                                                                                 | operator                  | both ToolGateway `DELETE_COMPLETE` in ~3 min; roles removed only after that                                                                                                                                                                                                        |
| `delete-stack` Workload pipeline root                                                                                                                                   | operator                  | `DELETE_COMPLETE`; no `DELETE_SKIPPED`/`DELETE_FAILED` resources; artifact bucket auto-emptied and removed; pipeline CMK entered pending deletion                                                                                                                                  |
| Residue sweep                                                                                                                                                           | agent                     | see below                                                                                                                                                                                                                                                                          |

## Defects found only by this teardown (each fixed with a regression test)

1. **`DeleteOauth2CredentialProvider` requires `secretsmanager:DeleteSecret` on the provider's managed secret.** Mirror image of the `CreateSecret` dependency found on 09-23. Fixed by granting `DeleteSecret` on exactly `bedrock-agentcore-identity!default/oauth2/<providerName>-*`; the IAM simulator proves own-secret allowed and other-environment / Platform secrets denied.
2. **`teardown.sh` could delete a by-name producer after its consumer failed.** `ToolGateway` imports its custom-resource execution roles from `RegistryRoles` by name, invisible to CloudFormation. The script now records the producers of a failed consumer and reports them `BLOCKED` instead of destroying them; the regression fails when the guard is removed (mutation-checked). The runbook documents the recovery, and `scripts/recover-stranded-toolgateway.sh` performs it fail-closed (wrong-account refusal; roles kept for diagnosis on a failed wait; roles removed only when both stacks are gone; stub-driven checks in the jest teardown suite).

## Zero-residue inventory

Workstream test account, this revision, measured against the baseline:

| Surface                                                                                                 | Before                             | After                             |
| ------------------------------------------------------------------------------------------------------- | ---------------------------------- | --------------------------------- |
| CloudFormation stacks                                                                                   | 6                                  | 0                                 |
| IAM roles / Lambda functions / Step Functions state machines                                            | all present                        | 0 / 0 / 0                         |
| AgentCore Gateways / targets / workload identities / OAuth2 providers / Runtimes / Memories             | 2 / 4 / 4 / 2 / 2 / 2              | 0 for every kind                  |
| Secrets Manager (incl. planned deletion)                                                                | 2 service-managed provider secrets | 0                                 |
| Agent image digests in the CDK asset repository (incl. the rejected High-finding fixture)               | 6                                  | 0                                 |
| CloudWatch log groups (Runtime, custom-resource Lambdas, scan gate, validators)                         | 50                                 | 0                                 |
| Memory CMKs (nonproduction scheduled by CloudFormation; production `RETAIN`, scheduled by the operator) | 2 Enabled                          | 2 `PendingDeletion`, 7-day window |

The only Enabled customer-managed keys remaining in that shared test account
belong to an unrelated project and were not touched.

Platform account: the Workload pipeline root's pipeline, five CodeBuild
projects, eleven roles, artifact bucket and auto-delete Lambda are gone; its
CMK is `PendingDeletion`; the five CodeBuild log groups and the auto-delete
Lambda's log group (never deleted by CloudFormation) were removed by exact
name. The Platform pipeline root and its six per-environment stacks remain
`UPDATE_COMPLETE`/`CREATE_COMPLETE`, unchanged since before the campaign.
The dedicated generated-agent Cognito clients and the two published
cross-account M2M secrets are Platform-owned and stay with it by design.

## Honest residuals

- Two Memory CMKs and one pipeline CMK are pending deletion (7 days), which
  is the shortest window KMS allows; cancellation is possible until then.
- Log groups and asset images had to be removed outside CloudFormation; the
  teardown script's exact-id service-log cleanup covers CodeBuild, Lambda and
  Runtime groups, and the shared CDK asset repository is intentionally not
  managed by any workload stack.
- The by-name role import between `RegistryRoles` and `ToolGateway` remains
  an architectural dependency CloudFormation cannot see; it is now guarded in
  the script and documented, not eliminated.
