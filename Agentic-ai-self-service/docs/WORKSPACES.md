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
Web Answer knowledge tools, and the Region the deployment will run in (the explicit `targetRegion`, else
the platform's home Region). The saved flow the request names contributes exactly two things: its budget
declaration, and the `riskClass` / `dataClass` a human gave an entry that is **the same resource** (same
type, reference and configuration, not merely the same id, so a label reviewed for one knowledge base
cannot travel to another). It can never add or remove a capability, so naming a benign flow while
submitting a different body gains nothing. A request that names no flow carries no classifications.

## Risk classes: who is trusted

`riskClass` and `dataClass` on a spec are **assertions by the builder**, and by default they do not count:

- A **connector's** class comes from its catalog (`services/connectors_catalog.py`): any
  `create_*`/`send_*`/`update_*`/`delete_*` operation makes it `write`; the Payments category makes it
  `pay`; a connector with only read operations is `read`. Nobody can lower it below that floor.
- Everything the catalog cannot vouch for (Lambda, MCP and custom tools, unknown connectors, every
  knowledge item) is `unclassified` in a workspace with `trustBuilderClassifications: false` (the
  default), so it floors the agent at `unclassifiedRiskTier` and is refused on a constrained axis
  with `tool_unclassified` / `knowledge_unclassified`.
- A workspace with `trustBuilderClassifications: true` (its builders are its reviewers) uses the
  builder's labels, still floored at the catalog's class for connectors.

## The risk classifier

Pure and deterministic (`services/risk_classifier.py`, version 2). Each rule is a floor; the
budget rule is a modifier applied last. Reasons are returned in this order.

| Rule | Tier |
|---|---|
| a `pay` tool | high |
| a `write` tool | medium |
| a non-Bedrock model provider | medium |
| `confidential` or `pii` knowledge | high |
| public ingress (webhook / email trigger) **and** a write or pay tool | high |
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
flag off later does not release a version admitted with an approval requirement, and a present but
malformed admission or approval record keeps the version blocked (`409`) until an administrator looks.
Versions deployed while the flag was off carry no admission and promote as before.

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
`workspace_not_member`, `workspace_selection_required`. Every refusal is audited like any other write
to `/api/deploy`.

## Where it lives

- Policies: the shared org-config table (`TAG_POLICY_TABLE_NAME`), items `WORKSPACE#<id>#POLICY`
  under `org_id`; strongly consistent reads; floats stored as `Decimal`. No new table or IAM grant.
- Routes: `/api/workspaces/{proxy+}` → the **deployment** Lambda (`routers/workspace_policies.py`);
  the exact `GET /api/workspaces` (workflow sharing) stays on the workflow Lambda.
- Code: `schemas/workspace_policy.py`, `services/risk_classifier.py`, `services/admission_spec.py`,
  `services/workspace_admission.py`, `services/workspace_policy_service.py`,
  `services/workspace_approval.py`, the gate in `deployment_handler._reject_workspace_policy_violation`,
  and the approve route plus promote gate in `routers/versions.py`.

## Not in this release

- Cedar per-workspace policy sets and the Gateway guardrail profile from `defaults.guardrailProfile`.
- The Admin → Workspaces UI (presets *Locked-down*, *Standard*, *Developer*) and the tier badge on
  agent cards; the data for the badge is already served (`admission` on deployment status and version rows).
- Aggregate budget reservation across a workspace's agents (`perWorkspaceMonthlyUsd` is recorded, not
  enforced at admission).
- A trusted source for knowledge data classes; until one exists, a locked workspace treats every
  knowledge item as unclassified.
- Triggers and channels are created after deployment by their own routes, so the public-ingress rule
  fires only for specs that carry them; gating trigger creation on the workspace policy is a follow-up.
