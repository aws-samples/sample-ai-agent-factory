# RBAC Enforcement Rollout Runbook

[← Back to README](../README.md)

Scope-based RBAC (`services/rbac.py`) ships **enforcing by default**
(`RBAC_ENFORCE=true` on the workflow + deployment Lambdas): a caller without a
route's scope gets `403`. Every API route declares a scope except `/health` and
`/api/identity/token-info`, and `backend/tests/test_rbac_route_coverage.py` fails
if a new route ships without one. Only an explicit `false` / `0` / `no` / `off`
selects advisory mode; an absent or misspelled value enforces.

Users created by the deploy (`COGNITO_USERS`) are put in `g-users-default` +
`t-user` — a standard user who builds, deploys and invokes their own agents. A
standard user also has `tag:read`, so tag-policy/profile loading cannot lock the
Deploy panel; tag-policy administration remains with `g-admins-security`. A user
in **no** group holds no scopes and is denied every call.

## Upgrading an existing deployment

Before this default, users were created in no group and the API was advisory, so
an ungrouped user worked. After the upgrade:

- **`COGNITO_USERS` users** are added to `g-users-default` + `t-user` by the
  upgrade deploy itself. Their passwords are not touched: the provisioner's
  Update only syncs groups when the pool and email are unchanged.
- **Users created any other way** (console, CLI, an IdP without a group mapping)
  get `403` until they are assigned a group (see PERSONAS.md). To find them before
  upgrading, list the pool's users and compare with each group's members
  (`aws cognito-idp list-users-in-group`).

If you cannot seed the grants first, upgrade in advisory mode and follow the
steps below.

## The advisory escape hatch

`RBAC_ENFORCE=false ./scripts/deploy.sh` allows every request, but a request that
*would* be denied logs `RBAC advisory (would-deny): ...`. Use it to size the blast
radius on an upgrade, then return to enforcing.

The platform emits a CloudWatch metric from the advisory log line:

- **Namespace:** `agentcore-workflow/<env>/rbac`
- **Metric:** `WouldDeny` (count; `0` when nothing would be denied)

(Wired via a metric filter on the workflow Lambda log group — see
`_create_lambda_alarms` in `infra/stacks/platform_stack.py`.)

1. **Deploy advisory.** `RBAC_ENFORCE=false ./scripts/deploy.sh`; confirm
   `RBAC_ENFORCE=false` on the workflow + deployment Lambdas.
2. **Seed Cognito group grants.** Assign users to the resource groups
   (`g-admins-*` / `g-users-*`) per `GROUP_SCOPES` in `services/rbac.py`. Assign
   a type group (`t-admin` / `t-user`) for UI shaping.
3. **Observe** (recommend ≥7 days). Watch the `WouldDeny` metric. A non-zero value
   means enforcing NOW would 403 that traffic — investigate which caller/scope
   (the log line names the path + held scopes) and fix the group grant.
4. **Reach zero would-deny** across a representative window.
5. **Enforce.** Redeploy with `./scripts/deploy.sh` (enforcing is the default), OR
   flip the env var on the workflow + deployment Lambdas directly for an instant,
   reversible cutover (`aws lambda update-function-configuration`).

   Go through `deploy.sh`, not a raw `cdk deploy -c rbac_enforce=...`. The
   `COGNITO_USERS` carry-forward guard lives in `deploy.sh`, so a bare `cdk
   deploy` with the context flag but no `-c cognito_users=...` drops every user
   provisioner and **deletes the users it created** — see the CHANGELOG entry
   "a plain redeploy silently deleted every provisioned Cognito user".
6. **Verify + keep the rollback ready.** A user in no group should get 403 on
   every route but `/health` and `/api/identity/token-info`; a standard user 200
   on their own agents and 403 on `/api/admin/*`. If anything breaks, set
   `RBAC_ENFORCE=false` again — it takes effect in seconds (no redeploy needed).

## Invariants (do not violate)

- Scopes gate the *capability* to call an endpoint. Per-record ownership
  (`assert_owner` / `workspace_acl`) is still authoritative for *which* rows a
  caller may touch — a scope NEVER bypasses tenant isolation.
- Local dev (no Cognito) grants all scopes; production always has a pool.
