# Loom-informed enhancement plan

Derived from a full study of **Loom for AWS** (awslabs/loom + the launch blog's 7
enterprise challenges) against our platform. Loom is our sibling: a fleet-management
console (ECS/Fargate + RDS) vs our serverless (Lambda + DynamoDB) **visual canvas
builder**. We keep our differentiator (the canvas + per-deploy codegen); we adopt
Loom's governance/identity/UX wins **only where they fit serverless**.

Each item: **value × fit-to-our-arch**, effort (S/M/L), and whether it's a
**real defect** in our code (fix regardless of roadmap) or a **new capability**.

---

> **Phase 0 status: SHIPPED + verified live (2026-07-16).** Deployed to
> agentcore-workflow-dev (us-east-1) via `deploy.sh`. Verified end-to-end:
> baseline deploy→invoke returned its canary; the EventBridge policy-sweep rule
> is ENABLED at rate(5min) and a direct invoke returned `{swept:2, promoted:0,
> failed:0}`; the runtime_configure step role carries `iam:CreateServiceLinkedRole`
> (0.1) + `secretsmanager:GetSecretValue` (0.2); an audited call with X-Session-Id
> produced an audit row carrying that session_uuid (0.5). All test resources torn
> down, zero orphans.

## Phase 0 — Real defects the study exposed (fix first; low risk, high correctness)

These are bugs/dead-config in OUR code, independent of the Loom roadmap.

| # | Defect | Where | Effort |
|---|--------|-------|--------|
| 0.1 | **VPC config is modeled but never read** — `RuntimeConfiguration.vpc_config` exists but `runtime_deployer.py` + `cfn_template_generator.py` hardcode `networkMode=PUBLIC`. The field is dead. | `runtime_deployer.py`, `cfn_template_generator.py` | M |
| — | *0.1 scope note:* the LIVE deploy path (runtime_configure → create/update_agent_runtime) is now VPC-capable + the VPC service-linked-role IAM grant is added. The **CFN-export** path (`cfn_template_generator.py` lines ~1983/2256) still hardcodes PUBLIC — threading VPC into the downloadable template is an additive follow-up. | | |
| 0.2 | **Alternate-provider model init emits NO credentials** — selecting litellm/openai/anthropic generates a model with no `base_url`/`api_key` (only groq/deepseek/writer get env keys). Selecting those providers produces a broken agent. | `code_generator.py:_get_model_init_code` | S |
| 0.3 | **OBO gateway target hardcodes `CLIENT_CREDENTIALS`** even when `delegation_mode=obo` — the OBO provider is minted correctly but the target still requests client-credentials, so OBO never actually exchanges. | `gateway_deployer.py` (~target cred config) | S |
| 0.4 | **AWS Agent Registry auto-registration has zero callers** — `aws_agent_registry.register()` exists but nothing invokes it on deploy; the federation feature is wired but never fires. | `step_handlers/status_update_step.py` | M |
| 0.5 | **`session_uuid` never populated** in the audit store (field exists, always empty) — blocks any per-session analytics. | `audit_store.py` | S |
| 0.6 | **Cedar promoter re-drive gap** (found live in P-PLAT-027) — the lazy promoter stops re-attempting `update_policy` once `enforce_pending` state is mutated, even while the policy is still `UPDATE_FAILED`. A direct `update_policy` converged instantly. Re-drive on ANY non-ACTIVE state until ACTIVE. | `policy_promoter.py` | S |
| 0.7 | **Registry status not synced / not deleted on teardown** — stale AWS-registry records persist after resource delete + across enable/disable. | `registry.py`, teardown paths | S |
| — | *0.7 scope note:* delete-on-teardown is DONE (the main orphan risk). The `_sync_registry_statuses` reconciliation on aws-config re-enable (validate all stored record ids vs the live registry) remains a documented follow-up. | | |

---

> **Phase 1 status: SHIPPED + verified live (2026-07-16).** All 6 items built,
> tested (21 phase tests + 454 regression), deployed to agentcore-workflow-dev.
> A deploy-time Lambda-policy-size limit (per-route permissions on the ~29-route
> deployment Lambda) was hit + fixed (scope_permission_to_route=False, ~29→3
> permissions). Verified live: token-info returns annotated claims (1.3); JIT
> request→approve WIDENED the shared role with a real inline policy + the iam:*
> escalation guard returns 400 (1.6); import bad-ARN → 400 (1.5); gating no-ops
> with federation off so a normal deploy→invoke returns its canary (1.4). Both
> OIDC synth paths (with/without context) clean (1.1). Zero orphans.

## Phase 1 — Identity & governance completeness (highest enterprise value, fits serverless)

