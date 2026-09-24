# Live evidence — redeploy after teardown: Platform tool-alias grant retirement

- **Date:** 2026-09-24
- **Status:** PASS — stale grants proven live, wrong fix rejected live, two-phase repair proven live; contract documented
- **Fix commits:** `4abd5cd` (RoleId-bound permissions — reverted), `d2186b9` (explicit two-phase retire/regrant contract, diagnostic `GatewayServiceRoleId` output, tests, README §6.3/§16, runbook rows)
- **Region:** `us-west-2`
- **Scope:** Platform account — the Platform pipeline and both environments' `Registry` stacks; Workstream test account — both environments' `RegistryRoles` stacks (read-only)

This is a sanitized summary. It contains no AWS account IDs, credentials,
stack IDs, IAM unique IDs, change-set IDs or other account-scoped physical
identifiers. Raw evidence remained in CloudFormation stack events, CloudTrail
and session scratch.

## What was observed

The Workload pipeline root was recreated from the recovered context of the
deleted root (31 resources, `CREATE_COMPLETE`, no failed events) and the
pipeline auto-started on the branch tip. After both `RegistryRoles` stacks
deployed, the Gateway service roles existed with **new** IAM RoleIds. Reading
each Platform tool alias policy showed that all four statements still carried
the **deleted** roles' RoleIds as `AROA...` principals: Lambda stores a role
principal as its RoleId, and the previous teardown had deleted the roles
without first retiring the grants (the 09-21 and 09-22 campaigns did retire
them). With ARN-only permission resources, re-running the Platform pipeline
would have been a template no-op; approving `GatewayPermissionReady` would
have failed later at `CreateGatewayTarget`.

## Wrong fix, rejected live

`4abd5cd` bound each `AWS::Lambda::Permission` identity to the role's RoleId
so that a recreated role produced a replacement. Run live, the nonproduction
`Registry` deploy failed with `AWS::Lambda::Permission CREATE_FAILED — The
provided principal was invalid` for both aliases and rolled back cleanly
(`UPDATE_ROLLBACK_COMPLETE`; no other resource changed). The role existed and
had not been replaced. A reversible `add-permission` probe with the same,
valid role ARN reproduced the rejection exactly: Lambda refuses every further
`AddPermission` on an alias whose policy still names a deleted principal, and
CloudFormation replaces resources create-before-delete, so no single-phase
update can repair a stale grant. The RoleId inputs were reverted in `d2186b9`.

Two sequencing lessons were also recorded: updating a CodePipeline through
CloudFormation auto-starts an execution (a root update made before the code was
pushed ran the old code, whose self-mutate re-baked the old context), and the
GitHub push trigger did not fire for either pipeline in this account, so runs
were started explicitly.

## Two-phase repair, proven live

1. **Retire.** Platform root updated to `enableGaGatewayInvokePermissions=false`
   (the only context change: the flag and role ARNs removed). The auto-started
   run on `d2186b9` deleted both nonproduction statements; `get-policy` then
   returned `ResourceNotFoundException` on both aliases; after `SecurityReview`,
   the production statements were deleted the same way. All four aliases had no
   resource policy.
2. **Grant.** Platform root updated to `enableGaGatewayInvokePermissions=true`
   with exactly the two `GatewayServiceRoleArn` outputs. The auto-started run
   recreated the four permissions under their original ARN-derived logical ids;
   every alias policy now names the live role ARN (no `AROA...`), verified per
   alias before each approval.
3. `GatewayPermissionReady` was approved only after both environments were
   verified; the Workload pipeline continued into the AgentCore propagation
   window.

## Contract now documented

- Retire the Platform permission phase **before** deleting `RegistryRoles`
  (README §16); recover a stale grant with the same off-then-on toggle
  (README §6.3, runbook rows naming the exact Lambda error).
- `RegistryRoles` exposes `GatewayServiceRoleId` as a diagnostic: a Platform
  alias grant showing a different `AROA...` principal is stale.
- The `GatewayPermissionReady` gate text names the verifiable condition.
- Conformance pins the permission identity as ARN-derived and stable across a
  retire/regrant cycle, and that the disabled phase renders no permission.

## Honest residuals

- The retirement contract is an operator procedure enforced by documentation
  and by Lambda's own rejection; no synth-time check can see live alias
  policies. A pre-deploy alias-policy preflight in the Platform pipeline would
  turn the Lambda error into an explicit, named failure and remains open.
- The teardown evidence of the same day was amended; its "zero residue" claim
  did not cover Platform-side grants to the deleted roles.
