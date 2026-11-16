# Workspaces, policy and risk-tiered admission

A **workspace policy** lets an administrator hand business users a safe sandbox: which model
providers, models, connectors, tool risk classes, data classes, channels and Regions a workspace may
use, how much an agent may cost, and which approver groups each risk tier must clear before an agent
reaches production. Every `POST /api/deploy` is admitted against the policy that governs it, before
any row, secret, execution or AWS resource exists.

Everything in this document is behind the `workspacePolicy` feature flag
(`/agentcore-workflow/{env}/platform-features`, see [API_REFERENCE.md](API_REFERENCE.md#platform-configuration-feature-flags)).
With the flag off, admission is exactly what it was before this feature and the admin routes answer
the same `404` as a nonexistent route.

## The policy document

`PUT /api/workspaces/{workspaceId}/policy` (scope `admin`) stores one policy per workspace. The
schema is strict: an unknown key is `422`, and so is `unclassified` inside an allow-list.

A policy is **never replaced blind**. Every stored policy has a version, served as the `ETag` of a
`GET` or `PUT`: an opaque quoted token (`<incarnation>.<revision>`, where the incarnation is a random id
minted when the policy is created and the revision counts the writes within it). A `PUT` says what it is
based on: `If-Match: "<token>"` to replace the policy exactly as it was read, or `If-None-Match: *` to
create one that does not exist yet. The store enforces the precondition on both attributes in the same
DynamoDB write, so two admins editing the same policy cannot overwrite each other: the second save is
`412 policy_revision_conflict` (the body names `expectedVersion` and `currentVersion`) and nothing
changes, which is what keeps a form opened before a colleague added `g-security` to the high routing from
silently removing it again. Because a delete followed by a create mints a new incarnation, a form opened
before the deletion cannot overwrite the recreated policy either (a plain counter would restart at 1 and
let it; the `412` then says the policy was deleted and recreated). **Deletion is conditional the same
way**: `DELETE` must carry `If-Match` with the version the admin confirmed, because a workspace without
its own policy falls back to the org `default`, so a delete confirmation opened before a colleague
tightened or recreated the policy must not remove it (`412`, nothing deleted). A `PUT` with neither
header, or a `DELETE` without `If-Match`, is `428 precondition_required`; `If-Match: *`, a weak validator,
a token this store never issued or both headers at once is `400`. Policies written before versions
existed read as the legacy token `"0"` and can be claimed or deleted exactly once as such.

```json
{
  "workspaceId": "finance",
  "name": "Finance (read-only business workspace)",
  "memberGroups": ["g-finance"],
  "adminGroups": ["g-admins-super"],
  "allowed": {
    "providers": ["bedrock"],
    "models": [],
    "connectors": ["slack", "jira"],
    "toolRiskClasses": ["read"],
    "dataClasses": ["public", "internal"],
    "channels": ["slack", "teams"],
    "regions": ["us-east-1"]
  },
  "budgets": { "perAgentMonthlyUsd": 50, "maxAgentMonthlyUsd": 100, "perWorkspaceMonthlyUsd": 2000 },
  "approval": { "low": [], "medium": ["g-approvers"], "high": ["g-admins-security", "g-approvers"] },
  "defaults": { "platformVersion": "V2", "guardrailProfile": "standard", "memory": "shortTerm", "unclassifiedRiskTier": "medium", "trustBuilderClassifications": false }
}
```

| Field | Meaning |
|---|---|
| `allowed.*` | An **empty** list is unconstrained on that axis. A non-empty list is exhaustive. `toolRiskClasses` and `dataClasses` can never contain `unclassified`: on a constrained axis, an item nobody classified is refused with `tool_unclassified` / `knowledge_unclassified` so the builder knows to classify it, never silently permitted. |
| `memberGroups` | Cognito groups whose members belong to the workspace. The workspace that governs a deployment is resolved from the **deployer's memberships** (see below), never from the agent's document alone. The `default` workspace has no membership: it governs everyone who belongs to no configured workspace. |
| `budgets.perAgentMonthlyUsd` | The budget an agent gets when its spec declares none. |
| `budgets.maxAgentMonthlyUsd` | The most any **one** agent may declare; admission enforces it. An omitted declaration falls to `perAgentMonthlyUsd`; if neither exists under a cap, the agent is refused (`budget_undeclared`). |
| `budgets.perWorkspaceMonthlyUsd` | The workspace's monthly budget as a whole. Recorded for run-time metering and chargeback (the cost components); admission does **not** enforce an aggregate. |
| `approval.<tier>` | Approver groups an agent of that tier must clear before production. Empty = auto-approve. Group names use the Cognito group grammar; an empty name is refused at `PUT`. |
| `defaults.unclassifiedRiskTier` | The tier an unclassified tool or knowledge item floors the agent to. `medium` by default: a class nobody gave cannot be shown to be harmless. Set `low` only for a prototyping workspace. |
| `defaults.trustBuilderClassifications` | Whether a builder's own `riskClass` / `dataClass` labels count. **Off by default**: a label the platform cannot verify reads as `unclassified`. Turn it on only for a workspace whose builders are its reviewers. |

## Which policy governs an agent

The workspace is resolved from the **deployer's memberships** (`memberGroups` ∩ the JWT's
`cognito:groups`), so the deployer's own document cannot choose a more permissive policy:

1. The deployer belongs to **no** configured workspace → the org **`default`** policy, whatever the
   agent's `workspaceId` says (an unconfigured name is a canvas saved before its workspace existed).
2. The deployer belongs to **exactly one** → that workspace governs, whether or not the agent names
   it. Dropping `flowId` or clearing `workspaceId` is not an escape.
3. The deployer belongs to **several** → the agent must name one of them (`workspaceId`, set when the
   canvas was saved); otherwise `403 policy_denied` / `workspace_selection_required`.
4. The agent names a configured workspace the deployer is **not** a member of → `403 policy_denied` /
   `workspace_not_member`. Never a silent re-route.
5. No `default` policy is stored when it is what governs, or the store cannot be read → `503`, nothing
   created. The default workspace is never auto-created: an administrator writes it.

## What is judged

**The request body.** `POST /api/deploy` deploys its body, so admission judges the body: the model and
provider, every connector (`connectors[]` and `gatewayTools` entries of the form `connector:<id>`),
Gateway Lambda tools, custom tools (which are also *custom code*), external MCP servers, the MCP server
runtime, the Code Interpreter and Browser when connected, the knowledge base, the managed Web Search /
Web Answer knowledge tools, the channels the deployment exposes (its runtime protocol as `api` / `mcp` /
`a2a`, plus an A2A configuration), and the Region the deployment will run in (the explicit
`targetRegion`, else the platform's home Region). The saved flow the request names contributes exactly
two things: its budget declaration, and the `riskClass` / `dataClass` a human gave an entry that is
**the same resource** (same type, reference and configuration: a custom tool's code hash, an external
MCP server's endpoint, the standalone MCP server runtime's whole configuration, a connector's auth and
scopes, a knowledge base's configuration; never the id alone, so a label reviewed for one resource
cannot travel to a changed one; when two labels name one resource the stricter governs). It can never add or remove a
capability, so naming a benign flow while submitting a different body gains nothing. A request that
names no flow carries no classifications.

## Risk classes: who is trusted

`riskClass` and `dataClass` on a spec are **assertions by the builder**, and by default they do not count:

- A **connector's** class comes from its catalog (`services/connectors_catalog.py`): any
  `create_*`/`send_*`/`update_*`/`delete_*` operation makes it `write`; the Payments category makes it
  `pay`; a connector with only read operations is `read`. Nobody can lower it below that floor.
- Outside a workspace with `trustBuilderClassifications: true`, a builder's label can only make the
  agent **stricter**, never admissible. For **tiering**, `write` / `pay` on a tool and `confidential` /
  `pii` on knowledge add their floor (a builder declaring more risk is never argued with), and a
  resource nothing verifies keeps the `unclassifiedRiskTier` floor as well: the floors combine as a
  maximum, so labelling an unknown tool `write` cannot pull it down from an admin's "unknown tools are
  high". For **allow-lists**, only a class the catalog verifies counts; every other label, in either
  direction, is `unclassified`, so a `[write]` list admits the catalog-verified Jira connector and
  refuses a Lambda the builder merely labelled `write` (`tool_unclassified`), and a `[pii]` list is not
  satisfied by a `pii` label (`knowledge_unclassified`).
- A workspace with `trustBuilderClassifications: true` (its builders are its reviewers) uses the
  builder's labels as written for both tiering and allow-lists, still floored at the catalog's class
  for connectors.

## The risk classifier

Pure and deterministic (`services/risk_classifier.py`, version 4). Each rule is a floor; the
budget rule is a modifier applied last. Reasons are returned in this order.

| Rule | Tier |
|---|---|
| a `pay` tool | high |
| a `write` tool | medium |
| a non-Bedrock model provider | medium |
| `confidential` or `pii` knowledge | high |
| public ingress (webhook / email trigger) **and** an ingress-sensitive capability: a tool that is not verified read-only (write, pay **or unclassified**), or executable custom code | high |
| custom code | medium |
| an unclassified tool or knowledge item | `defaults.unclassifiedRiskTier` (medium unless lowered) |
| per-agent budget above the workspace default | one tier higher |

The gallery templates carry Lambda tools nobody has classified, so under a default policy they are
**medium**; a `Standard` workspace that auto-approves medium (`approval.medium: []`) deploys them
as before, and a `Developer` workspace sets `unclassifiedRiskTier: low` and
`trustBuilderClassifications: true`.

## What happens to each tier

| Tier | Approver groups | Route |
|---|---|---|
| any | none (`[]`) | Deploys where the caller asked, as before. |
| medium / high | one or more | The deployment may only land in **`staging`**; an explicit `deploymentSlot: "production"` is `409` and nothing is created. The version row carries the admission (tier, reasons, approver groups). `POST .../versions/{id}/promote` to production is `409` naming the pending groups until **every** named group has approved through `POST .../versions/{id}/approve`. |

The deploy response says so: `"Deployment started in the staging slot. Workspace admission
classified this agent medium risk: approval from g-approvers is required before it can be promoted
to production."`

### Approving

`POST /api/runtimes/{name}/versions/{versionId}/approve` with `{"group": "g-approvers"}`.

- Requires the existing HITL approval scope **`hitl:write`** (today `g-admins-super` / `org-admin`;
  grant it to the groups you name as approvers, or make approvers members of a group that has it).
- The caller must be a **member of `group`**, and `group` must be one the version's admission routed to.
- The version's **owner can never approve** their own version, whatever groups they hold (`403`).
- A caller in none of the version's approver groups gets the same `404` as a missing version, so
  `hitl:write` alone does not reveal other tenants' versions or their routing.
- One group per call; repeating a group is idempotent. The record stores the group, the approver's
  subject and the timestamp, written with a compare-and-set fenced on the version's owner, its deploy,
  the exact admission document the approver was shown and the approvals list as read, so two racing
  approvers cannot duplicate or drop a record and an approval can never attach to a different decision.
- Promotion to production is itself fenced: the slot transaction is conditioned on exactly the
  admission and approvals the route validated, so a requirement tightened or an approval replaced
  between the read and the write is a `409`, never a promotion.

The requirement is read from the **version row**, not from the flag at promote time: turning the
flag off later does not release a version admitted with an approval requirement. A present but
malformed record keeps the version blocked (`409`) until an administrator looks: an admission that is
not a complete `admitted` decision carrying admission/classifier versions this build knows, an
approval record missing its group, approver or timestamp, or one naming the version's owner as
approver (the store never writes such a record, so its presence is tampering). Versions deployed while the flag was off carry no admission and promote as before. The
versions table is the trust root here, as it already is for ownership and status: a principal who can
edit it directly is outside this control's threat model.

Stored policies are read strongly consistently and must be **complete** documents (every field the
admin API writes, at every level); a truncated item is refused as corruption rather than read as an
unconstrained workspace.

## Triggers attached after deployment

A trigger is ingress attached to a **production** runtime after it was admitted
(`POST /api/runtimes/{name}/triggers`), so the admission decision never saw it. With the flag on, the
route **re-admits the agent with the trigger added**, under the workspace's **current** policy
(`services/trigger_admission.py`), after ownership (a non-owner stays `404`), request validation
(`400`) and eligibility (`409`), and before its first side effect (the webhook's secret), so a refusal
creates nothing.

The admission decision (version 3) records an **inventory** of what it judged, as the builder declared
it (model, tools with their labels, knowledge, channels, triggers, custom code, Region, budget):
exactly the fields the classifier and the allow-lists read, kept as labels rather than effective
classes so the policy of the day decides what they mean. The attachment rebuilds that spec plus the
trigger and runs the real admission against today's policy:

| Trigger | Version | Result |
|---|---|---|
| `cron`, `eventbridge`, `s3` | any | Allowed without reading anything: the platform's own plumbing never changes the tier. With the flag off, every trigger takes this path. |
| `webhook` | deployed without admission (flag off at the time) | `403 policy_denied` / `version_not_admitted`: its tools were never classified. Redeploy under the policy first. |
| `webhook` | admission older than version 3 | `403 policy_denied` / `admission_predates_ingress_rule`: nothing recorded to re-admit from. Redeploy under the policy first. |
| `webhook` | admitted (version 3) | The **current** policy is read (missing or unreadable: `503`, never permission). The agent plus the webhook is admitted against it in full: a connector, model, data class, channel or Region the policy no longer allows refuses the attachment with the allow-list's own code; the tier is recomputed (public ingress with any **ingress-sensitive capability** is `high`); and every approver group the current policy names for that tier must have approved this version. Pending groups: `403 policy_denied` / `trigger_raises_risk_tier` when the trigger raises the tier, `trigger_requires_approval` when the tier is unchanged but today's routing needs a group the version lacks. A tightened policy therefore applies to an agent admitted `high` under a looser one, and revoking `trustBuilderClassifications` revokes the exemption a `read` label once bought. |

A capability is **ingress-sensitive** when nothing verifies it is harmless behind public ingress: a
tool whose effective class is `write`, `pay` or **unclassified** (a Lambda nobody classified may mutate
anything; a builder's `read` label counts only while the workspace trusts builder classifications), or
any executable **custom code** node (moving the mutation into code is not a way around the gate). The
decision records the list (`ingressSensitiveCapabilities`) for the badge, from the same classifier
helper the ingress rule uses; tests pin the recorded inventory to re-admit identically to the original
spec on every gallery template, and the list to the rule.

The judgement is a snapshot, so the row write is **bound to it**: the trigger's `TransactWriteItems`
also condition-checks the exact policy item the judgement read. A policy changed or deleted between
the judgement and the write cancels the transaction (`409`, nothing created). A present but malformed
admission record is `409`, never read as "no requirement"; a version-3 record missing its inventory or
capabilities is malformed, and so is an inventory missing **any** of its fields (`tools`, `knowledge`,
`customCode`, `model`, `region`, ... are required to be present, as explicit empty lists or nulls where
the agent has none): a record that lost its `tools` list would otherwise re-admit as a toolless, low-risk
agent and get its webhook without the security approval its real tier needs. Note the consequence until
specs can declare triggers (Phase 3): approvers
can only approve the groups a version's admission routed to, so an agent whose current routing names a
group its admission did not cannot get a webhook at all; the path is a workspace whose policy fits the
agent's real ingress, then a redeploy. Slack and Teams channel routes do not exist yet; when they do,
they go through this gate.

```json
{
  "detail": {
    "code": "policy_denied",
    "workspaceId": "finance",
    "tier": "medium",
    "violations": [
      { "code": "trigger_raises_risk_tier", "path": "/triggers/0/type", "message": "a webhook trigger is public ingress; combined with this agent's write tools it makes the agent high risk, but the version was admitted medium and approval from g-security is missing; in this workspace a high-risk agent needs their approval before it can be reached from the internet" }
    ],
    "message": "This trigger cannot be attached: a webhook trigger is public ingress; ... Nothing was created."
  }
}
```

## Admin → Workspaces (the UI)

Admins manage policies from the **Agent Registry** modal, in the admin panels under the search box,
while the flag is on (off, the panel is not rendered: its routes would answer `404`). The panel lists
every policy with who it governs, what it allows and who approves; creates or edits one from a
**preset**; deletes one after an explicit confirmation; and warns when no `default` policy exists
(with the flag on, every deploy is `503` until one does).

| Preset | Sets |
|---|---|
| **Locked-down** | Bedrock only; `toolRiskClasses: [read]` (verified read connectors only; an unclassified Lambda or MCP tool is refused, not floored); `dataClasses: [public, internal]`; `channels: [web, api]`; budgets 50 / 100 / 1000 USD; **strict** Gateway guardrails; unclassified items floored to **high**; medium → the approver groups, high → security + approvers. |
| **Standard** | Bedrock only; tools and data are not filtered by class (the tier governs: an unclassified or write tool is medium and goes to the approvers, payments or sensitive data is high and goes to security too); budgets 100 / 500 / 5000 USD; standard guardrails; unclassified floored to medium. **Every gallery template deploys under it.** |
| **Developer** | Nothing constrained, every tier auto-approved, builders' own classifications trusted, unclassified floored to low. |

The approver and security groups are inputs the admin types (a group nobody holds would gate a tier
forever). They and the medium/high routing fields are one state seen two ways: typing reviewers fills
the routing, editing the routing refills the reviewers, before or after a preset is applied, so a group
typed anywhere is never dropped on save. The panel **refuses to save** a policy whose review is
reduced (a tier auto-approves, or high risk adds no reviewer beyond the medium approvers, which is what
a reviewed preset with the security group left blank would be) unless the admin ticks *Accept reduced
review* (the Developer preset ticks it, because that is what it means), and "Create the default
policy" starts from Standard rather than from an unconstrained document, so the org-wide default
cannot become "everything auto-approves" by accident. The three preset documents are a contract: the frontend snapshots them to
`backend/tests/fixtures/workspace_policy/presets/` and `backend/tests/test_workspace_presets.py`
validates each against the schema and pins what it admits.

**Edit** fetches the policy as the server holds it now, with its version token, and the save names that
version (`If-Match`); a new policy is created only if none exists (`If-None-Match: *`). When a colleague
saved first, or deleted and recreated the policy, the backend's `412` is shown verbatim, the admin's form
is kept, and **Reload current version** fetches the colleague's version so the change can be reapplied on
top of it instead of over it. **Delete** reads the policy first and shows exactly what is about to be
deleted (name, version, approval routing) before asking for confirmation; the delete names that version,
so a policy changed or recreated meanwhile stays and the refusal is shown. A response without a version
is refused rather than edited or deleted.

Every version in the deploy panel's **Versions** list carries a **tier badge** (`low` / `medium` /
`high risk`, the classifier's reasons in its tooltip, and the groups whose approval is still pending);
versions deployed while the flag was off show none.

## The refusal

```json
{
  "detail": {
    "code": "policy_denied",
    "workspaceId": "finance",
    "tier": "medium",
    "violations": [
      { "code": "connector_not_allowed", "path": "/tools/0/ref", "message": "connector 'github' (tool 'connector-github') is not allowed in this workspace" },
      { "code": "tool_risk_class_not_allowed", "path": "/tools/0/riskClass", "message": "tool 'connector-github' is a 'write' tool; this workspace allows ['read']" }
    ],
    "message": "This agent uses 2 thing(s) workspace 'finance' does not allow. Nothing was created."
  }
}
```

Reason codes: `provider_not_allowed`, `model_not_allowed`, `connector_not_allowed`,
`tool_risk_class_not_allowed`, `tool_unclassified`, `data_class_not_allowed`, `knowledge_unclassified`,
`channel_not_allowed`, `region_not_allowed`, `budget_undeclared`, `budget_exceeds_workspace`,
`workspace_not_member`, `workspace_selection_required`; for a trigger attachment, the same allow-list
codes plus `version_not_admitted`, `admission_predates_ingress_rule`, `trigger_raises_risk_tier` and
`trigger_requires_approval`. A refused policy write is `precondition_required` (`428`) or
`policy_revision_conflict` (`412`). Every refusal is audited like any other write
to `/api/deploy`, `/api/runtimes/{name}/triggers` or `/api/workspaces/{id}/policy`.

## Where it lives

- Policies: the shared org-config table (`TAG_POLICY_TABLE_NAME`), items `WORKSPACE#<id>#POLICY`
  under `org_id` with an `incarnation` (minted at create) and a `revision` counter; strongly consistent
  reads; every write conditional on both; floats stored as `Decimal`. No new table or IAM grant.
- Routes: `/api/workspaces/{proxy+}` → the **deployment** Lambda (`routers/workspace_policies.py`);
  the exact `GET /api/workspaces` (workflow sharing) stays on the workflow Lambda.
- Code: `schemas/workspace_policy.py`, `services/risk_classifier.py`, `services/admission_spec.py`,
  `services/workspace_admission.py`, `services/workspace_policy_service.py`,
  `services/workspace_approval.py`, the gate in `deployment_handler._reject_workspace_policy_violation`,
  the approve route plus promote gate in `routers/versions.py`, and the trigger attachment gate
  `services/trigger_admission.py` + `routers/triggers._reject_trigger_policy_violation`.
- UI: `frontend/src/components/modals/WorkspacesPanel.tsx` (with `workspacePresets.ts` and
  `workspacePolicyForm.ts`), `frontend/src/services/api/workspacePolicies.ts`, and the badge
  `frontend/src/components/deploy/AdmissionBadge.tsx` on `VersionsList`.

## Not in this release

- Cedar per-workspace policy sets and the Gateway guardrail profile from `defaults.guardrailProfile`.
- A tier badge on **registry** cards: a registry entry does not carry the admission of the version it
  was published from, so the badge lives on the Versions list.
- Aggregate budget reservation across a workspace's agents (`perWorkspaceMonthlyUsd` is recorded, not
  enforced at admission).
- A trusted source for knowledge data classes; until one exists, a locked workspace treats every
  knowledge item as unclassified.
- Spec-declared triggers and Slack/Teams channel routes (Phase 3). Until then the public-ingress rule
  fires at deploy time only for specs that carry triggers, the channel allow-list sees only the
  protocol and A2A channels at deploy time, and a trigger attached afterwards is judged by the
  attachment gate above.