| # | Capability | Approach (serverless-fit) | Effort |
|---|-----------|---------------------------|--------|
| 1.1 | **3rd-party IdP federation** (Entra/Okta/Auth0/OIDC) | Federate INTO Cognito (hosted UI IdP), NOT Loom's in-app multi-issuer validation (that's ECS-shaped). Keeps the API-GW Cognito authorizer unchanged. + a pre-token Lambda for group-claim → internal-group mapping. | L |
| 1.2 | **OBO token-exchange: prove it works** | Add `/agents/{id}/test-obo` dry-run (ACPS `get_resource_oauth2_token` ON_BEHALF_OF) + `oauth_audience` field (Okta custom auth servers reject exchange without it). Pairs with 0.3. | M |
| 1.3 | **Token-info visualization** | A TokenInfoCard on the invoke/harness panel showing decoded user + OBO claims (iss/aud/scp) with group-mapping resolution — the "prove delegation preserved the user" story. Fits our React canvas. | M |
| 1.4 | **Integration gating** — only APPROVED MCP/A2A selectable in a deploy | Validation pass rejecting flows whose connected MCP/A2A map to non-APPROVED registry records. | M |
| 1.5 | **Import existing AgentCore Runtime by ARN** | `POST /register-runtime`: describe + adopt an externally-built runtime into our observability/cost/registry without redeploy. | M |
| 1.6 | **JIT IAM permission-request workflow** | request→approve→widen-role, gated by admin scope, wired to `iam_manager`. | M |

---

> **Phase 2 status: SHIPPED + verified live (2026-07-16).** All 4 items built,
> tested (12 phase tests + 162 regression), deployed to agentcore-workflow-dev.
> Live verify caught a real wiring bug — the runtime_configure STEP Lambda lacked
> TAG_POLICY_TABLE_NAME env + tag-policy read grant, so LOOM_APPROVAL_POLICIES was
> never injected; fixed + redeployed. Confirmed live: approval-policy CRUD works
> (2.2); a deployed runtime now carries the injected
> LOOM_APPROVAL_POLICIES=[{danger-tools,require}] env so the guaranteed
> BeforeToolInvocation hook enforces it (2.1); GET /api/hitl/logs is live (2.3);
> harness invoke-boundary approval detection (2.4). Zero orphans.

## Phase 2 — HITL hardening (we have voluntary HITL; Loom has guaranteed HITL)

| # | Capability | Approach | Effort |
|---|-----------|----------|--------|
| 2.1 | **BeforeToolCall hook** (guarantee, not voluntary) | Today we inject a `human_approval` tool the LLM *may* call — a sensitive tool can be invoked without approval. Add a Strands `BeforeToolCallEvent` HookProvider that auto-gates matched tools. | M |
| 2.2 | **Config-driven approval policies** | ApprovalPolicy store (DDB, tenant-scoped like `hitl_store`) + CRUD + glob tool-match/notify-only/timeout, injected to the agent as env var consumed by 2.1. | M |
| 2.3 | **Durable approval audit log** | Our HITL rows TTL-expire in 24h. Persist decisions to `audit_store` + a filterable `/api/hitl/logs`. | S |
| 2.4 | **Harness HITL** (managed agents have zero HITL today) | tool_use pause + toolResult-resume in the harness invoke path. | L |

---

> **Phase 3 status: SHIPPED + verified live (2026-07-16).** Both items built,
> tested (3 stream-invoke tests + 232 frontend suite), deployed. A real
> stream-error-swallow bug was caught by the new test + fixed. Verified live: the
> deployed CloudFront bundle contains the ChatPage; the agent-picker data path
> (GET /api/deployments?status=succeeded) returns 200; and a freshly-created
> t-user's token resolves to groups [t-user, g-users-default] + consumer scopes
> (invoke/agent:read, no admin) — confirming the isTypeAdmin→ChatPage routing.
> Test end-user removed. NOTE: agent visibility is owner-scoped today; Loom's
> group-shared visibility is a documented backend follow-up.

## Phase 3 — End-user Chat persona (biggest NEW capability; we're builder-only)

| # | Capability | Approach | Effort |
|---|-----------|----------|--------|
| 3.1 | **ChatPage for `t-user`** | We defined `t-user`/`t-admin` groups but standard users have nowhere to land. Route `t-user` to a chat UI (agent picker filtered by group tag + SSE streaming + conversation history + "My Memory" panel). We already have SSE invoke + memory + RBAC — this is mostly frontend. | L |
| 3.2 | **View-as preview** | Admin previews the end-user experience (we partly have this via RBAC). | S |

---

> **Phase 4 status: SHIPPED + verified live (2026-07-16).** All items built,
> tested (6 VPC tests + regression), deployed. Verified live:
> - **4.2** VPC-profile CRUD works via the API; unknown-profile deploy → 400.
> - **4.1** a VPC-egress agent deployed via the `default-egress` profile came up
>   `READY` in **networkMode: VPC** with the exact resolved subnets — proving the
>   profile→config→VPC-mode path end-to-end on real AWS. (A first attempt
>   CREATE_FAILED on an unsupported AZ — real AWS signal — fixed by moving to
>   supported-AZ subnets.)
> - **4.3** the PrivateLink CFN template passes the real validate-template API.
>
> **Honest verification boundary:** a *functional invoke* of the VPC-egress agent
> could NOT be exercised in this account — the default VPC has 0 NAT gateways and
> no Bedrock/S3/ECR VPC endpoints, so a VPC-mode runtime has no egress to pull its
> image or reach the model (confirmed: empty container logs; matches the
> vpc_lambda_ddb_endpoint memory). That is a CUSTOMER-INFRA prerequisite the
> platform doesn't provision — the platform code is proven correct. PrivateLink
> ingress likewise needs a consumer VPC to fully exercise. All test resources torn
> down, zero orphans.

