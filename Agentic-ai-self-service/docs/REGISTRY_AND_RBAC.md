# Agent Registry — Roles & Approval

How the org-wide agent registry's two-persona approval workflow works and how it plugs into the platform's Cognito-group RBAC model.

[← Back to README](../README.md)

The registry turns a deployed agent into a reusable, governed blueprint others can discover and clone. Access is **driven entirely by Cognito groups** — no separate auth system. Two group families cooperate:

1. **Scope groups** (`g-admins-*` / `g-users-*`) grant capability **scopes** — the actual enforcement boundary. Registry actions map to `registry:read` (browse, view, **clone**) and `registry:write` (publish, edit, delete, approve, reject).
2. **Registry-persona groups** (`registry-admin` / `registry-developer`) drive the **two-persona approval workflow** (who may approve vs only publish).

> **A user in NO group has NO scopes → effectively read-only.** `registry:read` gates the **Clone to canvas** button, so a freshly-provisioned user (e.g. one created via `COGNITO_USERS`, which assigns no group) can browse but **cannot clone or publish** until a scope group is assigned. This is the most common "why is Clone greyed out?" cause. The registry detail **Access tab** renders exactly which actions the signed-in user can/can't perform, and why.

## Group → scope map

The full platform-wide group→scope table (super-admin, security, cost, standard-user, legacy groups) lives in [`PERSONAS.md`](PERSONAS.md) — source of truth in code: `backend/src/app/services/rbac.py` `GROUP_SCOPES`, mirrored in the UI at `frontend/src/auth/scopes.ts` (keep in sync). For the advisory→enforce rollout procedure, see [`RBAC_ROLLOUT.md`](RBAC_ROLLOUT.md).

The registry-relevant slice:

- `g-admins-registry` (legacy `registry-admin`) → `registry:read` + `registry:write` — publish, clone, edit/delete, approve/reject.
- `g-users-default` → includes `registry:read` — browse + clone approved entries; publish own via the `registry-developer` persona; **cannot** approve.
- *(no group)* → no scopes — browse only (advisory backend); **Clone disabled**.

`t-admin` / `t-user` are a separate **UI dimension** — they decide which admin sections render, not what you're authorized to do (scopes do that).

## Registry personas (the approval workflow, on top of scopes)

| Persona | Cognito group | Can do | Cannot do |
|---------|---------------|--------|-----------|
| **Developer** | `registry-developer` (+ a scope group granting `registry:read`/`registry:write`) | Publish (entry enters `pending`); view **approved** entries + their **own** (any status); clone approved/own; edit/delete their own | Approve or reject; see other users' pending entries |
| **Admin** | `registry-admin` (legacy `org-admin` also honored) | Everything a developer can, **plus**: see the pending-review queue, approve/reject submissions, delete any entry | — |

## Entry lifecycle

```
developer publishes ──▶ pending ──▶ (admin approves) ──▶ approved ──▶ visible + clonable org-wide
                           │
                           └──▶ (admin rejects, optional reason) ──▶ rejected
```

- New publishes start `pending` and are invisible to other developers until approved.
- A non-admin edit (`PUT`) of an approved entry resets it to `pending` (re-review). Admin edits preserve status.
- Backward-compatible: entries created before this feature (no `status` attribute) deserialize as `approved`, so nothing already published disappears.

## Authorization rules (enforced server-side)

- Admin status is read from the caller's `cognito:groups` JWT claim (`auth.is_registry_admin`); the frontend reads the same claim to show/hide the admin "Pending review" UI.
- **RBAC-role denial returns `403`** (e.g. a developer calling `approve`); **cross-tenant / not-visible returns `404`** (never disclosing existence). These are kept strictly distinct.
- Before attaching, the server reads the engine/entry back from the store — a defense-in-depth ground-truth check, not a client-supplied flag.

## Assigning personas

All groups below are created by the CDK stack at deploy time (`platform_stack.py`), so you only *assign* users. Give each user a **scope group** (what they can do) plus the matching **registry-persona group** (approver vs publisher):

```bash
POOL_ID=$(aws cognito-idp list-user-pools --max-results 40 \
  --query "UserPools[?Name=='agentcore-workflow-dev-users'].Id | [0]" --output text)

# An approver: registry admin scopes + the approver persona + admin UI
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \
  --username alice@example.com --group-name g-admins-registry
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \
  --username alice@example.com --group-name registry-admin

# A standard developer: read-only defaults incl. registry:read (browse + clone),
# plus the developer persona so they can publish their own blueprints
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \
  --username bob@example.com --group-name g-users-default
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \
  --username bob@example.com --group-name registry-developer
```

> Group changes take effect on the **next token issuance** — have the user **sign out and back in** (or refresh the session). If Cognito is federated to Okta/Entra, map the IdP group claim to these names and assign in the IdP instead — zero platform change.

In the UI: developers click **Publish to Registry** in the Deploy panel after a deploy, and **Registry** in the component palette to browse and **Clone to Canvas** (Clone requires `registry:read`). Admins additionally see a **Pending review** tab with Approve / Reject actions.

> The registry roles above are one slice of the platform-wide persona/scope model. For how personas are *defined* (`rbac.py` `GROUP_SCOPES`), *created* (CDK `CfnUserPoolGroup`), and *assigned* (AWS Cognito / federated IdP), see [`PERSONAS.md`](PERSONAS.md).
