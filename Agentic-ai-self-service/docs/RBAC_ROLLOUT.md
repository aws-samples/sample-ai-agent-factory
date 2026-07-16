# RBAC Enforcement Rollout Runbook

Scope-based RBAC (`services/rbac.py`) ships **advisory by default**
(`RBAC_ENFORCE=false`): every request is allowed, but a request that *would* be
denied logs `RBAC advisory (would-deny): ...`. This runbook takes an org from
advisory → enforced safely, without locking users out.

## Why advisory-first

Flipping straight to enforce risks 403-ing legitimate users whose Cognito group
grants weren't fully mapped. Advisory mode lets you observe real traffic and
size the blast radius before committing.

## The metric

The platform emits a CloudWatch metric from the advisory log line:

- **Namespace:** `agentcore-workflow/<env>/rbac`
- **Metric:** `WouldDeny` (count; `0` when nothing would be denied)

(Wired via a metric filter on the workflow Lambda log group — see
`_create_lambda_alarms` in `infra/stacks/platform_stack.py`.)

## Rollout steps

1. **Deploy advisory (default).** Confirm `RBAC_ENFORCE` is unset/`false` on the
   workflow + deployment Lambdas.
2. **Seed Cognito group grants.** Assign users to the resource groups
   (`g-admins-*` / `g-users-*`) per `GROUP_SCOPES` in `services/rbac.py`. Assign
   a type group (`t-admin` / `t-user`) for UI shaping.
3. **Observe for N days** (recommend ≥7). Watch the `WouldDeny` metric. A
   non-zero value means enforcing NOW would 403 that traffic — investigate which
   caller/scope (the log line names the path + held scopes) and fix the group
   grant before proceeding.
4. **Reach zero would-deny.** When `WouldDeny` sits at 0 across a representative
   window, the grants cover real usage.
5. **Enforce.** Redeploy with `-c rbac_enforce=true` (sets `RBAC_ENFORCE=true`),
   OR flip the env var on the workflow + deployment Lambdas directly for an
   instant, reversible cutover (`aws lambda update-function-configuration`).
6. **Verify + keep the rollback ready.** A read-only user should get 200 on GET,
   403 on POST. If anything breaks, set `RBAC_ENFORCE=false` again — it takes
   effect in seconds (no redeploy needed).

## Invariants (do not violate)

- Scopes gate the *capability* to call an endpoint. Per-record ownership
  (`assert_owner` / `workspace_acl`) is still authoritative for *which* rows a
  caller may touch — a scope NEVER bypasses tenant isolation.
- Local dev (no Cognito) grants all scopes; production always has a pool.
