# Personas & Access (RBAC/ABAC)

[← Back to README](../README.md)

Who can do what on the platform is driven by **AWS Cognito groups** on the
signed-in user's JWT. There is no in-app user-management screen by design —
identity + group assignment is an AWS/IdP responsibility; the platform only
*reads* the `cognito:groups` claim and maps it to capability **scopes**.

## Three layers

1. **Persona definitions** (what a group can do) — code:
   `backend/src/app/services/rbac.py` → `GROUP_SCOPES`. Mirrored in the UI at
   `frontend/src/auth/scopes.ts` (keep both in sync).
2. **The groups themselves** — created at deploy time by
   `infra/stacks/platform_stack.py` as `CfnUserPoolGroup`s in the Cognito pool.
3. **User → persona assignment** — done in AWS (console / CLI / federated IdP),
   NOT in the app (see below).

## Two dimensions (Loom-style)

- **Type group** (`t-admin` / `t-user`) — drives which **UI** sections render.
- **Resource group** (`g-admins-*` / `g-users-*`) — grants capability **scopes**
  (the enforced boundary). A user gets one type group + one or more resource groups.

## Group → scope map (source of truth: `rbac.py`)

| Group | Scopes | Persona |
|---|---|---|
| `g-admins-super` / `org-admin` | `admin` (implies all) + invoke | **Super admin** — everything |
| `g-admins-registry` / `registry-admin` | `registry:read`, `registry:write` | Registry approver (publish/approve/reject); `g-admins-super` / `org-admin` approve too |
| `g-admins-security` | `settings:read/write`, `observability:read`, `tag:read/write` | Security / settings and tag-governance admin |
| `g-admins-cost` | `cost:read/write` | FinOps admin |
| `registry-developer` | `registry:read`, `registry:write` | Registry publisher — publish and maintain own entries; cannot approve/reject |
| `g-users-default` | `invoke`, `agent:read`, `agent:write`, `cost:read`, `prompt:read`, `registry:read`, `tag:read` | **Standard user** — build/deploy/invoke own agents, select governed tags, browse + **clone** the registry |
| `editor` (legacy) | invoke + all read/write | generic editor |
| `viewer` (legacy) | invoke + all read | generic read-only |

Scope vocabulary: `invoke`, `admin`, and `<resource>:read` / `<resource>:write`
for `agent, registry, prompt, tag, cost, eval, workspace, connector, trigger,
hitl, observability, settings`.

## Registry access specifically (what a user sees + can do)

- **Browse + view + search + clone** need `registry:read` → standard users CAN
  use the org catalog (clone is a *consume* action, not a write).
- **Publish, update, and delete** need `registry:write`; a
  `registry-developer` may perform them on entries they own.
- **Approve and reject** additionally require a registry-approver group:
  `g-admins-registry`, `g-admins-super`, `registry-admin`, or `org-admin`.
- **Visibility filtering** still applies on top of scopes: a user sees APPROVED
  org/public entries + their own (incl. pending); pending entries from others are
  hidden until approved. Cross-tenant private entries are never shown (404).

## Assigning a user to a persona (AWS-side)

> **`COGNITO_USERS` users start as standard users.** The deploy's user
> provisioner puts each one in `g-users-default` + `t-user`. A user created any
> other way (a bare `AdminCreateUser`, the Cognito console) is in **no group**, so
> they sign in with an empty `cognito:groups` claim → **zero scopes** → `403` on
> every call, because the API enforces by default. Admin personas are always a
> manual grant. Changes take effect on the **next token issuance** — the user
> must sign out/in.

```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <POOL_ID> --username alice@example.com --group-name g-admins-super
# a standard user:
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <POOL_ID> --username bob@example.com --group-name g-users-default
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <POOL_ID> --username bob@example.com --group-name t-user
```
If Cognito is federated to Okta/Entra, map the IdP group claim to these names and
assignment happens in your IdP — zero platform code change.

## Enforcement is on by default

RBAC ships **enforcing** (`RBAC_ENFORCE=true`): a caller without a route's scope
gets `403`. Every API route declares a scope except `/health` and
`/api/identity/token-info` (pinned by `backend/tests/test_rbac_route_coverage.py`).
`RBAC_ENFORCE=false ./scripts/deploy.sh` is the advisory escape hatch: would-be
denials are logged + surfaced as a CloudWatch `WouldDeny` metric, but allowed — see
`RBAC_ROLLOUT.md`, which explains why this must go through `deploy.sh` rather than a
raw `cdk deploy`. In local dev (no Cognito) every scope is granted.

## Changing personas

- New capability for a persona → edit `GROUP_SCOPES` in `rbac.py` **and**
  `frontend/src/auth/scopes.ts`, redeploy.
- New persona → add a group to `GROUP_SCOPES`, seed it in `platform_stack.py`
  (`_rbac_groups`), redeploy, then assign users.
