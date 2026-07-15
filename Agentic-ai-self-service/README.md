# AgentCore Visual Workflow Platform

A visual workflow builder for **AWS Bedrock AgentCore** that lets you design, configure, and deploy AI agents through a drag-and-drop canvas interface. Inspired by n8n's node-based editor, built for the AgentCore ecosystem. Deployed to AWS with API Gateway, Lambda, Step Functions, DynamoDB, and CloudFront.

**Two authoring paths, one deploy pipeline.** Build agents either by wiring components on the **Visual Canvas** (code-generated AgentCore **Runtime**) or as a config-driven **AgentCore Harness** (AWS's managed, Strands-powered harness — declare model + instructions + tools + memory, no code artifact). Pick the mode at deploy time (`deploymentMode: "runtime" | "harness"`); both share the same Gateway, Memory, connectors, test, and teardown surfaces.

**Real SaaS connectors.** Connect agents to live third-party APIs (Jira, Asana, Slack, GitHub, Salesforce, or any OpenAPI spec) as Gateway targets with API-key or OAuth2 (client-credentials) outbound auth — credentials live only in Secrets Manager, never in the canvas or logs.

## Architecture

![Architecture](docs/architecture.jpg)

> The editable diagram source is at [`docs/architecture.drawio`](docs/architecture.drawio). Open it in [draw.io](https://app.diagrams.net/) to view or edit. To regenerate the PNG, export from draw.io with **File > Export as > PNG**.

| Component | AWS Service | Purpose |
|-----------|-------------|---------|
| Frontend hosting | S3 + CloudFront | Static SPA with HTTPS and SPA routing |
| API routing | API Gateway HTTP API | HTTPS by default, pay-per-request, CORS |
| Workflow CRUD | Lambda (FastAPI + Mangum) | Workflow create, read, update, delete, validate, import/export |
| Deployment orchestration | Lambda + Step Functions | 13-step agent deployment with retries and timeouts |
| Agent framework | Strands Agents SDK | Provider-aware model init, multi-agent patterns (Graph, Swarm, Workflow) |
| Managed harness | Bedrock AgentCore Harness | Config-driven authoring path (`create_harness` / `invoke_harness`) — model + instructions + tools + memory, no code artifact |
| Streaming test endpoint | Lambda Function URL (RESPONSE_STREAM, AWS_IAM) | Streams long (>30s) agent test invocations past the API Gateway 30s cap |
| Model providers | 13 Strands providers | Bedrock, OpenAI, Anthropic, Gemini, Mistral, Ollama, Groq, DeepSeek, Together, LiteLLM, SageMaker, Writer, LlamaAPI |
| Workflow storage | DynamoDB | Persistent storage with on-demand billing |
| Deployment state | DynamoDB | Deployment progress tracking with 30-day TTL |
| AgentCore services | Bedrock AgentCore | Runtime, Gateway, Memory, Knowledge Base, Evaluation, Policy, Observability |
| Configuration | SSM Parameter Store | Runtime config under `/agentcore-workflow/{env}/` |
| Logging | CloudWatch Logs | Lambda and Step Functions execution logs |
| Infrastructure | AWS CDK (Python) | Single stack, all resources defined as code |


## Key Features

- **Visual Canvas** -- Drag-and-drop AgentCore components (Runtime, Gateway, Memory, Knowledge Base, Browser, Identity, Observability, Policy, Connectors) and wire them together.
- **AgentCore Harness (parallel authoring path)** -- Deploy a config-driven managed harness instead of a code-generated runtime: declare model, instructions, connected gateway/connectors, and memory — AWS runs the orchestration loop (powered by Strands). Selected via `deploymentMode: "harness"`; shares the same gateway/memory/connector steps and the same test/delete/teardown path as the runtime mode. Memory is auto-provisioned for session continuity.
- **Real SaaS Connectors** -- A curated catalog (Jira, Asana, Slack, GitHub, Salesforce) plus a generic OpenAPI/MCP connector lets agents call live third-party APIs as Gateway targets. Outbound auth is API-key or OAuth2 client-credentials; secrets are minted into Secrets Manager (`agentcore-connector/{owner}/*`) and the raw value never touches the canvas, DynamoDB, or logs. Branded connectors map to first-class AgentCore OAuth2 vendors (e.g. `AtlassianOauth2` for Jira) where available; Jira also supports API-key auth via a pre-computed HTTP Basic `base64(email:token)` credential, and Asana uses a Personal Access Token. GitHub + Asana are proven live end-to-end (agent → real upstream data); Jira/Slack/Salesforce paths are in place pending live credentials.
- **Template Gallery** -- Six production-ready templates to deploy in one click: Web Search Agent, Strands + Gateway, Customer Support Assistant, Customer Support Blueprint, MCP Server Runtime, and MCP Server Gateway Target.
- **CloudFormation Export** -- Generate downloadable CloudFormation stacks from any template or free-form diagram. Includes `deploy.sh`, `teardown.sh`, template YAML, and all code artifacts in a single zip. External users can deploy without the platform.
- **AI Tool Generator** -- Describe a tool in natural language (e.g. "a tool that fetches GitHub repo info") and Claude Sonnet on Bedrock generates a complete Lambda function with tool schema. Add it to your canvas and deploy as a Gateway Target.
- **MCP Server on Runtime** -- Host tools directly on the Runtime via MCP protocol with embedded Python functions. No Gateway or Lambda needed -- the simplest path to a tool-using agent.
- **Dynamic Gateway Tool-to-Lambda Pipeline** -- User-selected tools are deployed as a single Lambda function behind the MCP Gateway. The agent discovers tools at runtime through the MCP protocol, not via embedded code.
- **Custom Deployments** -- Create your own agent configurations with any combination of tools and models from 13 providers.
- **Strands Agents SDK** -- All agents are built on the Strands Agents framework with provider-aware model initialization. Supports `BedrockModel`, `OpenAIModel`, `AnthropicModel`, `GeminiModel`, `MistralModel`, `OllamaModel`, `GroqModel`, `LiteLLMModel`, `SageMakerModel`, and more.
- **13 Model Providers** -- Choose from Amazon Bedrock (default, IAM-based), OpenAI, Anthropic (direct), Google Gemini, Mistral, Ollama (local), Groq, DeepSeek, Together AI, LiteLLM, SageMaker, Writer, and LlamaAPI.
- **Multi-Agent Patterns** -- Orchestrate multiple agents using Graph (directed agent graph), Swarm (dynamic handoff), or Workflow (sequential/parallel pipeline) patterns via `strands.multiagent`.
- **BedrockAgentCoreApp SDK** -- All deployed agents use the `BedrockAgentCoreApp` SDK for the AgentCore Runtime protocol (`GET /ping` + `POST /invocations`).
- **Tool-Calling Loop** -- Agents use the Converse API with native `toolConfig` for structured tool calling, with multi-turn tool loops until final response.
- **MCP Gateway Integration** -- Gateway-enabled agents discover and invoke tools via the Model Context Protocol (MCP) over HTTP, with Cognito OAuth2 authentication.
- **Real-time Validation** -- Connection compatibility checks, required field validation, and orphan node warnings.
- **Step Functions Orchestration** -- Multi-step deployments with built-in retries (3 attempts, exponential backoff), per-step timeouts, and persistent state tracking.
- **Deployment Persistence** -- Active deployments are stored per-user. Switching browsers or refreshing shows a banner to restore the last deployment with its test panel.
- **In-Canvas Testing** -- Test deployed agents directly from the UI with conversation history support.
- **Knowledge Base (RAG)** -- Create Bedrock Knowledge Bases with 5 data source types (S3, Web Crawler, Confluence, Salesforce, SharePoint), 3 vector store types (S3 Vectors, OpenSearch Serverless, RDS Aurora PostgreSQL), 3 parsing strategies (Default, Bedrock Data Automation, Foundation Model), 3 chunking strategies (Fixed-Size, Hierarchical, Semantic), custom transformation Lambda, and configurable deletion policy and KMS encryption. The Web Crawler data source filters empty / null seed URLs before submission (accepting `seedUrls`, `webCrawlerUrls`, or `webCrawlerUrl`), BDA parsing auto-attaches `supplementalDataStorageConfiguration` pointing at the artifacts bucket, and S3 Vectors managed mode is the default — with an "Advanced (custom bucket)" toggle in the modal to attach an existing `s3VectorsBucketArn` / `s3VectorsIndexName` / `s3VectorsIndexArn`. **OpenSearch Serverless is fully auto-provisioned** when no `opensearchCollectionArn` is supplied: the KB step creates the collection + encryption/network/data-access policies + the knn vector index (1024-dim), waits for it to become ACTIVE, and records it to the deployment manifest for teardown — so an OSS-backed KB "just works" without bringing your own collection (a caller-supplied ARN is still honored if present). Deployed as a Gateway tool target with a per-deployment Lambda for RetrieveAndGenerate.
- **Full Resource Cleanup (manifest-driven)** -- Every deploy step records the AWS sub-resources it creates into a per-deployment manifest (`created_resources[]`); delete then iterates the manifest and tears down **everything** — Runtime/Harness, Gateway + targets, Cognito User Pool, credential providers, connector secrets, Knowledge Base, auto-provisioned S3 Vectors buckets and OpenSearch Serverless collections (+ their access/security policies — a standing billable resource, priority-ordered to outlive the KB), Memory (+ its IAM role), Policy Engine, Guardrail, custom-tool Lambdas, and IAM roles. Teardown is complete-by-construction (no orphans when a new component type is added), with the legacy per-component cleanup retained as a fallback for pre-manifest records.

## Enterprise Platform Capabilities

Beyond the core canvas, the platform layers an enterprise feature set on top of raw AgentCore primitives. Each capability is wired end-to-end (UI → API → Step Functions / store → AWS) and owner-scoped for multi-tenant safety.

### Agent lifecycle & quality

- **Agent Versioning & Rollback** -- Every deploy is an immutable, versioned snapshot. A runtime has `production` / `staging` slots; `GET /api/runtimes/{name}/versions` lists history and `POST .../rollback` promotes the previous version back into production. Lets you ship, compare, and revert without losing prior canvases.
- **Cedar Policy Enforcement (`ENFORCE`)** -- Attach an AgentCore Policy node to a Gateway and the platform generates schema-correct Cedar (`permit(principal is AgentCore::OAuthUser, action in [AgentCore::Action::"Target___tool", …], resource == AgentCore::Gateway::"<arn>")`) over the **real** synced tool manifest. The action list form (`action in [...]`) is required even for a single tool — a singleton `action == "X"` is rejected by AgentCore's analysis as overly-permissive. Forbidden tools are denied by omission (default-deny) — and because `ENFORCE` filters MCP `tools/list`, a forbidden tool is **invisible** to the agent, not merely un-callable. The engine↔gateway authorization plane takes **~20–60 minutes (variable, AgentCore-side)** to become consistent on a freshly-created gateway; until then `create_policy`/`update_policy` end `CREATE_FAILED`/`UPDATE_FAILED "Insufficient permissions to call gateway"`. That is too long to block the deploy, so the policy step attaches the engine **fail-closed in `ENFORCE`** with the permit recorded as `enforce_pending` (default-deny means tools are temporarily unavailable, never *un*protected), and a shared `_maybe_promote_policy()` **converges the permit to `ACTIVE` on each invoke/status touchpoint** once the gateway settles. The promoter recovers a `CREATE_FAILED` permit **in place via `update_policy`** (stable policy id, no delete/recreate name-collision window) so overlapping status-poll invocations are idempotent under concurrency. State is surfaced via `policy_result.{mode,enforce_validation_pending,promoted_at_first_use}`. Requires the deployment Lambda role to hold `bedrock-agentcore:UpdatePolicy`. Verified live: post-convergence `tools/list` returns only permitted tools, permitted tools return data, forbidden tools are denied.
- **Evaluation Framework** -- Register AgentCore Online Evaluation (built-in goal-success / correctness / helpfulness evaluators + custom judge prompts) at deploy time. `GET /api/runtimes/{name}/evaluations` aggregates per-evaluator scores from the runtime's CloudWatch log group via Logs Insights.
- **Observability Dashboard** -- Each deploy upserts a per-runtime CloudWatch dashboard (latency, invocations, errors, token usage, tool-call success). `GET /api/runtimes/{name}/dashboard-url` returns the deep link. Platform Lambdas and deployed agents both emit OTLP spans (see [Observability](#observability)).
- **Cost & Usage Analytics** -- `GET /api/runtimes/{name}/cost` prices `gen_ai.usage.*` token counts from the runtime's logs against a baked Bedrock price table and returns `{total_cost, total_in, total_out, by_model}` for the requested window.

### Authoring & reuse

- **NL Agent Generator (describe → canvas)** -- `POST /api/generate-canvas` turns a natural-language description into a validated canvas spec via Bedrock tool-use (two-turn: clarify → generate). Generated tools are constrained to real built-in tool IDs (or custom tools with an input schema) so a generated agent always deploys with working gateway targets.
- **Agent Registry with two-persona approval** -- An org-wide catalog to publish, discover, and clone agents as reusable blueprints. **Role-based via Cognito groups**: a `registry-developer` publishes (entry enters `pending`), browses approved entries, and clones approved/own entries — but **cannot approve**; a `registry-admin` sees the pending-review queue, approves/rejects submissions, and can delete any entry. Publish from the Deploy panel; browse/clone from the canvas. See [Registry Roles & Approval](#agent-registry--roles--approval).
- **Prompt Library** -- Versioned, reusable system prompts (`/api/prompts`) with version history, a promotable default version, and resolve-by-reference at codegen time. A runtime's `systemPrompt` can be an inline string or a `{prompt_id, version_id?}` reference.
- **Python Code Export ("eject")** -- `POST /api/export-python` returns a standalone, runnable Python project (`agent.py`, `requirements.txt`, `Dockerfile`, `run.sh`, `.env.example`, `README`) so an agent can run independently of the platform. (Companion to the existing CloudFormation export.)

### Integration & automation

- **A2A (Agent-to-Agent)** -- Deploy a runtime that serves a `/.well-known/agent-card.json` and exposes an SSRF-guarded `call_a2a_peer` tool (https-only, host allowlist + IP denylist) so agents can discover and invoke peer agents.
- **Per-Agent Identity** -- Opt-in `identityConfig.mode=per_agent` mints a distinct least-privilege IAM execution role scoped to exactly the resources an agent is wired to (vs the shared demo role). Roles are tagged `ManagedBy=agentcore-flows` so cleanup is tag-scoped.
- **Agentic Retrieval** -- Knowledge Base nodes support `retrievalStrategy` of `multi_hop` (LLM query decomposition + iterative retrieve), `hybrid` (vector + keyword, with managed-KB fallback to semantic), or `reranked` (wide retrieve + Claude-judge reorder) beyond simple retrieval.
- **Scheduled / Event Triggers** -- Register `cron`, `eventbridge`, `s3`, or `webhook` triggers on a runtime (`/api/runtimes/{name}/triggers`). The `target_runtime_arn` is derived server-side from the owned production slot (confused-deputy guard); webhook triggers mint an owner-scoped HMAC secret in Secrets Manager. New triggers are recorded as **`registered`** (not yet firing) until the AWS resource is provisioned — the UI never falsely shows an unwired trigger as active.
- **SaaS Connectors (live OpenAPI Gateway targets)** -- A curated catalog (Jira/`AtlassianOauth2` + API-key Basic, Asana/PAT, Slack, GitHub, Salesforce) plus a generic OpenAPI/MCP connector, deployable as real Gateway targets. The deploy step fetches the connector's OpenAPI spec (SSRF-guarded, host-allowlisted), registers an API-key or OAuth2 client-credentials credential provider backed by an owner-scoped Secrets Manager secret, and attaches it to the gateway target. OpenAPI targets are crawled (not inline) and readiness is verified by probing the gateway's live MCP `tools/list`. `GET /api/connectors` lists the catalog for discovery. Teardown deletes the target, credential provider, and secret via the resource manifest. **Live-verified:** GitHub (api-key → real `/user` login) and Asana (PAT → real `/users/me`) end-to-end; Jira/Slack/Salesforce require live credentials to exercise.
- **GitOps Sync** -- Store a Git PAT in an owner-scoped Secrets Manager namespace (`/api/workflows/{id}/git-token`) and pull a workflow spec from a repo (`/api/workflows/{id}/git-sync`), preserving id/owner/ACL. SSRF-guarded.
- **Human-in-the-Loop (HITL)** -- Inject a `human_approval` tool into an agent; pending approvals land in an owner-scoped queue (`GET /api/hitl/pending`, `POST /api/hitl/{id}/decision`) surfaced in the UI inbox.
- **Team Workspaces & Sharing** -- Share a workflow with viewer/editor roles (`/api/workflows/{id}/share`); list workspace-visible workflows (`GET /api/workspaces`). Owner-only mutation with escalation guards.

## Security

### Infrastructure Hardening

- **S3 enforce_ssl** -- Both the frontend and artifacts S3 buckets require HTTPS (`enforce_ssl=True`), auto-generating a bucket policy that denies non-TLS requests.
- **CloudFront security headers** -- A custom `ResponseHeadersPolicy` on all CloudFront behaviors sets:
  - `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload` (HSTS, 2 years)
  - `X-Frame-Options: DENY`
  - `X-Content-Type-Options: nosniff`
  - `X-XSS-Protection: 1; mode=block`
  - `Referrer-Policy: strict-origin-when-cross-origin`
- **Least-privilege IAM** -- Bedrock permissions scoped to `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` (not `bedrock:*`). Each Step Functions step Lambda has its own role with only the IAM actions it needs (no shared kitchen-sink policy). CDK feature flag `@aws-cdk/aws-iam:minimizePolicies` merges overlapping statements.
- **Tenant isolation** -- Workflows, flows, and deployments are owner-scoped to the Cognito JWT `sub` claim. Cross-tenant reads return 404 (not 403) to avoid leaking record existence. Enforced in `backend/src/app/services/auth.py::assert_owner`.
- **Shared runtime execution role** -- The platform pre-creates one `AgentCoreRuntime-{project}-{env}-shared` role at stack-init with `s3:GetObject` on the artifacts bucket + Bedrock + tool permissions, rather than minting a fresh per-deploy role. This avoids the IAM-cache propagation race that otherwise blocks first-time `CreateAgentRuntime` calls. The `DELETE /api/runtime/{id}` path explicitly skips deleting this shared role.
- **Cognito hardening** -- `prevent_user_existence_errors=ENABLED`, `USER_PASSWORD_AUTH` disabled (SRP only), `ADMIN_NO_SRP_AUTH` disabled.
- **TLS 1.2 minimum** -- CDK feature flag `@aws-cdk/aws-cloudfront:defaultSecurityPolicyTLSv1.2_2021` enforces TLS 1.2+ on CloudFront.
- **CORS** -- API Gateway allows `http://localhost:5173` only (for local dev). In production, the frontend and API are served from the same CloudFront domain, making requests same-origin — no CORS headers needed.

### CDK-NAG (AWS Solutions Checks)

CDK-NAG (`cdk_nag.AwsSolutionsChecks`) runs during every `cdk synth` to flag security best-practice violations. Suppressions are scoped per-construct via `NagSuppressions.add_resource_suppressions(<construct>, [...], apply_to_children=True)` in `PlatformStack._apply_nag_suppressions()` — never stack-wide. Each suppression names the specific construct that legitimately needs the exception (e.g. shared runtime exec role for IAM4/IAM5, Cognito user pool for COG2/COG4/COG8, the State Machine for SF1) so a future contributor adding a wildcard policy to an unrelated construct fails the build instead of silently absorbing the finding. Unsuppressed violations cause synthesis to fail.

### Reliability & Operational Hardening

- **RemovalPolicy gating** — DynamoDB tables, S3 buckets, and the Cognito user pool default to `RETAIN` in production. Set `ENVIRONMENT_NAME` to one of `dev|test|sandbox|preview|ephemeral` (or export `AGENTCORE_ALLOW_DESTROY=true`) to switch to `DESTROY` + `auto_delete_objects=True` for fast iteration. Guards against accidental data loss on `cdk destroy` against a long-running prod stack.
- **runtime_id-index GSI** — `DeploymentsTable` has a GSI keyed on `runtime_id` so `DELETE /api/runtime/{id}` and `POST /api/test-runtime` resolve via O(1) Query instead of O(N) Scan. Falls back to paginated Scan when the GSI is absent (covers stacks deployed before the GSI was added).
- **Cleanup-failure aggregation** — `handle_delete_runtime` tracks per-resource cleanup failures (`mcp_server_runtime`, `policy_engine`, `memory`, `guardrail`, `gateway`, `kb_lambda`, `knowledge_base`) and only returns `success=true` when **all** cleanups succeed. Prevents reporting success when a Cognito pool / KB / guardrail leaks.
- **Idempotent guardrail creation** — `guardrails_step` catches `ResourceAlreadyExistsException` from `create_guardrail`, then either updates the existing guardrail in place or retries with a UUID-suffixed name. Step Functions retries no longer break a partially-deployed flow.
- **Gateway deploy rollback** — `deploy_gateway` tracks partial state (Cognito client info, gateway ID, tool Lambdas, custom-tool roles) and runs `cleanup_gateway_resources` on any mid-flow exception before re-raising. No more orphan Cognito pools / Lambdas after a transient AgentCore error.
- **SSRF guard on Gateway URL fetches** — Any URL the gateway deployer follows is validated before `urlopen` against a 21-network IPv4/IPv6 denylist (loopback, link-local incl. IMDS `169.254.169.254` and Lambda creds `169.254.170.2`, RFC1918, CGNAT, multicast, ULA, IPv4-mapped IPv6). DNS resolution is performed up-front so hostname rebinding (`evil.com → 169.254.169.254`) cannot bypass the check. Optional `OIDC_DISCOVERY_HOST_ALLOWLIST` env var pins discovery hosts to an operator-approved set. Same defense applied to the embedded `_do_fetch_webpage` tool Lambda.
- **OTEL secret namespace lock** — User-supplied `auth_header_secret_arn` (per-canvas Observability node) is validated against `^arn:aws:secretsmanager:.*:secret:agentcore-otel/.*` before being granted to the runtime IAM role. Foreign ARNs are rejected at the API boundary; tenant cannot trick the runtime into reading + exfiltrating arbitrary secrets via OTLP headers. Secrets created via `POST /api/observability/credentials` are tagged with `owner_sub` (Cognito sub) so cross-tenant ownership is auditable.
- **Tenant isolation hardening** — The `X-Test-Sub` header bypass is removed from `services/auth.py`; tests inject sub via FastAPI `dependency_overrides`. `assert_owner` returns 404 for None-owner records (no legacy-data bypass). Flow/workflow listing uses strict `owner_sub == caller_sub` equality (no None-coalescing fallback that previously surfaced legacy rows in every tenant's list).
- **MCPClient wiring proof gate** — When a runtime is configured with `GATEWAY_URL` but `MCPClient.list_tools_sync()` returns an empty list, the runtime raises `RuntimeError("Gateway MCPClient returned 0 tools…")` at first invocation rather than letting the agent bluff a canary out of the system prompt. Coverage-audit finding #109 (9 silent-canary GW PASSes) is now structurally impossible.
- **Cedar ENFORCE policy enforcement (fail-closed, converge-in-place)** — When a Policy node runs in `ENFORCE` mode, the policy step builds a schema-correct Cedar policy set against the gateway's real MCP tool manifest: one `permit(principal is AgentCore::OAuthUser, action in [AgentCore::Action::"{Target}___{tool}", …], resource == AgentCore::Gateway::"<arn>")` over the **allowed** tools (the `action in [...]` **list** form is mandatory even for one tool — a singleton `action == "X"` is rejected as "Overly Permissive"), with forbidden tools **denied by omission** (AgentCore is default-deny; `ENFORCE` also filters `tools/list` so a forbidden tool is invisible to the agent). Several guards keep enforcement honest: (1) policy names are **engine-prefixed** because AgentCore policy names are account-global, not engine-scoped (Bug 137); (2) on a name conflict the step recovers the existing policy *from this engine* and validates it, or aborts if the name belongs to a foreign engine; (3) it never reports success on an engine holding **0 ACTIVE policies**. A freshly-created policy engine + gateway take **~20–60 minutes (variable, AgentCore-side)** to become consistent (`create_policy`/`update_policy` end `CREATE_FAILED`/`UPDATE_FAILED "Insufficient permissions to call gateway"` until then), too long to block the deploy. So the step attaches the engine **fail-closed in `ENFORCE`** with the permit recorded as `enforce_pending` — default-deny leaves tools temporarily *unavailable* rather than *unprotected* (a forbidden-tool value must never leak) — and a shared `_maybe_promote_policy()` **converges the permit to `ACTIVE` on each invoke/status touchpoint** once the gateway settles. The promoter recovers a failed permit **in place via `update_policy`** (stable policy id — NOT delete+recreate, which raced concurrent status-poll invocations on the account-global name and never converged), skips in-flight (`CREATING`/`UPDATING`) policies, and treats `ConflictException` as a benign concurrent-run signal. `update_policy`'s `description` is a `{"optionalValue": str}` structure (not a bare string like `create_policy`). Requires `bedrock-agentcore:UpdatePolicy` on the deployment Lambda role. State is surfaced via `policy_result.{mode,enforce_validation_pending,promoted_at_first_use}`. Verified live: post-convergence `tools/list` returns only permitted tools, permitted tools return data, forbidden tools are denied; the policy engine is torn down children-first on delete.
- **DDB GSI NULL-key safety** — `DeploymentState` serializer omits None-valued optional fields (`runtime_id`, `gateway_url`, `completed_at`, etc.) via `model_dump(mode="json", exclude_none=True)` so the `runtime_id-index` GSI accepts the initial intake write. Pairs with the runtime_id-index GSI for cost-bounded delete/test/invoke lookups.
- **Frontend ErrorBoundary** — `frontend/src/components/ErrorBoundary.tsx` wraps the app root. A render-time exception shows a recoverable banner with reset/reload buttons instead of a blank screen.
- **Auto-save error toast** — `useAutoSave` exposes `lastSaveError` so a transient save failure renders a dismissable toast instead of being clobbered by a subsequent successful read.

### Pre-commit Hooks

`.pre-commit-config.yaml` includes:
- `detect-secrets` -- Prevents accidental secret commits (API keys, passwords) with a baseline file
- `detect-private-key` -- Blocks commits containing private keys
- `check-added-large-files` -- Rejects files over 1MB
- `no-commit-to-branch` -- Prevents direct commits to `main`
- `ruff` -- Python linting and formatting checks
- Standard checks: trailing whitespace, end-of-file fixer, YAML/JSON validation, merge conflict markers

Install with `pip install pre-commit && pre-commit install`. Run manually with `pre-commit run --all-files`.

### Holmes Security Review — status & known hardening items

The codebase is scanned with **Holmes** (Content Security Review rubric + SMGS AppSec
static-analysis baseline: Checkov / cfn-guard / Semgrep). Latest scan (97 files across
`backend/src`, `infra/stacks`, `scripts`).

**Remediated:**
- **Harness execution role least-privilege** — `bedrock:InvokeModel` is scoped to the
  connected model's family ARN, and the memory/gateway data-plane actions are scoped to
  the connected memory/gateway ARNs (falling back to `*` only when an ARN is unknown).
  Token-vault fetches (`GetResourceOauth2Token`/`GetResourceApiKey`) stay account-level
  (no resource-ARN form). See `services/harness_deployer.create_harness_iam_role`.
- **Exported `requirements.txt` supply chain** — generated dependency lists now carry
  `>=` minimum-version floors and a header instructing users to pin exact versions before
  a production build. See `services/python_exporter.build_requirements`.
- **Teardown completeness (no orphans)** — IAM grants added for the manifest delete path so
  no managed resource orphans on delete: `s3vectors:Delete*` + `bedrock:DeleteKnowledgeBase`/
  `DeleteDataSource` (KB vector store), `lambda:DeleteFunction` scoped to `MCPServer*` as well
  as `AgentCore*`, and the policy-engine teardown deletes child policies before the engine.
  An `IAM-completeness` test asserts every manifest delete verb is granted.

**Known hardening items (tracked, lower priority for a 1:Many sample):**
- Several platform IAM roles (shared runtime role, deployment Lambda, per-step roles, and
  the generated CloudFormation evaluation/memory/CFN-provider roles) use broad
  `bedrock-agentcore:*` / `Resource: "*"` actions for development convenience. Production
  deployments should scope these to specific resource ARNs and tag-based IAM conditions
  (`ManagedBy=agentcore-flows`), and constrain destructive actions
  (`Delete*`, `logs:DeleteLogGroup`, `cloudwatch:DeleteDashboards`).
- Several `logger.exception(...)` call sites may include user-supplied prompt/config in
  tracebacks; production should use structured logging that records error type + id only
  and routes detail to a tenant-scoped store.
- Connector credential secrets use the default Secrets Manager key; consider a customer
  KMS CMK per your data-classification policy.

## Observability

The platform supports two OTEL deployment modes — they are not mutually exclusive.

**Per-canvas (the original mode).** Drop an Observability node onto the canvas, configure the OTLP endpoint + credentials in the modal, and the agent's traces are exported to that backend.

**Platform-level (added later).** Configure once at deploy time via the `OTEL_*` env vars listed above. Every deployed agent inherits the configuration automatically, AND every platform Lambda (workflow Lambda, deployment Lambda, all Step Functions step handlers) emits OTLP spans to the same backend. When a deploy is stuck, the stuck step Lambda shows up as a span in your backend alongside the agent invocations it's trying to set up.

Platform-level config takes precedence over per-canvas: the endpoint, secret ARN, sample rate, and service-name prefix are admin-locked. Per-canvas Observability nodes can still add resource attributes additively (e.g. `team=ops`), but cannot override the endpoint.

Implementation:

- `backend/src/app/services/_otel_platform.py` — module-load OTel SDK setup imported as the first import of every Lambda handler. Resolves `OTEL_AUTH_SECRET_ARN` from Secrets Manager into `OTEL_EXPORTER_OTLP_HEADERS`, sets up `BatchSpanProcessor` + `OTLPSpanExporter` (HTTP), instruments boto3.
- `backend/src/app/services/observability.py::get_platform_observability_defaults()` — reads SSM at module load, cached via `lru_cache`.
- `backend/src/app/services/code_generator.py::_inject_otel()` — post-processes generated agent code to bootstrap Strands `StrandsTelemetry` + force-flush spans on shutdown.

Verified live against Langfuse Cloud — both platform Lambda traces and deployed-agent traces appear under the same project.

## Prerequisites

- **AWS CLI** v2 -- configured with credentials (`aws configure`)
- **Node.js** 18+
- **Python** 3.12+

No Docker installation required. CDK is invoked via `npx` (no global install needed).

## Deploy

The platform supports two deploy modes. Pick **one** per environment.

### Mode 1: Without OTEL (default)

Use this when you don't have an OTLP backend yet, or you don't need centralized tracing. Each deployed agent can still configure its own per-canvas Observability node from the UI later — platform Lambdas just won't emit traces.

```bash
# Minimal deploy (dev environment, us-east-1)
COGNITO_USERS="user@example.com" ./scripts/deploy.sh

# Specific environment and region
COGNITO_USERS="user@example.com" ENVIRONMENT_NAME=prod AWS_REGION=us-west-2 ./scripts/deploy.sh
```

### Mode 2: With platform-level OTEL

Use this when you want **every deployed agent AND every platform Lambda** (workflow, deployment, all 13 Step Functions step handlers) to export OTLP traces to a single backend automatically. The endpoint becomes admin-locked — per-canvas Observability nodes can still add resource attributes (e.g. `team=ops`) but cannot override the endpoint.

**Prerequisite (one-time)**: create the auth-header secret with your OTLP backend credentials. The example below uses Langfuse Cloud; any OTLP-compatible backend with HTTP Basic auth works.

```bash
LANGFUSE_PUBLIC_KEY=pk-... LANGFUSE_SECRET_KEY=sk-... ./scripts/bootstrap-otel-secret.sh
# Prints the secret ARN — copy it for the next command.
```

**Deploy with platform OTEL enabled**:

```bash
COGNITO_USERS="user@example.com" \
  OTEL_ENDPOINT="https://cloud.langfuse.com/api/public/otel" \
  OTEL_AUTH_SECRET_ARN="arn:aws:secretsmanager:us-east-1:...:secret:agentcore-otel/platform/dev-XXXXXX" \
  OTEL_SAMPLE_RATE=1.0 \
  ./scripts/deploy.sh
```

To switch an existing deployment from Mode 1 → Mode 2 (or vice versa), just re-run `./scripts/deploy.sh` with the new env vars set/unset. CDK reconciles the change in place; no resource churn.

The deploy script (`scripts/deploy.sh`) will:
1. Validate prerequisites (Node.js, Python, AWS CLI -- exits with descriptive error if missing)
2. Validate AWS credentials
3. Install CDK and backend dependencies
4. Install Lambda dependencies for Linux x86_64 into `backend/lib/`
5. Build AgentCore dependency bundles (`base.zip`, `strands-mcp.zip`) into `backend/agentcore-deps/`
6. Bootstrap CDK (if needed)
7. Run `cdk deploy` (creates API Gateway, Lambda functions, Step Functions, DynamoDB tables, S3, CloudFront)
8. Extract API Gateway URL and CloudFront URL from stack outputs
9. Build the frontend with `VITE_API_BASE_URL` pointing to the CloudFront URL
10. Upload frontend assets to S3
11. Invalidate the CloudFront cache
12. Print the CloudFront URL (frontend) and API Gateway URL (API)

Lambda code is packaged automatically by CDK from the `backend/` directory -- no Docker build or ECR push required.

### Accessing the Platform

After deployment completes, the script prints two URLs:

- **Frontend** -- `https://dXXXXXXXXXX.cloudfront.net` -- open this in your browser to access the visual workflow builder.
- **Backend API** -- `https://XXXXXXXXXX.execute-api.region.amazonaws.com` -- the API Gateway endpoint (CloudFront routes `/api/*` here automatically).

You can also retrieve these URLs at any time from the CloudFormation stack outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name agentcore-workflow-dev \
  --query "Stacks[0].Outputs" \
  --output table
```

Look for `CloudFrontUrl` (frontend), `ApiGatewayUrl` (API), and `S3BucketName` (frontend assets).

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ENVIRONMENT_NAME` | `dev` | Environment identifier (e.g., `dev`, `staging`, `prod`) |
| `AWS_REGION` | `us-east-1` | Target AWS region |
| `PROJECT_NAME` | `agentcore-workflow` | Project name used for resource naming and tagging |
| `COGNITO_USERS` | *(none)* | Comma-separated emails for pre-created Cognito users (e.g., `user1@example.com,user2@example.com`) |
| `OTEL_ENDPOINT` | *(unset)* | OTLP HTTP endpoint for platform-level observability (e.g. `https://cloud.langfuse.com/api/public/otel`). When set, every platform Lambda + every deployed agent exports traces here. Per-canvas Observability nodes can still add resource attributes additively but cannot override the endpoint. |
| `OTEL_AUTH_SECRET_ARN` | *(unset)* | ARN of a Secrets Manager secret holding the precomputed `Authorization` header value (e.g. `Basic <base64>`). Created by `scripts/bootstrap-otel-secret.sh`. Required when `OTEL_ENDPOINT` is set. |
| `OTEL_SAMPLE_RATE` | `1.0` | Trace sampling ratio (0.0–1.0). |
| `OTEL_SERVICE_NAME_PREFIX` | `{PROJECT_NAME}` | Prefix prepended to `service.name` resource attribute on every span. |

These are passed as CDK context parameters to the infrastructure stack.

### Environment Variables (Lambda)

| Variable | Description |
|----------|-------------|
| `DEPLOYMENT_TABLE_NAME` | DynamoDB table name for deployment state |
| `WORKFLOWS_TABLE_NAME` | DynamoDB table name for workflow definitions |
| `STATE_MACHINE_ARN` | Step Functions state machine ARN for deployment orchestration |
| `APP_AWS_REGION` | AWS region for service calls |
| `TOOL_GENERATOR_MODEL_ID` | Claude model ID for AI Tool Generator (default: `us.anthropic.claude-sonnet-5`) |

### SSM Parameters

Application configuration is stored under `/agentcore-workflow/{env}/` in SSM Parameter Store:

| Parameter | Description |
|-----------|-------------|
| `/agentcore-workflow/{env}/cors-origins` | Allowed CORS origins |
| `/agentcore-workflow/{env}/aws-region` | AWS region |
| `/agentcore-workflow/{env}/dynamodb-table-name` | Workflows DynamoDB table name |
| `/agentcore-workflow/{env}/otel/endpoint` | OTLP endpoint (when platform OTEL is configured) |
| `/agentcore-workflow/{env}/otel/auth-secret-arn` | Secrets Manager ARN for the OTLP auth header |
| `/agentcore-workflow/{env}/otel/sample-rate` | Trace sampling ratio |
| `/agentcore-workflow/{env}/otel/service-name-prefix` | `service.name` prefix |

## Deployment Flow

There are two deployment flows: **infrastructure deployment** (deploying the platform itself to AWS) and **agent deployment** (deploying an AI agent from the UI).

### Infrastructure Deployment (`./scripts/deploy.sh`)

When you run `./scripts/deploy.sh`, this is the sequence of operations:

```
1. Check prerequisites
   Node.js, npm, Python 3, AWS CLI, npx — exits with descriptive error if any missing

2. Validate AWS credentials
   aws sts get-caller-identity — verifies configured credentials are valid

3. Install CDK dependencies
   pip install -r infra/requirements.txt (aws-cdk-lib, constructs, cdk-nag)
   npm install in infra/ (for npx cdk)

4. Install backend dependencies
   pip install backend/ (FastAPI, Pydantic, boto3, Mangum)

5. Install Lambda dependencies (platform-targeted)
   scripts/install-lambda-deps.sh
   pip install into backend/lib/ with --platform manylinux2014_x86_64
   (pydantic-core and other native packages compiled for Amazon Linux)

6. Build AgentCore dependency bundles (ARM-targeted)
   scripts/install-agentcore-deps.sh
   Creates backend/agentcore-deps/base.zip (bedrock-agentcore + boto3, aarch64)
   Creates backend/agentcore-deps/strands-mcp.zip (+ strands-agents + mcp, aarch64)

7. Bootstrap CDK (if first time in this region)
   npx cdk bootstrap aws://{account}/{region}
   Creates the CDKToolkit stack with S3 bucket for CDK assets

8. Run cdk deploy
   Synthesizes CloudFormation template (CDK-NAG runs here — fails on violations)
   Creates/updates all AWS resources in a single stack:
     → API Gateway HTTP API (routes /api/* to Lambda)
     → Workflow Lambda (FastAPI + Mangum, handles CRUD)
     → Deployment Lambda (handles deploy/test/delete/generate-tool)
     → 13 Step Function step Lambdas, each with its own least-privilege IAM role
       (validate, codegen, iam, gateway, knowledge_base, mcp_server, memory,
       policy, evaluation, runtime_configure, runtime_launch, auth, status_update)
     → Step Functions state machine (orchestrates the 13 steps)
     → Shared AgentCore runtime execution role (warmed at stack-init to bypass
       AgentCore's IAM-cache propagation race)
     → DynamoDB tables (workflows + deployments)
     → S3 bucket (frontend assets + dependency bundles + code artifacts)
     → CloudFront distribution (SPA routing + API origin)
     → SSM parameters (CORS origins, region, table name, OTEL config when enabled)
     → IAM roles (least-privilege per function)
     → CloudWatch log groups (Lambda + Step Functions)

9. Extract stack outputs
   Reads ApiGatewayUrl, CloudFrontUrl, S3BucketName from CloudFormation outputs

10. Build frontend
    cd frontend && VITE_API_BASE_URL={CloudFrontUrl} npm run build
    Bakes the CloudFront URL into the React SPA at build time

11. Upload frontend to S3
    aws s3 sync frontend/dist/ s3://{bucket} --delete

12. Invalidate CloudFront cache
    aws cloudfront create-invalidation --paths "/*"

13. Print summary
    Frontend URL (CloudFront) + API URL (Gateway)
```

### Agent Deployment (UI → Step Functions)

When a user clicks **Deploy** in the UI, this is the end-to-end flow:

```
Frontend (DeployPanel)
    │
    ├── Collects: runtime config, gateway config, identity config,
    │   connected tools, gateway tools, custom tools (AI-generated),
    │   memory config, policy config, knowledge base config, MCP server config
    │
    └── POST /api/deploy
            │
            ▼
Deployment Lambda (deployment_handler.py)
    │
    ├── Generates deployment_id (UUID)
    ├── Creates initial state in DynamoDB (status: PENDING)
    ├── Builds SFN input payload:
    │     { deployment_id, workflow_id, config, connected_tools,
    │       template_id, gateway_config?, gateway_tools?,
    │       identity_config?, custom_tools?, memory_config?,
    │       policy_config?, knowledge_base_config?, mcp_server_config? }
    │
    └── sfn.start_execution()
            │
            ▼
Step Functions State Machine (13 steps, 30min timeout)
    │
    │  Each step: 3 retries, exponential backoff (2s → 4s → 8s)
    │  On failure: catches error → StatusUpdate writes FAILED to DynamoDB
    │
    ├── Step 1: ValidateWorkflow (30s timeout)
    │   Loads workflow from DynamoDB, runs ValidationEngine
    │   Checks: required fields, connection compatibility, orphan nodes
    │   Output: { is_valid, errors }
    │
    ├── Step 2: HasMcpServer? (Choice)
    │   If mcp_server_config present → DeployMcpServer
    │   Otherwise → skip
    │
    ├── Step 3: DeployMcpServer (600s timeout) [conditional]
    │   Deploys an MCP Server Runtime (FastMCP with embedded tools)
    │   Output: { mcp_server_runtime_arn }
    │
    ├── Step 4: HasKnowledgeBase? (Choice)
    │   If knowledge_base_config present → CreateKnowledgeBase
    │   Otherwise → skip
    │
    ├── Step 5: CreateKnowledgeBase (600s timeout) [conditional]
    │   Creates Bedrock Knowledge Base with selected data source and vector store
    │   Creates data source (S3, Web Crawler, Confluence, Salesforce, SharePoint)
    │   Starts data ingestion sync job
    │   Creates per-deployment Lambda for RetrieveAndGenerate
    │   Output: { knowledge_base_result: { kb_id, lambda_arn, ... } }
    │
    ├── Step 6: HasGateway? (Choice)
    │   If gateway_config present → DeployGateway
    │   Otherwise → skip
    │
    ├── Step 7: DeployGateway (120s timeout) [conditional]
    │   Creates MCP Gateway via bedrock-agentcore API
    │   Deploys a Lambda with tool implementations
    │   Creates Gateway Target with selected tool schemas
    │   Creates KB tool Gateway Target (if KB was deployed)
    │   Sets up Cognito OAuth2 (user pool + app client + resource server)
    │   Creates AI-generated custom tool Lambdas (if any)
    │   Output: { gateway_result: { gateway_url, client_info, ... } }
    │
    ├── Step 8: HasMemory? (Choice)
    │   If memory_config present → CreateMemory
    │   Otherwise → skip
    │
    ├── Step 9: CreateMemory (120s timeout) [conditional]
    │   Creates AgentCore Memory with configured extraction strategy
    │   Output: { memory_id, memory_arn }
    │
    ├── Step 10: HasPolicy? (Choice)
    │   If policy_config present → CreatePolicy
    │   Otherwise → skip
    │
    ├── Step 11: CreatePolicy (120s timeout) [conditional]
    │   Creates AgentCore Policy Engine with Cedar-based policies
    │   Output: { policy_engine_id }
    │
    ├── Choice: deployment_mode == "harness"?
    │   If harness → DeployHarness (create_harness + wait READY, reusing the
    │       gateway/memory results above; records harness_id/arn). SKIPS codegen,
    │       IAM, ConfigureRuntime, and LaunchRuntime, then rejoins at the
    │       evaluation/auth/status_update tail.
    │   Otherwise (runtime, default) → GenerateCode (Step 12 below).
    │
    ├── Step 12: GenerateCode (30s timeout)   [runtime mode]
    │   Generates Strands Agent code (provider-aware model init + multi-agent)
    │   Merges gateway credentials into code (from gateway step output)
    │   Downloads pre-built dependency bundle from S3 (base.zip or strands-mcp.zip)
    │   Merges agent code + dependencies into code.zip
    │   Uploads code.zip to S3
    │   Output: { s3_bucket, s3_key, entrypoint }
    │
    ├── Step 13: CreateIAMRole (60s timeout)
    │   Creates IAM execution role for the runtime
    │   Scopes permissions based on connected tools (gateway, memory, policy, etc.)
    │   Output: { role_name, role_arn }
    │
    ├── Step 14: ConfigureRuntime (60s timeout)
    │   Calls bedrock-agentcore-control.create_agent_runtime()
    │   Points to code.zip in S3
    │   Sets environment variables:
    │     MODEL_ID, MODEL_PROVIDER, GATEWAY_URL, COGNITO_*/OAUTH_* credentials
    │   Output: { runtime_id, runtime_arn }
    │
    ├── Step 15: LaunchRuntime (600s timeout)
    │   Polls bedrock-agentcore-control.get_agent_runtime()
    │   Waits for status == READY (up to 540s)
    │   Retrieves runtime endpoint ARN
    │   Output: { runtime_endpoint, launch_result }
    │
    ├── Step 16: HasEvaluation? (Choice)
    │   If evaluation_config present → CreateEvaluation
    │   Otherwise → skip
    │
    ├── Step 17: CreateEvaluation (120s timeout) [conditional]
    │   Creates AgentCore online evaluation config
    │   Configures evaluators (correctness, faithfulness, helpfulness, etc.)
    │   Output: { evaluation_config_id }
    │
    ├── Step 18: HasGatewayForAuth? (Choice)
    │   If gateway_config present → ConfigureJWTAuth
    │   Otherwise → skip
    │
    ├── Step 19: ConfigureJWTAuth (60s timeout) [conditional]
    │   Configures JWT auth on runtime (uses SigV4 for invocation)
    │   Output: { auth_result }
    │
    ├── Step 20: UpdateStatusSuccess (15s timeout)
    │   Writes final state to DynamoDB:
    │     status: SUCCEEDED, runtime_id, runtime_endpoint,
    │     gateway_url, gateway_result, knowledge_base_result (for cleanup later)
    │
    └── DeploymentSucceeded
            │
            ▼
Frontend polls GET /api/deploy/{deployment_id}
    │
    └── Shows status updates → "Deployed" with test panel
```

### Agent Deletion Flow

When a user clicks **Delete** in the UI:

```
DELETE /api/runtime/{runtime_id}
    │
    ├── Scans DynamoDB for deployment record matching runtime_id
    ├── Reads gateway_result, policy_result, memory_result,
    │   knowledge_base_result from the record
    │
    ├── Destroy MCP Server Runtime (if deployed)
    │
    ├── Cleanup Policy Engine (if deployed)
    │   ├── Detach engine from Gateway
    │   ├── Delete all policies
    │   └── Delete Policy Engine
    │
    ├── Cleanup Memory (if deployed)
    │   └── bedrock-agentcore-control.delete_memory()
    │
    ├── Cleanup Knowledge Base (if deployed)
    │   ├── Delete data sources
    │   ├── Delete Knowledge Base
    │   └── Delete KB tool Lambda
    │
    ├── Cleanup Gateway resources (if deployed)
    │   ├── Delete Gateway Targets
    │   ├── Delete MCP Gateway
    │   ├── Delete tools Lambda function
    │   ├── Delete custom tool Lambdas (AI-generated)
    │   └── Delete Cognito User Pool
    │
    ├── Destroy Agent Runtime
    │   bedrock-agentcore-control.delete_agent_runtime()
    │
    └── Update DynamoDB status → DELETED
```

## Cleanup

```bash
# Tear down all resources
./scripts/cleanup.sh

# Tear down a specific environment
ENVIRONMENT_NAME=prod AWS_REGION=us-west-2 ./scripts/cleanup.sh
```

The cleanup script (`scripts/cleanup.sh`) will:
1. Validate AWS credentials
2. Check if the stack exists
3. Empty the S3 bucket (required before stack deletion)
4. Run `cdk destroy --force`
5. Verify all resources have been removed

## AWS Resources Created

The CDK stack (`infra/stacks/platform_stack.py`) creates:

- **API Gateway HTTP API** -- Routes `/api/workflows/*` to Workflow Lambda, `/api/deploy`, `/api/test-runtime`, `/api/runtime/*`, `/api/generate-tool` to Deployment Lambda. HTTPS by default, CORS configured.
- **Workflow Lambda** -- FastAPI app wrapped with Mangum. Handles workflow CRUD, validation, import/export.
- **Deployment Lambda** -- Handles deploy initiation, status polling, runtime testing, runtime deletion (full cleanup), AI tool generation via Claude Sonnet on Bedrock, and CloudFormation template generation/export. 120s timeout for LLM calls.
- **Step Functions State Machine** -- Orchestrates multi-step deployments: validate -> [mcp_server?] -> [knowledge_base?] -> [gateway?] -> [memory?] -> [policy?] -> codegen -> IAM -> runtime configure -> runtime launch -> [evaluation?] -> [auth?] -> status update. 3 retries with exponential backoff per step.
- **Step Lambdas** -- Individual Lambda functions for each deployment step (validate, codegen, IAM, gateway, knowledge_base, mcp_server, memory, policy, evaluation, runtime_configure, runtime_launch, auth, status_update).
- **DynamoDB Tables** -- Workflows + Deployments (TTL + GSI on workflow_id/runtime_id), plus the enterprise-feature stores: `agent-versions`, `runtime-slots`, `agent-registry`, `prompt-library`, `hitl-requests` (24h TTL), `triggers`, `usage-events` (90d TTL), and `flows`. Each user-data table carries an `owner_sub` GSI for owner-scoped list queries.
- **Cognito User Pool Groups** -- `registry-admin` and `registry-developer` for the registry two-persona approval model (see [Agent Registry — Roles & Approval](#agent-registry--roles--approval)).
- **S3 Bucket** -- Frontend static assets (CloudFront OAI access only) + AgentCore dependency bundles + deployment code artifacts.
- **CloudFront Distribution** -- HTTPS, SPA routing (404/403 -> index.html), API Gateway as additional origin for `/api/*`.
- **SSM Parameters** -- CORS origins, AWS region, DynamoDB table name under `/agentcore-workflow/{env}/`.
- **IAM Roles** -- Least-privilege per function: Workflow Lambda gets DynamoDB workflows + SSM read; Deployment Lambda gets DynamoDB deployments + bedrock-agentcore + cleanup permissions (Cognito, Lambda, STS); Step Lambdas get full deployment permissions (IAM, Lambda, Cognito, S3, bedrock-agentcore).
- **CloudWatch Log Groups** -- Lambda and Step Functions execution logs.

All resources are tagged with `environment` and `project` for cost tracking.

## Dynamic Gateway Tool-to-Lambda Pipeline

This is the core innovation of the platform. When a user creates a custom deployment and selects tools (DuckDuckGo Search, Wikipedia, Weather, Web Page Fetcher), those tools are **not embedded** as Python functions inside the agent code. Instead:

```
User selects tools in UI
    |
    v
Backend deploys a single Lambda ("AgentCoreDynamicTools")
containing implementations for ALL supported tools
    |
    v
Backend creates a Gateway target with only the SELECTED tool schemas
registered as inlinePayload (so the agent only sees what was chosen)
    |
    v
Backend creates Cognito OAuth2 (user pool + app client + resource server)
and configures the Gateway with JWT authorizer
    |
    v
Agent code connects to Gateway via MCP protocol (with Cognito token)
    |
    v
Agent discovers available tools from Gateway at runtime via tools/list
    |
    v
Tool calls route:  Agent -> Bedrock Converse API (tool_use) -> MCP Gateway -> Lambda -> API
```

### Why This Matters

| Approach | Problems |
|----------|----------|
| Embed tools in agent code | Bloated deployment, needs extra packages installed on the runtime, tools can't be updated without redeploying the agent |
| **Dynamic Gateway Pipeline** | Agent stays lightweight (only `bedrock-agentcore` + `boto3`), tools run in a managed Lambda, tool schemas are registered per-deployment, tools can be updated by redeploying just the Lambda |

### Supported Tools

| Tool ID | Lambda | API |
|---------|--------|-----|
| `duckduckgo_search` | AgentCoreDynamicTools | DuckDuckGo Instant Answer API |
| `wikipedia_search` | AgentCoreDynamicTools | Wikipedia REST API |
| `weather_api` | AgentCoreDynamicTools | Open-Meteo Geocoding + Weather API |
| `web_page_fetcher` | AgentCoreDynamicTools | stdlib `urllib.request` |
| `get_order` | AgentCoreCustomerSupportTools | Mock order database |
| `get_customer` | AgentCoreCustomerSupportTools | Mock customer database |
| `list_orders` | AgentCoreCustomerSupportTools | Mock order listing |
| `process_refund` | AgentCoreCustomerSupportTools | Mock refund processing |
| `knowledge_base` | Per-deployment KB Lambda | Bedrock Knowledge Base RetrieveAndGenerate |

The search/weather/customer tools use **zero external dependencies** -- only Python standard library. No Lambda layers, no custom runtimes, instant cold starts. The Knowledge Base tool uses boto3 (bundled in the Lambda runtime).

### How Tool Schemas Work

Each tool has an MCP-compliant schema registered in `GATEWAY_TOOL_SCHEMAS`:

```python
GATEWAY_TOOL_SCHEMAS = {
    'duckduckgo_search': {
        'name': 'duckduckgo_search',
        'description': 'Search the web using DuckDuckGo...',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'The search query'}
            },
            'required': ['query']
        }
    },
    # ... wikipedia_search, weather_api, web_page_fetcher
}
```

When deploying, only schemas for the user's selected tools are included:

```python
selected_schemas = [GATEWAY_TOOL_SCHEMAS[tid] for tid in gateway_tools if tid in GATEWAY_TOOL_SCHEMAS]
# This list goes into the gateway target's toolSchema.inlinePayload
```

## Agent Code Architecture

All generated agent code uses Strands Agents SDK with provider-aware model initialization:

1. **BedrockAgentCoreApp SDK** -- Provides the HTTP server for AgentCore Runtime protocol (`GET /ping` health check, `POST /invocations` for requests).
2. **Strands Agent with provider-aware model init** -- Based on the selected provider, the code generator imports the correct model class (e.g., `BedrockModel`, `OpenAIModel`, `AnthropicModel`, `GeminiModel`) and initializes it with the chosen model ID and any required API keys.
3. **Multi-agent orchestration** -- When a multi-agent pattern is selected (Graph, Swarm, or Workflow), the code generator creates multiple sub-agents and wires them together using `strands.multiagent`.
4. **Tool-calling loop** -- The Strands Agent handles the Converse API tool-use cycle automatically, executing tools and feeding results back until the model produces a final text response.

For gateway-enabled agents (Templates 2-5):
- Tools are discovered at startup via MCP `initialize` -> `notifications/initialized` -> `tools/list`
- OAuth2 tokens are acquired via `client_credentials` grant (Cognito)
- Gateway credentials are injected as environment variables by the runtime configure step (`COGNITO_*`)

For MCP Server Runtime agents (Template 5):
- Tools are embedded as Python functions directly in the agent code
- No Gateway, Lambda, or Cognito resources are created
- The agent uses the Strands tool-calling loop to route to embedded tool handlers

For MCP Server Gateway Target (Template 6):
- Two Runtimes: an Agent Runtime (HTTP) and an MCP Server Runtime (MCP protocol)
- The Agent connects to the MCP Server through the Gateway with OAuth2 authentication
- The MCP Server exposes tools via FastMCP that the Agent discovers at runtime

Provider-specific packages are determined at code generation time from `PROVIDER_PACKAGES` mapping (e.g., `openai` provider adds `strands-agents strands-agents-tools openai`). Dependencies are bundled into `code.zip` at deploy time from pre-built dependency bundles, avoiding any pip-install during the 30-second AgentCore init window.

## CloudFormation Export

Any template or free-form diagram can be exported as a self-contained CloudFormation stack. The export generates a downloadable zip containing everything an external user needs to deploy the agent without access to the platform.

### What's in the Download

| File | Purpose |
|------|---------|
| `template.yaml` | CloudFormation template with all AWS resources |
| `agent-code/agent.py` | Generated agent code |
| `cfn-provider.zip` | Custom Resource Lambda (merges agent code with dependency bundle at deploy time) |
| `tool-lambdas.zip` | Gateway tool Lambda implementations (if gateway tools are used) |
| `deploy.sh` | One-command deploy script (`./deploy.sh <stack-name> <region> <s3-bucket>`) |
| `teardown.sh` | One-command teardown script (`./teardown.sh <stack-name> <region>`) |
| `README.md` | Setup instructions and prerequisites |

### How It Works

1. **User clicks "Download CloudFormation"** in the deploy panel (or calls `POST /api/generate-cfn-template`)
2. `CfnTemplateGenerator.generate()` builds:
   - A CloudFormation template with AgentCore Runtime, Gateway, Cognito, Memory, Evaluation, and IAM resources as needed
   - A `Custom::AgentCodePackage` resource (backed by the cfn-provider Lambda) that downloads a pre-built dependency bundle from S3, merges the agent code into it, and uploads the final `code.zip` at deploy time
3. The download zip is returned to the browser
4. The external user runs `./deploy.sh my-agent us-east-1 my-s3-bucket` to deploy

### Prerequisites for External Users

- AWS CLI v2 configured with credentials
- An S3 bucket to host deployment artifacts
- Pre-built dependency bundle in S3: `agentcore-deps/base.zip` (for boto3 agents) or `agentcore-deps/strands-mcp.zip` (for Strands/MCP agents)

### Supported Patterns

The CFN generator supports both built-in templates (all 7) and free-form diagrams with any combination of:

| Component | CFN Resources Created |
|-----------|----------------------|
| Runtime only | Runtime, Endpoint, IAM Role, Custom Resource (code packager) |
| + Gateway | MCP Gateway, Gateway Targets, Tool Lambda, Cognito User Pool/Client/Domain/ResourceServer |
| + Memory | AgentCore Memory, Memory IAM Role |
| + Evaluation | Online Evaluation Config, Evaluation IAM Role |
| + Knowledge Base | Bedrock Knowledge Base, Data Source, KB IAM Role, KB Tool Lambda + Target |
| + Policy Engine | Policy Engine (attached to Gateway) |
| + MCP Server | Second Runtime (MCP protocol), MCP Server code, OAuth2 Credential Provider |

## Agent Registry — Roles & Approval

The registry turns a deployed agent into a reusable, governed blueprint others can discover and clone. It uses a **two-persona model driven entirely by Cognito groups** — no separate auth system.

### Personas

| Persona | Cognito group | Can do | Cannot do |
|---------|---------------|--------|-----------|
| **Developer** | `registry-developer` (or any signed-in user) | Publish (entry enters `pending`); view **approved** entries + their **own** (any status); clone approved/own; edit/delete their own | Approve or reject; see other users' pending entries |
| **Admin** | `registry-admin` (legacy `org-admin` also honored) | Everything a developer can, **plus**: see the pending-review queue, approve/reject submissions, delete any entry | — |

### Entry lifecycle

```
developer publishes ──▶ pending ──▶ (admin approves) ──▶ approved ──▶ visible + clonable org-wide
                           │
                           └──▶ (admin rejects, optional reason) ──▶ rejected
```

- New publishes start `pending` and are invisible to other developers until approved.
- A non-admin edit (`PUT`) of an approved entry resets it to `pending` (re-review). Admin edits preserve status.
- Backward-compatible: entries created before this feature (no `status` attribute) deserialize as `approved`, so nothing already published disappears.

### Authorization rules (enforced server-side)

- Admin status is read from the caller's `cognito:groups` JWT claim (`auth.is_registry_admin`); the frontend reads the same claim to show/hide the admin "Pending review" UI.
- **RBAC-role denial returns `403`** (e.g. a developer calling `approve`); **cross-tenant / not-visible returns `404`** (never disclosing existence). These are kept strictly distinct.
- Before attaching, the server reads the engine/entry back from the store — a defense-in-depth ground-truth check, not a client-supplied flag.

### Assigning personas

```bash
# Create the two Cognito groups (the CDK stack also defines them at deploy time)
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <POOL_ID> --username alice@example.com --group-name registry-admin
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <POOL_ID> --username bob@example.com   --group-name registry-developer
```

In the UI: developers click **Publish to Registry** in the Deploy panel after a deploy, and **Registry** in the component palette to browse and **Clone to Canvas**. Admins additionally see a **Pending review** tab with Approve / Reject actions.

## Deployment Templates

### Template 1: Web Search Agent (Beginner)
- Agent: BedrockAgentCoreApp + boto3 Converse API with tool-calling loop
- Tools: DuckDuckGo Search, Weather (Open-Meteo), Web Page Fetcher (embedded in agent code, no gateway)
- Components: Runtime only (6 CFN resources)

### Template 2: Strands Agent + Gateway (Intermediate)
- Agent: BedrockAgentCoreApp + Strands Agent + MCP Gateway tools
- Tools: DuckDuckGo, Weather, Web Page Fetcher -- discovered via MCP Gateway
- Auth: Cognito OAuth2 (client_credentials grant)
- Components: Runtime + Gateway + Identity (16 CFN resources)

### Template 3: Customer Support Blueprint (Intermediate)
- Agent: BedrockAgentCoreApp + boto3 Converse API + MCP Gateway tools
- Tools: Order lookup, customer search, order listing, refund processing -- custom Lambda behind Gateway
- Auth: Cognito OAuth2 (client_credentials grant)
- Components: Runtime + Gateway + Identity + Memory (18 CFN resources)

### Template 4: Customer Support Assistant (Advanced)
- Agent: BedrockAgentCoreApp + boto3 Converse API + MCP Gateway tools
- Tools: DuckDuckGo, Weather, Web Page Fetcher + order lookup, customer search -- Gateway tools + Memory + Online Evaluation
- Auth: Cognito OAuth2 (client_credentials grant)
- Components: Runtime + Gateway + Identity + Memory + Evaluation (20 CFN resources)

### Template 5: MCP Server Runtime (Intermediate)
- Agent: BedrockAgentCoreApp + boto3 Converse API with embedded tool handlers
- Tools: Weather (Open-Meteo), Web Search (DuckDuckGo), URL Fetcher -- all embedded as Python functions, no Gateway needed
- Components: Runtime only, protocol: MCP (6 CFN resources)
- Use case: Simplest path to a tool-using agent with zero external infrastructure

### Template 6: MCP Server Gateway Target (Advanced)
- Architecture: Agent Runtime → MCP Gateway → MCP Server Runtime (multi-runtime chain)
- Tools: hosted on the MCP Server Runtime, invoked through the Gateway as an MCP target
- Auth: Cognito OAuth2 between Agent and MCP Server via Gateway
- Components: 2 Runtimes + Gateway + Identity
- Use case: Decouple tool hosting from agent logic via MCP protocol
- Notes: the generated MCP server binds **port 8000** (the AgentCore MCP-runtime ingress contract) and uses a lean `mcp`-only dependency bundle so it cold-starts within the Gateway's ~30s tool-discovery probe; the gateway step pre-warms the runtime and retries `UpdateGatewayTarget`. Verified live end-to-end (target `READY`, agent calls the MCP tool through the gateway).

### Custom Deployment (free-form)
- Framework: Strands Agents (with provider selection from 13 providers)
- Multi-agent: Optional Graph, Swarm, or Workflow orchestration patterns
- Tools: Any combination of built-in tools + AI-generated custom tools + SaaS connectors, deployed as Gateway Targets
- Components: User-configured — hand-wire any valid combination of Runtime/Gateway/Memory/Identity/Policy/Guardrails/Observability/Connectors on the canvas (no `templateId`); the backend deploys whatever is wired

### Harness Mode (config-driven, no canvas required)
- Authoring: declare model + instructions + connected gateway/connectors + memory
- Deploy: `deploymentMode: "harness"` — runs `create_harness` instead of codegen/runtime steps, reusing the shared gateway/memory steps
- Test/Delete: identical surface to runtime mode (`/api/test-runtime`, `DELETE /api/runtime/{id}` → `invoke_harness` / `delete_harness`)

## Project Structure

```
.
+-- backend/
|   +-- pyproject.toml                    # Python deps and build config
|   +-- requirements-lambda.txt           # Lambda-specific dependencies (installed to lib/)
|   +-- src/app/
|   |   +-- main.py                       # FastAPI entry point (auto-selects storage backend)
|   |   +-- lambda_handler.py             # Mangum wrapper for Workflow Lambda
|   |   +-- deployment_handler.py         # Deployment Lambda (deploy, status, test, delete; manifest-driven teardown)
|   |   +-- stream_handler.py             # Lambda Function URL handler (RESPONSE_STREAM) for >30s test invokes; accepts the AWS_IAM SigV4 caller OR a Cognito JWT
|   |   +-- models/
|   |   |   +-- enums.py                  # Component types, frameworks, statuses
|   |   |   +-- components.py             # Pydantic models for all AgentCore components
|   |   |   +-- workflow.py               # Workflow, node, edge, validation models
|   |   |   +-- deployment_models.py      # DeploymentState, RuntimeConfig, IdentityConfig, CustomToolDefinition
|   |   |   +-- catalog_models.py         # Tool catalog and flow submission models
|   |   |   +-- tool_generation_models.py # AI Tool Generator request/response models
|   |   +-- routers/
|   |   |   +-- workflows.py              # Workflow CRUD + import/export (owner-scoped)
|   |   |   +-- flows.py                  # Flow CRUD (owner-scoped)
|   |   |   +-- observability.py          # Credential bootstrap + GET /platform-defaults
|   |   |   +-- versions.py               # Agent versioning + slots + rollback
|   |   |   +-- evaluations.py            # Online-eval config + scores + dashboard URL
|   |   |   +-- cost.py                   # Per-runtime token + cost rollups
|   |   |   +-- triggers.py               # cron/eventbridge/s3/webhook triggers
|   |   |   +-- registry.py               # Agent registry + two-persona approval (RBAC)
|   |   |   +-- prompts.py                # Prompt library (versions + promote + resolve)
|   |   |   +-- hitl.py                    # Human-in-the-loop approval queue
|   |   |   +-- connectors.py             # Pre-built SaaS connector catalog (read-only)
|   |   |   +-- workspaces.py             # Workflow sharing + workspace listing (ACL)
|   |   |   +-- git_sync.py               # GitOps: store PAT + pull workflow spec
|   |   +-- services/
|   |   |   +-- config.py                 # AppConfig -- SSM Parameter Store / env var loader
|   |   |   +-- auth.py                   # JWT sub + assert_owner tenant guard + is_registry_admin (Cognito-group RBAC)
|   |   |   +-- agent_versions_store.py   # Versions + RuntimeSlots store (DDB)
|   |   |   +-- registry_store.py         # Agent registry store (status/approval, DDB)
|   |   |   +-- prompt_library_store.py   # Prompt library store (DDB)
|   |   |   +-- hitl_store.py             # HITL request store (DDB, 24h TTL)
|   |   |   +-- trigger_store.py          # Trigger store (DDB)
|   |   |   +-- cost_tracking.py          # gen_ai.usage parsing + Bedrock price table
|   |   |   +-- observability_dashboard.py # Per-runtime CloudWatch dashboard builder
|   |   |   +-- agent_generator.py        # NL description → validated canvas spec (Bedrock tool-use)
|   |   |   +-- python_exporter.py        # Standalone Python project "eject" bundle
|   |   |   +-- a2a_codegen.py            # A2A agent-card + SSRF-guarded call_a2a_peer tool
|   |   |   +-- agentic_rag_codegen.py    # multi-hop / hybrid / reranked retrieval tools
|   |   |   +-- per_agent_identity.py     # Per-agent least-privilege IAM role builder
|   |   |   +-- connectors_catalog.py     # Connector definitions (tool + credential schema)
|   |   |   +-- workspace_acl.py          # Workflow share/ACL logic
|   |   |   +-- git_sync.py               # Git PAT (Secrets Manager) + SSRF-guarded repo fetch
|   |   |   +-- guardrail_builders.py     # Contextual grounding / regex / injection-defense configs
|   |   |   +-- _otel_platform.py         # Module-load OTel SDK bootstrap (imported first by every Lambda handler)
|   |   |   +-- observability.py          # build_otel_env_vars + get_platform_observability_defaults
|   |   |   +-- dynamodb_storage.py       # DynamoDB workflow storage adapter
|   |   |   +-- flow_storage.py           # DynamoDB flow storage adapter
|   |   |   +-- deployment_state_store.py # DynamoDB deployment state adapter
|   |   |   +-- deployment.py             # End-to-end deploy orchestration (direct + SFN paths)
|   |   |   +-- code_generator.py         # Agent code generation (Strands Agents, 13 providers, multi-agent)
|   |   |   +-- runtime_deployer.py       # AgentCore runtime configure/launch/destroy + transient-error retry
|   |   |   +-- harness_deployer.py       # AgentCore Harness lifecycle (create/get/invoke/destroy) + gateway outbound-auth wiring
|   |   |   +-- connectors.py             # SaaS connector catalog (Jira/Asana/Slack/GitHub/Salesforce + generic OpenAPI)
|   |   |   +-- policy_promoter.py         # Converge a fail-closed ENFORCE Cedar permit to ACTIVE in place (update_policy) on invoke/status touchpoints (post-convergence)
|   |   |   +-- naming.py                 # Shared AgentCore resource-name sanitizer (underscore vs hyphen styles)
|   |   |   +-- gateway_deployer.py       # Gateway deploy, Cognito OAuth, JWT auth, custom tool + connector (OpenAPI) targets, cleanup
|   |   |   +-- tool_catalog_store.py     # DynamoDB tool catalog storage
|   |   |   +-- flow_submission_store.py  # DynamoDB flow submission storage
|   |   |   +-- tool_tester.py            # Tool testing utilities
|   |   |   +-- iam_manager.py            # Scoped IAM role management for tools
|   |   |   +-- tool_generator.py         # AI Tool Generator -- Claude Sonnet on Bedrock for Lambda code generation
|   |   |   +-- cfn_template_generator.py # CloudFormation template generator (templates + free-form diagrams → CFN stacks)
|   |   |   +-- cfn_provider/             # Custom Resource Lambda for CFN stacks (code packaging + OAuth2 credential provider)
|   |   |   |   +-- handler.py            # CloudFormation Custom Resource handler
|   |   |   |   +-- cfn_response.py       # CFN response helper
|   |   |   +-- validation.py             # Connection compatibility + field validation
|   |   |   +-- storage.py                # In-memory workflow storage (local dev fallback)
|   |   +-- step_handlers/                # Step Functions step Lambda handlers
|   |       +-- validate_step.py          # Workflow validation
|   |       +-- codegen_step.py           # Code generation + dependency bundle merge
|   |       +-- iam_step.py               # IAM role creation
|   |       +-- gateway_step.py           # Gateway + Cognito + Lambda deployment
|   |       +-- knowledge_base_step.py    # Bedrock Knowledge Base creation + data source + sync
|   |       +-- mcp_server_step.py        # MCP Server Runtime deployment
|   |       +-- memory_step.py            # AgentCore Memory creation
|   |       +-- evaluation_step.py        # AgentCore Evaluation configuration
|   |       +-- policy_step.py            # AgentCore Policy Engine creation
|   |       +-- runtime_configure_step.py # AgentCore runtime creation (with gateway env vars)
|   |       +-- runtime_launch_step.py    # Runtime launch + readiness polling
|   |       +-- harness_step.py           # AgentCore Harness create + wait (runs instead of codegen/runtime in harness mode)
|   |       +-- auth_step.py              # JWT auth configuration on runtime
|   |       +-- status_update_step.py     # Final status + gateway_result write to DynamoDB
|   +-- tests/
|       +-- test_*_properties.py          # Property-based tests (Hypothesis)
|       +-- integration/                  # Integration tests (real AWS API calls)
+-- frontend/
|   +-- src/
|   |   +-- components/
|   |   |   +-- ai/                       # ToolGeneratorPanel + AgentGeneratorPanel (NL → canvas)
|   |   |   +-- auth/                      # AuthWrapper (Amplify Authenticator) + cinematic login hero
|   |   |   +-- canvas/                   # WorkflowCanvas (React Flow)
|   |   |   +-- deploy/                   # DeployPanel + tabs: VersionsList, EvaluationResultsPanel,
|   |   |   |                             #   ObservabilityPanel, CostPanel, TriggersPanel; Publish-to-Registry; authoring-mode toggle
|   |   |   +-- harness/                  # Harness authoring form (model/instructions/memory/tools) for deploymentMode=harness
|   |   |   +-- hero/                      # MotionSites-style hero (animated gradient, glass badge, corner marks) for login/empty-state
|   |   |   +-- modals/                   # Runtime/Gateway/Identity/KB/Policy/Guardrails/Memory/Observability/
|   |   |   |                             #   Evaluation/A2A config modals + Registry, PromptLibrary, Hitl inbox,
|   |   |   |                             #   ConnectorPicker + ConnectorConfigModal (SaaS connector auth/spec)
|   |   |   |   +-- kb/                   # Knowledge Base sub-components (DataSourceFields, VectorStoreFields, AdvancedFields)
|   |   |   +-- nodes/                    # AgentCoreNode component
|   |   |   +-- palette/                  # ComponentPalette (drag source + AI Tool Generator + Registry + Connectors)
|   |   |   +-- templates/                # TemplateGallery
|   |   +-- auth/useIsRegistryAdmin.ts    # Reads cognito:groups from the ID token for registry RBAC
|   |   +-- data/templates.ts             # Template definitions
|   |   +-- store/workflowStore.ts        # Zustand state management
|   |   +-- services/api.ts               # Backend API client (configurable base URL)
|   |   +-- types/                        # TypeScript type definitions
|   |   +-- utils/                        # Utility functions + tests
|   +-- package.json
+-- infra/                                # AWS CDK infrastructure (Python)
|   +-- app.py                            # CDK app entry point
|   +-- cdk.json                          # CDK configuration + context defaults
|   +-- requirements.txt                  # CDK dependencies
|   +-- stacks/
|   |   +-- platform_stack.py             # Single stack: API GW, Lambda, Step Functions, DynamoDB, S3, CloudFront
|   +-- tests/
|       +-- test_platform_stack.py        # CDK template assertion tests
+-- scripts/
|   +-- deploy.sh                         # One-command serverless deploy to AWS
|   +-- cleanup.sh                        # One-command teardown of all AWS resources
|   +-- install-lambda-deps.sh            # Install Lambda deps for Linux x86_64 into backend/lib/
|   +-- install-agentcore-deps.sh         # Build AgentCore dependency bundles into backend/agentcore-deps/
+-- docs/
|   +-- architecture.drawio               # Editable architecture diagram (draw.io)
|   +-- architecture.jpg                  # Architecture diagram image
+-- .gitignore
+-- .pre-commit-config.yaml            # Security scanning and code quality hooks
+-- .secrets.baseline                   # detect-secrets baseline (false positive allowlist)
+-- README.md
```

## API Endpoints

### Workflow Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/api/workflows` | Create workflow |
| `GET` | `/api/workflows/{id}` | Get workflow |
| `PUT` | `/api/workflows/{id}` | Update workflow |
| `DELETE` | `/api/workflows/{id}` | Delete workflow |
| `POST` | `/api/workflows/{id}/validate` | Validate workflow |
| `POST` | `/api/workflows/import` | Import workflow JSON |
| `GET` | `/api/workflows/{id}/export` | Export workflow JSON |

### Deployment

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/deploy` | Start deployment (returns 202 with deployment_id and execution_arn) |
| `GET` | `/api/deploy/{deployment_id}` | Get deployment status from DynamoDB |
| `POST` | `/api/test-runtime` | Test a deployed agent with a prompt (supports session_id for conversation context) |
| `DELETE` | `/api/runtime/{id}` | Delete runtime + gateway + Cognito + Lambda (full cleanup) |
| `POST` | `/api/generate-tool` | AI Tool Generator -- generate Lambda code from natural language via Claude Sonnet |
| `POST` | `/api/generate-cfn-template` | Generate downloadable CloudFormation stack (template YAML + deploy scripts + code artifacts) |

### Flows

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/flows` | Create flow |
| `GET` | `/api/flows` | List caller's flows |
| `GET` | `/api/flows/{flow_id}` | Get flow |
| `PUT` | `/api/flows/{flow_id}` | Update flow |
| `DELETE` | `/api/flows/{flow_id}` | Delete flow |

### Observability

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/observability/platform-defaults` | Returns `{enabled, endpoint, sample_rate}` so the UI can render the Observability node read-only when platform OTEL is configured. Never returns the secret ARN. |
| `POST` | `/api/observability/credentials` | Stores OTLP auth credentials in Secrets Manager and returns the secret ARN. |

### Versioning & Slots

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/runtimes/{name}/versions` | List a runtime's version history (newest first) |
| `GET` | `/api/runtimes/{name}/slots` | Get the production / staging slot pointers |
| `POST` | `/api/runtimes/{name}/rollback` | Promote the previous production version back into production |

### Evaluation, Cost & Observability (runtime-scoped)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/runtimes/{name}/evaluation-config` | Registered Online Evaluation config (evaluator IDs + sampling rate) |
| `GET` | `/api/runtimes/{name}/evaluations?hours=` | Per-evaluator score time-series from CloudWatch Logs Insights |
| `GET` | `/api/runtimes/{name}/dashboard-url` | Deep link to the auto-generated CloudWatch dashboard |
| `GET` | `/api/runtimes/{name}/cost?from=&to=` | Token + estimated-cost rollup by model for the window |

### Triggers (runtime-scoped)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/runtimes/{name}/triggers` | Register a `cron` / `eventbridge` / `s3` / `webhook` trigger (target ARN derived server-side; created as `registered`) |
| `GET` | `/api/runtimes/{name}/triggers` | List the runtime's triggers |
| `DELETE` | `/api/runtimes/{name}/triggers/{id}` | Delete a trigger |

### Agent Registry

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/registry` | Publish an agent blueprint (enters `pending` review) |
| `GET` | `/api/registry?q=&tag=&scope=all\|mine\|public\|pending` | Search/list visible entries (admins can list `pending`) |
| `GET` | `/api/registry/{slug}` | Get one entry (visibility/approval-checked, 404 if not visible) |
| `POST` | `/api/registry/{slug}/clone` | Clone an approved/own entry's canvas to the caller |
| `PUT` | `/api/registry/{slug}` | Update metadata (owner only; non-admin edit resets to `pending`) |
| `DELETE` | `/api/registry/{slug}` | Delete (owner **or** `registry-admin`) |
| `POST` | `/api/registry/{slug}/approve` | **Admin only** — approve a pending entry (403 otherwise) |
| `POST` | `/api/registry/{slug}/reject` | **Admin only** — reject with optional reason (403 otherwise) |

### Prompt Library

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/prompts` | Create a prompt (seeds v1) |
| `GET` | `/api/prompts` | List visible prompts |
| `GET` / `PUT` / `DELETE` | `/api/prompts/{name}` | Get / update / delete a prompt |
| `POST` | `/api/prompts/{name}/versions` | Append a new version |
| `POST` | `/api/prompts/{name}/promote/{version_id}` | Pin the default version |
| `GET` | `/api/prompts/{name}/resolve?version=` | Resolve `{version_id, body}` (used at codegen) |

### HITL, Connectors, Workspaces & GitOps

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/hitl/pending` | Caller's pending human-approval queue |
| `POST` | `/api/hitl/{request_id}/decision` | Approve / reject a pending approval |
| `GET` | `/api/connectors` | List pre-built SaaS connector definitions |
| `GET` | `/api/connectors/{id}` | Connector tool + credential schema |
| `POST` | `/api/workflows/{id}/share` | Share a workflow (viewer/editor; owner only) |
| `DELETE` | `/api/workflows/{id}/share/{sub}` | Revoke a share |
| `GET` | `/api/workspaces` | List workspace-visible workflows with effective role |
| `POST` | `/api/workflows/{id}/git-token` | Store a Git PAT (owner-scoped Secrets Manager) |
| `POST` | `/api/workflows/{id}/git-sync` | Pull a workflow spec from Git (SSRF-guarded) |

### NL Agent Generation & Code Export

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/generate-canvas` | NL description → validated canvas spec (Bedrock tool-use, clarify → generate) |
| `POST` | `/api/export-python` | Download a standalone runnable Python agent project (presigned S3 zip) |

## Running Tests

### Unit and Property-Based Tests

```bash
# Backend (property-based tests with Hypothesis)
cd backend
pip install -e ".[dev]"
pytest
```

Property-based tests use Hypothesis with `@settings(max_examples=100)` to verify correctness properties across randomly generated inputs (workflow CRUD round-trips, serialization, validation consistency, IAM scoping, etc.).

### CDK Infrastructure Tests

```bash
cd infra
pip install -r requirements.txt
pytest tests/ -v
```

Verifies the synthesized CloudFormation template contains expected serverless resources (API Gateway, Lambda, Step Functions, DynamoDB) and does NOT contain removed resources (VPC, ECS, ALB, ECR, CodeBuild).

### Integration Tests

Integration tests perform real AWS API calls with zero mocking. They require:
- Valid AWS credentials with permissions for API Gateway, Lambda, Step Functions, DynamoDB, AgentCore, IAM, Cognito
- A deployed stack (run `./scripts/deploy.sh` first)
- Environment variables: `API_GATEWAY_URL` and `AWS_REGION`

```bash
cd backend

# Set required environment variables
export API_GATEWAY_URL="https://XXXXXXXXXX.execute-api.us-east-1.amazonaws.com"
export AWS_REGION="us-east-1"

# Run integration tests only
pytest -m integration -v

# Run a specific integration test
pytest -m integration tests/integration/test_deployment_lifecycle.py -v
pytest -m integration tests/integration/test_template_deployments.py -v
```

Integration tests deploy each of the 7 built-in templates, invoke the deployed runtimes, verify responses, and clean up all resources.

### Frontend Tests

```bash
cd frontend
npm install
npm test
```

## Testing a Deployment

### Deploy a Custom Agent with Dynamic Tools

1. Open the CloudFront URL and click **"Custom"** to create a new deployment
2. Configure the Runtime (name, model, system prompt)
3. Connect a **Gateway** node to the Runtime
4. Select tools: e.g., `duckduckgo_search` + `wikipedia_search`
5. Click **Deploy**

The backend will:
- Start a Step Functions execution
- Validate the workflow
- Generate Strands Agent code (provider-aware model init + multi-agent) and merge with dependency bundle
- Create a scoped IAM role
- Deploy a Lambda with all tool implementations behind the Gateway
- Create Cognito OAuth2 and configure Gateway JWT authorizer
- Configure and launch the AgentCore Runtime (with gateway env vars)
- Configure JWT auth on runtime (if gateway present)
- Write final status + gateway_result to DynamoDB

### Test the Agent

After deployment, use the test panel in the UI:
- Ask "What is the weather in Chicago?" -- triggers `get_weather` via Gateway
- Ask "Search for the latest news about AWS" -- triggers `duckduckgo_search` via Gateway
- Ask "Tell me about Python programming" -- triggers `wikipedia_search` via Gateway

### Clean Up Deployed Agents

Delete from the UI, or:

```bash
curl -X DELETE https://<cloudfront-url>/api/runtime/<runtime-id>
```

This deletes: Runtime, Gateway, Gateway Targets, Cognito User Pool, custom tool Lambdas (AI-generated), and the tools Lambda function.

## Connection Compatibility

```
Runtime --> Gateway, Memory, Knowledge Base, Code Interpreter, Browser, Observability, Identity, Evaluation, Policy
Gateway --> Runtime, Identity, Policy, Knowledge Base
Memory --> Runtime
Code Interpreter --> Runtime
Browser --> Runtime
Observability --> Runtime
Identity --> Runtime, Gateway
Evaluation --> Runtime
Policy --> Runtime, Gateway
```

## Local Development

For contributors who want to run the platform locally without deploying to AWS. The backend falls back to in-memory storage when `DYNAMODB_TABLE_NAME` is not set **and** the process is not running inside Lambda (detected via `AWS_LAMBDA_FUNCTION_NAME`). Inside Lambda the missing table env var raises `RuntimeError` at module load, so a misconfigured deploy fails to initialize rather than silently dropping writes.

```bash
# Backend
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,deploy]"
cp .env.example .env
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Frontend
cd frontend
npm install
cp .env.example .env
npm run dev
```

The UI opens at `http://localhost:5173`. The backend API runs at `http://localhost:8000`.

Note: Local mode uses in-memory storage (workflows are lost on restart) and requires AWS credentials for agent deployment features.

## Lambda Dependency Packaging

Lambda functions run on Amazon Linux (x86_64), so native Python packages like `pydantic-core` must be compiled for that platform -- not your local macOS/arm64. Dependencies are pre-installed into `backend/lib/` and bundled by CDK alongside the source code.

AgentCore Runtime agents run on aarch64 (ARM) with a different set of dependencies. Pre-built bundles (`base.zip` with `bedrock-agentcore` + `boto3`, and `strands-mcp.zip` with additional Strands + MCP packages) are created by `install-agentcore-deps.sh` and uploaded to S3 during deployment. The codegen step merges these bundles into each agent's `code.zip`.

The deploy script (`scripts/deploy.sh`) handles both installs automatically. To install manually:

```bash
# Lambda dependencies (x86_64)
./scripts/install-lambda-deps.sh

# AgentCore dependency bundles (aarch64)
./scripts/install-agentcore-deps.sh
```

All Lambda functions include `PYTHONPATH=/var/task/src:/var/task:/var/task/lib` so the bundled packages are found at runtime. Both `backend/lib/` and `backend/agentcore-deps/` are in `.gitignore` -- they are build artifacts, not source code.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 19, @xyflow/react 12, Zustand 5, Tailwind CSS 4, Vite |
| Backend | FastAPI, Mangum, Pydantic 2, boto3 |
| Agent Framework | Strands Agents SDK (strands-agents, strands-agents-tools) |
| Agent Runtime | BedrockAgentCoreApp (bedrock-agentcore) |
| Model Providers | Bedrock (default), OpenAI, Anthropic, Gemini, Mistral, Ollama, Groq, DeepSeek, Together, LiteLLM, SageMaker, Writer, LlamaAPI |
| Multi-Agent | strands.multiagent (Graph, Swarm, Workflow patterns) |
| Orchestration | AWS Step Functions (Standard Workflows) |
| Testing | Pytest + Hypothesis (backend properties), Pytest + real AWS (integration), Vitest + fast-check (frontend), CDK assertions (infra) |
| Deployment Target | AWS Bedrock AgentCore (Runtime, Gateway, Knowledge Base, Memory, Evaluation, Policy, Browser, Identity, Observability) |
| Platform Infrastructure | AWS CDK (Python), API Gateway HTTP API, Lambda, Step Functions, DynamoDB, S3, CloudFront, SSM, Cognito |

## Infrastructure Pricing

This section covers the cost of running the **platform infrastructure** itself (API Gateway, Lambda, Step Functions, DynamoDB, S3, CloudFront, etc.). It does **not** include AgentCore deployment costs (Runtime compute, Gateway invocations, Memory storage, Bedrock model inference) which are billed separately by AWS Bedrock AgentCore.

All prices are for **us-east-1 (N. Virginia)** as of 2025. See the linked AWS pricing pages for the latest rates.

### Per-Service Breakdown

| Service | Pricing Model | Unit Price | AWS Free Tier |
|---------|--------------|------------|---------------|
| [API Gateway HTTP API](https://aws.amazon.com/api-gateway/pricing/) | Per-request | $1.00 / million requests | 1M requests/mo (12 months) |
| [Lambda](https://aws.amazon.com/lambda/pricing/) | Per-request + per-GB-second | $0.20 / million requests + $0.0000166667 / GB-second | 1M requests + 400,000 GB-s/mo (always free) |
| [Step Functions](https://aws.amazon.com/step-functions/pricing/) | Per state transition | $0.000025 / transition ($25 / million) | 4,000 transitions/mo (always free) |
| [DynamoDB On-Demand](https://aws.amazon.com/dynamodb/pricing/on-demand/) | Per-request + per-GB storage | $1.25 / million writes, $0.25 / million reads, $0.25 / GB-mo | 25 GB storage (always free) |
| [S3 Standard](https://aws.amazon.com/s3/pricing/) | Per-GB storage + per-request | $0.023 / GB-mo, $0.005 / 1K PUT, $0.0004 / 1K GET | 5 GB + 20K GET + 2K PUT/mo (12 months) |
| [CloudFront](https://aws.amazon.com/cloudfront/pricing/) | Per-request + per-GB transfer | $1.00 / million HTTPS requests, $0.085 / GB transfer | 1 TB transfer + 10M requests/mo (always free) |
| [CloudWatch Logs](https://aws.amazon.com/cloudwatch/pricing/) | Per-GB ingested + stored | $0.50 / GB ingestion, $0.03 / GB-mo storage | 5 GB ingestion + 5 GB storage/mo (always free) |
| [SSM Parameter Store](https://aws.amazon.com/systems-manager/pricing/) | Standard parameters | Free | Up to 10,000 standard parameters |
| [IAM](https://aws.amazon.com/iam/) | All resources | Free | Unlimited |

### Monthly Cost Estimates

Estimates assume all agent deployment activity flows through Step Functions (the primary path from the UI). Each deployment triggers ~16 state transitions and ~16 Lambda invocations.

| Service | Low Usage | Moderate Usage |
|---------|-----------|----------------|
| | *~100 deploys, ~1K API requests/mo* | *~1,000 deploys, ~10K API requests/mo* |
| API Gateway HTTP API | $0.00 | $0.01 |
| Lambda (3 functions + 11 step handlers) | $0.00 | $0.00 |
| Step Functions | $0.00 | $0.30 |
| DynamoDB (2 tables, on-demand) | $0.00 | $0.01 |
| S3 (2 buckets, ~0.6–2 GB) | $0.02 | $0.07 |
| CloudFront (1 distribution) | $0.00 | $0.00 |
| CloudWatch Logs | $0.00 | $0.00 |
| SSM Parameter Store (5 params) | $0.00 | $0.00 |
| IAM | $0.00 | $0.00 |
| **Total** | **~$0.02/mo** | **~$0.39/mo** |

> With the AWS Free Tier (first 12 months or always-free tiers), this platform costs effectively **$0.02/month** at low usage and under **$0.40/month** at moderate usage. Even without any free tier, moderate usage stays under **$2/month**. The serverless, pay-per-request architecture means you pay nothing when the platform is idle.

### What's NOT Included

The table above covers **platform infrastructure only**. The following are billed separately by AWS and depend on your agent usage:

- **Amazon Bedrock model inference** — per-token pricing varies by model (e.g., Claude Sonnet, Nova)
- **AgentCore Runtime compute** — billed per-second while the runtime is active
- **AgentCore Gateway invocations** — per-request to the MCP gateway
- **AgentCore Memory storage** — per-GB for conversation memory
- **Bedrock Knowledge Base** — per-query for RetrieveAndGenerate, vector store costs (OpenSearch Serverless / RDS Aurora), S3 storage for source documents
- **AgentCore Code Interpreter / Browser** — per-session usage
- **Cognito** — first 50,000 MAUs are free; OAuth2 machine-to-machine auth adds $0.0065/token request beyond 250/mo
- **Custom tool Lambda functions** — same Lambda pricing as above, billed per invocation

Refer to the [Amazon Bedrock AgentCore pricing page](https://aws.amazon.com/bedrock/agentcore/pricing/) for current rates.


- **Additional tool types** -- Add tools like `code_executor`, `slack_notifier`, `s3_file_reader` to the dynamic tool registry.
- **Tool composition** -- Allow chaining tools as a single Gateway target with an orchestration Lambda.
- **Multi-target Gateway** -- Deploy different tools as separate Lambda targets for isolation and independent scaling.
- **Container deployments** -- Support deploying agents as containers.
- **Real-time logs** -- Stream CloudWatch logs from deployed agents into the test panel UI.
- **Versioned deployments** -- Deployment history per workflow with rollback support.
- **Collaborative editing** -- WebSocket-based real-time collaboration on the canvas.
- **Custom domain** -- Route 53 + ACM certificate support for custom domain names on CloudFront.
- **CI/CD pipeline** -- Automate deployments via CodePipeline or GitHub Actions on push to main.
- **Tool marketplace** -- Share and discover AI-generated tools across teams.
- **Multi-turn tool refinement** -- Iteratively refine AI-generated tools with conversation context in the Tool Generator.

## License

MIT
