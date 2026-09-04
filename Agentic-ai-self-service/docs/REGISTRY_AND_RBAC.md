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

## Bring your own registry — LiteLLM as the catalog

The internal DynamoDB catalog described above is the default and is unchanged. A
LiteLLM proxy can be made the **authoritative** catalog instead, for organisations
that already govern their MCP servers there and do not want a second source of
truth.

Configure it in **Registry → LiteLLM** (registry-admin only): base URL + virtual
key. The key is stored in Secrets Manager under `agentcore-registry/` and is never
returned by the API or written to logs. Connecting and activating are separate
steps, so you can test reachability before handing over the catalog.

When active, LiteLLM's enabled MCP servers become the catalog, and **presence and
enablement in LiteLLM is the approval signal** for the pre-deploy governance gate.
Removing or disabling a server in LiteLLM immediately blocks new deploys that
reference it.

Be aware that **not every LiteLLM release reports an enablement flag.** On the
release verified in
[MCP Gateway Integration](MCP_GATEWAY_INTEGRATION.md#the-two-wire-shapes-those-probes-parse),
server records carry no `enabled`/`disabled` field at all, so presence in the list
is what actually gates a deploy. Where a flag *is* reported it is honored. If you
need a server to stop being deployable on such a release, **remove** it from
LiteLLM rather than relying on a disable toggle.

### What it does and does not replace

The read-only limit is **per entry, not per operation**. LiteLLM has no write API
for MCP server records — registration happens through its Admin UI or
`config.yaml` — so a row *projected from LiteLLM* cannot be written back. Agents
published from a canvas still live in the platform sidecar and keep the full normal
workflow, so the catalog is a merge of the two:

| Entry origin | Under the LiteLLM provider |
|---|---|
| Projected from LiteLLM (`source: litellm`) | Read-only. list / search / get work; publish, update, delete, approve, reject and clone return **501** naming LiteLLM and telling you to change it there, rather than silently accepting a write that would then diverge |
| Published from a canvas (platform sidecar) | Fully mutable — normal publish / update / delete / approve / reject / clone, because LiteLLM has no canvas-snapshot or review-state counterpart to replace them |
| Pre-deploy approval gate | Served by LiteLLM (present **and** enabled = approved) |

`GET /api/registry/litellm-config` returns this as a machine-readable
`capabilities` object, including `read_only_sources`, so the UI can disable the
right buttons instead of discovering the 501 by trying.

On a slug collision the **sidecar row wins**. That can only happen for an entry
published before this provider was switched on, and preferring the sidecar keeps
it reachable and mutable rather than making it vanish behind a read-only
projection its owner cannot touch.

Two consequences worth planning for:

- **Fail-closed, not fail-open.** If the catalog cannot be read, deploys that
  reference an integration return **503** and are refused — an unknown approval
  status is never treated as approved. An empty-but-readable catalog blocks too:
  "nothing approved yet" is not a free pass.
- **A private LiteLLM saves as `unverified`.** The control plane has no VPC
  egress, so it cannot reach a VPC-private proxy to probe it. That is a normal
  state, not an error, and the UI labels it. The deployed agent reaches the proxy
  from its own VPC-mode runtime.

Switching back is one click — **Disconnect & use platform catalog**. The DynamoDB
entries were never touched, so approve/clone start working again immediately. Note
this *disconnects* rather than just toggling: the stored connection is cleared
along with the setting, so the base URL has to be re-entered to switch back to
LiteLLM later. The virtual key's secret is deliberately *not* deleted — it may be
shared with a LiteLLM gateway node — and the UI names the ARN so you can remove it
yourself once you're sure.
