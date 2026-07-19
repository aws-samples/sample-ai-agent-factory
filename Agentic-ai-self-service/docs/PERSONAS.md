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
| `g-admins-registry` / `registry-admin` | `registry:read`, `registry:write` | Registry approver (publish/approve/reject) |
| `g-admins-security` | `settings:read/write`, `observability:read` | Security / settings admin |
| `g-admins-cost` | `cost:read/write` | FinOps admin |
| `g-users-default` | `invoke`, `agent:read`, `cost:read`, `prompt:read`, `registry:read` | **Standard user** — build/deploy/invoke own agents, browse + **clone** the registry |
| `editor` (legacy) | invoke + all read/write | generic editor |
| `viewer` (legacy) | invoke + all read | generic read-only |

Scope vocabulary: `invoke`, `admin`, and `<resource>:read` / `<resource>:write`
for `agent, registry, prompt, tag, cost, eval, workspace, connector, trigger,
hitl, observability, settings`.

## Registry access specifically (what a user sees + can do)

- **Browse + view + search + clone** need `registry:read` → standard users CAN
  use the org catalog (clone is a *consume* action, not a write).
- **Publish, approve, reject, update, delete** need `registry:write` → registry
  admins only.
- **Visibility filtering** still applies on top of scopes: a user sees APPROVED
  org/public entries + their own (incl. pending); pending entries from others are
  hidden until approved. Cross-tenant private entries are never shown (404).

## Assigning a user to a persona (AWS-side)

> **New users start in NO group.** `AdminCreateUser` (and the `COGNITO_USERS`
> deploy var, which calls it) creates the user but assigns **no group**, so they
> sign in with an empty `cognito:groups` claim → **zero scopes**. The UI fails
> closed on missing scopes (e.g. the registry **Clone** button, gated on
> `registry:read`, is disabled) even though the advisory backend still lets them
> browse. Assign a group below to grant capabilities. Changes take effect on the
> **next token issuance** — the user must sign out/in.

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

## Enforcement is opt-in

RBAC ships **advisory** (`RBAC_ENFORCE=false`): would-be denials are logged +
surfaced as a CloudWatch `WouldDeny` metric, but allowed. Flip to enforce
(`-c rbac_enforce=true` or the Lambda env) once group grants are validated — see
`RBAC_ROLLOUT.md`. In local dev (no Cognito) every scope is granted.

## Changing personas

- New capability for a persona → edit `GROUP_SCOPES` in `rbac.py` **and**
  `frontend/src/auth/scopes.ts`, redeploy.
- New persona → add a group to `GROUP_SCOPES`, seed it in `platform_stack.py`
  (`_rbac_groups`), redeploy, then assign users.