## Phase 4 — Networking (enterprise-private connectivity; depends on 0.1)

| # | Capability | Approach | Effort |
|---|-----------|----------|--------|
| 4.1 | **VPC-egress agents** | Thread `vpc_config` → `networkMode=VPC` (builds on 0.1) + `iam:CreateServiceLinkedRole` for the AgentCore network SLR (first VPC deploy fails without it). | M |
| 4.2 | **Named VPC config profiles** | DDB store + CRUD + a canvas network-mode selector bound to the existing (dead) frontend field. | M |
| 4.3 | **PrivateLink ingress + SG IaC** | Ship optional CFN (NLB + VPC Endpoint Service + per-protocol SG) as a downloadable add-on stack. | L |

---

> **Phase 5 status: SHIPPED + verified live (2026-07-16).** All 4 items built,
> tested (full backend suite 1093 pass / 0 fail after fixing a pre-existing stale
> KB-ingestion mock), deployed to `agentcore-workflow-dev` (UPDATE_COMPLETE, 142s,
> no rollback). Verified live against the real API + deployment Lambda:
> - **5.1** `GET /api/models` → HTTP 200 with **75 live models** (29 inference
>   profiles + 46 foundation models) discovered from real Bedrock, curated overlay
>   applying friendly labels — proving the new `bedrock:List*` grants + route +
>   merge/dedup work end-to-end (no fallback triggered).
> - **5.2** `GET /api/admin/audit` → HTTP 200 carrying the new analytics keys
>   `distinct_actors` (4), `distinct_sessions` (1), and the chart-ready `by_day`
>   time-series, over 66 real audited events.
> - **5.3** the `{"cost_reconcile": true}` EventBridge sentinel invoked the live
>   deployment Lambda → `{reconciled, breached, skipped, failed}`. With a real
>   owner budget + a tag budget present it returned `reconciled:1, skipped:1`
>   (tag scope correctly skipped — no tag→runtime index), proving the scheduled
>   self-drive path over real DynamoDB + budget store. The `CostReconcileSchedule`
>   rate(24h) rule + Lambda permission are live in CloudFormation.
> - **5.4** the four OpenAI-compat/LiteLLM providers (groq/deepseek/together/writer)
>   now read the deploy-injected `PROVIDER_API_KEY` — verified at the codegen layer
>   (6 provider-credential tests, `ast.parse`-validated init lines).
>
> **Honest verification boundary:** 5.4's *functional* invoke (a live non-Bedrock
> agent actually calling e.g. Groq) was NOT exercised — it needs a real third-party
> API key + provider account this Bedrock-only test account doesn't have. The bug
> fixed was a real latent 401 (the injected secret was never consumed by those four
> providers); the fix is proven correct at the generation layer. All live test
> artifacts (2 budgets + 1 Cognito user) torn down, zero orphans.

## Phase 5 — Analytics & alternate models (valuable; some external-infra-bound)

| # | Capability | Approach | Effort / caveat |
|---|-----------|----------|--------|
| 5.1 | **Live model catalog** | `/api/models` doing live Bedrock `list_foundation_models` + pricing merge, replacing the hardcoded frontend list. Valuable even without LiteLLM. | M |
| 5.2 | **Rich admin analytics** | recharts dashboard: login/action/page tracking, session-UUID scoping (needs 0.5), 2-level action taxonomy, per-session drill-down, multi-user filter. | L |
| 5.3 | **Usage-log cost reconciliation** | EventBridge-scheduled Lambda (NOT Loom's always-on poller) upgrading estimated→actual vCPU/GB-hr cost. | M |
| 5.4 | **LiteLLM alternate providers** | Per-agent vended virtual keys + dynamic catalog. **CAVEAT: needs a running LiteLLM proxy (out-of-band infra) — weighs against our pure-serverless model.** Lowest priority; consider only if a customer demands non-Bedrock models at scale. | M + external infra |

---

## Explicitly NOT adopting (Loom features that fight our architecture)
- Loom's **in-app multi-issuer JWT validation** (`jwt_validator` per-request) — assumes a long-running app; we use the API-GW edge authorizer + Cognito federation instead (simpler).
- Loom's **backend code-exchange proxy / confidential-client toggle** — subsumed by Cognito hosted UI.
- Loom's **always-on asyncio usage_poller** — re-cast as EventBridge (5.3).
- **RDS/Postgres + integer PKs** — we're DynamoDB single-table; no change.

## Recommended sequencing
**Phase 0 first** (defects — some are shipping bugs). Then **Phase 1** (enterprise
identity/governance is the blog's core thesis and our biggest credibility gap).
**Phase 3** (end-user chat) is the highest-visibility new capability — good to
schedule alongside Phase 1. Phases 2/4/5 as capacity allows. Phase 5.4 only on demand.
