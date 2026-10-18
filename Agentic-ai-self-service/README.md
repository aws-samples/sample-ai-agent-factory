# AgentCore Visual Workflow Platform

[![CI](https://github.com/aws-samples/sample-agentcore-lowcode-nocode/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/aws-samples/sample-agentcore-lowcode-nocode/actions/workflows/ci.yml)

A visual workflow builder for **AWS Bedrock AgentCore** that lets you design, configure, and deploy AI agents through a drag-and-drop canvas interface. Inspired by n8n's node-based editor, built for AWS Bedrock AgentCore. Deployed to AWS with API Gateway, Lambda, Step Functions, DynamoDB, and CloudFront — fully serverless, pay-per-request.

![Visual canvas — a customer support agent wired to Gateway, Identity, Memory, and Observability](docs/images/canvas.png)

<details>
<summary>More screenshots</summary>

**Template gallery** — six one-click starting points from beginner to advanced:

![Template gallery](docs/images/templates.png)

</details>

## Architecture

![Architecture](docs/architecture.jpg)

> The editable diagram source is at [`docs/architecture.drawio`](docs/architecture.drawio). Open it in [draw.io](https://app.diagrams.net/) to view or edit.

## Key Features

- **Visual Canvas** — Drag-and-drop AgentCore components (Runtime, Gateway, Memory, Knowledge Base, Browser, Identity, Observability, Policy, Connectors) and wire them together with real-time validation.
- **Two authoring paths, one deploy pipeline** — Build agents on the canvas (code-generated AgentCore **Runtime**) or as a config-driven **AgentCore Harness** (`deploymentMode: "runtime" | "harness"`); both share Gateway, Memory, connector, test, and teardown surfaces in development. Production admission requires the Runtime path because current Harness hooks cannot redact model output or retract a completed unsafe tool result.
- **Real SaaS connectors** — Jira, Asana, Slack, GitHub, Salesforce, or any OpenAPI spec as Gateway targets with API-key or OAuth2 outbound auth; credentials live only in Secrets Manager.
- **External browser delivery** — A versioned, dependency-free `AgentCoreClient` and `<agentcore-chat>` Web Component ship from CloudFront with TypeScript declarations. Hosts provide a request-local Cognito access-token callback; the client invokes only the owner-checked production alias, never retries POSTs, stores no token, and renders model output as inert text. See [Embeddable Chat](docs/EMBEDDABLE_CHAT.md).
- **Template Gallery + CloudFormation Export** — Six one-click templates, plus a downloadable self-contained CloudFormation bundle (`template.yaml`, `deploy.sh`, `teardown.sh`, `build-dependency-bundle.sh`, code artifacts, README) that an external recipient deploys with no access to the platform — `deploy.sh` builds and stages the dependency bundle itself. Five custom resource types, all served by the bundled provider code, cover what no pin-capable native `AWS::BedrockAgentCore::*` type does yet; managed Web Search uses a dedicated provider function so update rollback cannot restore older shared-provider code before deleting its target. Data-holding resources carry configurable `DeletionPolicy`/`UpdateReplacePolicy` (retained by default); a LiteLLM-provider gateway exports as a direct proxy connection whose virtual key is passed as a Secrets Manager ARN, never as a parameter value; and the stack wraps cleanly in Terraform's `aws_cloudformation_stack` via `template_url`. See [CloudFormation Export](docs/DEPLOYMENT_INTERNALS.md#cloudformation-export).
- **Shared-account-safe gallery defaults** — An untouched gallery Gateway name is paired server-side with the ownership-fenced Runtime name, while explicit custom Gateway names are preserved. Live tool-wiring checks use an isolated customer session per tool, accept only direct or syntactically valid Gateway-qualified receipt names with exact successful argument digests, and keep the Memory proof as a separate same-session customer-support journey.
- **Read-after-write release control** — Runtime slot reads are strongly consistent, so a deployment reported as succeeded cannot transiently return `404` while its newly written staging pointer is still converging.
- **AI generation** — Describe a tool or a whole agent in natural language; Claude on Bedrock generates a deployable Lambda tool or a validated canvas spec.
- **Multi-target gateways** — One gateway can carry multiple targets of different families at once (Lambda tools, external MCP servers from a curated catalog or a custom endpoint, OpenAPI specs, Smithy models), each with the right outbound auth.
- **Dynamic Gateway tool pipeline** — Managed search deploys as a pinned AWS connector target; selected Wikipedia, weather, fetch, and demo tools deploy behind scoped Lambda targets. Agents discover both through the MCP Gateway with Cognito OAuth2.
- **Bring your own LiteLLM** — Point an agent at a LiteLLM MCP proxy *instead of* AgentCore Gateway, or carry it *inside* one as an MCP target, and/or make a LiteLLM proxy the authoritative agent catalog. All opt-in and additive; the AgentCore Gateway and the built-in registry remain the defaults. See [Bring your own LiteLLM](#bring-your-own-litellm).
- **13 model providers & multi-agent patterns** — Bedrock (default), OpenAI, Anthropic, Gemini, Mistral, Ollama, Groq, DeepSeek, Together, LiteLLM, SageMaker, Writer, LlamaAPI; Graph / Swarm / Workflow orchestration via Strands Agents SDK.
- **Knowledge Base (RAG)** — 5 data source types, 3 vector stores (S3 Vectors, OpenSearch Serverless auto-provisioned, Aurora PostgreSQL), configurable parsing/chunking, plus agentic retrieval strategies. Customer-owned AWS resources require an explicit `AgentCoreFlowsAccess=allow` opt-in before the platform can grant a Knowledge Base role access.
- **Enterprise governance** — Scope-based RBAC/ABAC, Cedar policy enforcement, agent registry with approval workflow, versioning with AgentCore batch-evaluation-gated production promotion and emergency rollback, cost budgets, audit analytics, HITL approvals, VPC-egress runtimes, OIDC federation. See [Enterprise Capabilities](docs/ENTERPRISE_CAPABILITIES.md).
- **Manifest-driven teardown with live ownership proof** — Every deploy records the sub-resources it creates; delete re-validates live ownership, confirms asynchronous removal, and reports retained or failed resources instead of claiming they were removed.

## Prerequisites

- **AWS CLI** v2 — configured with credentials for the target account (`aws configure`)
- **Node.js** 20+ (the version CI runs on)
- **Python** 3.12+ (CI runs on 3.13)
- **Any AWS region** — `us-east-1` is the default; see [Deploying to another region](#deploying-to-another-region) for the two things that differ elsewhere.

No Docker installation required. CDK is invoked via `npx` (no global install needed).

## Quickstart

```bash
# Minimal deploy (dev environment, us-east-1)
COGNITO_USERS="user@example.com" ./scripts/deploy.sh

# Specific environment
COGNITO_USERS="user@example.com" ENVIRONMENT_NAME=prod ./scripts/deploy.sh

# Another region (Frankfurt)
COGNITO_USERS="user@example.com" AWS_REGION=eu-central-1 ./scripts/deploy.sh
```

The deploy script validates prerequisites and AWS credentials, installs backend/Lambda Python dependencies, builds the AgentCore dependency bundles, runs `cdk deploy` via `npx` (API Gateway, Lambda, Step Functions, DynamoDB, S3, CloudFront), then builds and uploads the frontend and prints the URLs. Lambda code is packaged automatically by CDK — no Docker build or ECR push required. A first-time deploy takes roughly 15–20 minutes.

The CDK safety gate strictly synthesizes once, records the template and every asset
payload, diffs that same assembly, and re-verifies its receipts immediately before
deployment. A nonzero `cdk deploy` status is explicitly returned even when a wrapper
calls the helper from a shell `if` condition; the script never prints success or
continues to frontend publication after a failed or rolled-back stack update.

Fresh deployments also verify that a missing stack is genuinely absent before deciding
there are no Cognito users to carry forward. Both AWS CLI v2 missing-stack formats are
supported, including the current `aws: [ERROR]:` prefix; every nonexact error still
fails closed before synthesis so an unreadable existing stack cannot lose users.

Dependency builds are a release gate, not a best-effort optimization. The AgentCore
bundle lock covers every distribution across the baseline and all provider deltas;
the packager rejects an absent or mismatched pin and writes fixed-metadata, sorted,
path-neutral ZIPs. Lambda dependencies are compiled from `requirements-lambda.in`
into a target-specific `requirements-lambda.txt` with hashes, installed with
`--require-hashes`, and stripped of path-dependent console scripts/RECORD files.
The source distribution explicitly excludes the generated `backend/lib/` Lambda tree
while retaining the locked Runtime ZIPs, so its contents do not depend on build order.
Two builds of one commit must therefore produce identical Runtime ZIPs, Lambda tree,
wheel, source distribution, CDK template, and asset manifest.

Upgrade dependencies deliberately: update the direct input or rebuild the complete
AgentCore graph, regenerate the corresponding lock, then run bundle integrity/import,
clean-clone, strict synth, and live AgentCore invocation gates. In particular,
`websockets==16.1.1` and `tenacity==9.1.2` are the shared versions accepted by the
Strands baseline and `google-genai`; an incompatible update fails before CDK.

To also export OTLP traces from every platform Lambda and deployed agent to a backend like Langfuse, see the platform-level OTEL deploy mode in [Observability](docs/OBSERVABILITY.md).

### Deploying to another region

Set `AWS_REGION`. Everything is derived from it — Bedrock cross-region inference
profiles, Lambda regions, the frontend's `VITE_AWS_REGION`, and the generated
agent code. Two things genuinely differ outside `us-east-1`, both handled
automatically:

| | `us-east-1` | Any other region |
|---|---|---|
| **WAF** | One `CLOUDFRONT`-scoped WebACL on the CloudFront distribution | The same rule set as a `REGIONAL` WebACL on the Cognito user pool. The distribution runs without an edge ACL — AWS accepts only `CLOUDFRONT`-scoped ACLs there and creates those exclusively in `us-east-1`. Pass `CLOUDFRONT_WEB_ACL_ARN=arn:...` to also attach one you created in `us-east-1`. |
| **Account-global names** | Unqualified (unchanged) | Suffixed with the region, so two regions can coexist in one account |

Because `us-east-1` keeps its unqualified names, adding a second region **does not
rename or replace anything in an existing `us-east-1` deployment**.

One region-specific caveat worth knowing before you pick a region: Bedrock's
cross-region inference prefixes are `us.`, `eu.` and `apac.`, and the `apac.`
family covers only the older Claude models. In APAC, current-generation models
are published under *country* prefixes (`jp.` in `ap-northeast-1`, `au.` in
`ap-southeast-2`) or as `global.`, so an APAC deployment may need its model ID set
explicitly. `us-*` and `eu-*` regions need no such adjustment.

## Workforce OBO invocation contract

A connector with `delegationMode: "obo"` changes the complete invocation chain;
it is not a credential-provider-only switch. The Gateway trusts the same workforce
Cognito issuer/client that protects the API and requires the exact deployment-owner
`sub` claim. End-user chat invokes
`POST /api/runtimes/{name}/slots/production/invoke`; the server resolves the
production slot to its immutable version/deployment/Runtime ARN and recorded
endpoint qualifier, revalidates the workforce token, reserves invocation capacity,
and invokes Runtime with target-account SigV4. The request carries the token only in
AgentCore's signed, allowlisted
`X-Amzn-Bedrock-AgentCore-Runtime-Custom-Workforce-Token` header. Runtime has no
custom-JWT authorizer, so a user bearer cannot invoke it directly and bypass platform
admission or budget authorization.

Both `GET /api/deploy/{deployment_id}` and `GET /api/deployments` expose the same
`runtime_name`: the sanitized tenant-facing slot key used by chat and the external
client. It is derived from the internal friendly-name authority (with AgentCore
name/id fallback), classified explicitly as public, and never persisted as a second
source of truth. Internal teardown authority and credential leaves remain excluded.

Generated agents create a fresh MCP client per request, present that request-local JWT
to the owner-bound Gateway, and close the client after every success or failure path.
The Gateway target's `grantType=TOKEN_EXCHANGE` performs the configured RFC 8693 or
RFC 7523 exchange. Missing, expired, Basic, SigV4, malformed, or wrong-target
credentials fail before Runtime dispatch; no client-credentials fallback exists.
Non-OBO agents retain their existing cached M2M/static authentication path.

Tokens are never stored in deployment state, Runtime payloads, logs, Memory,
tool receipts, or returned evidence. Responses may include non-credential
`identityEvidence` (issuer, subject, expiry, trace correlation, and an optional
downstream principal). The deploy cannot probe an OBO tool plane without a real
user token, so `delegated_identity_live_gate=first_authenticated_user_tool_call`
remains the explicit live gate: the downstream tool's `whoami`-style response
must attest the expected principal before production promotion. A new request
must present a fresh JWT; expiry and caller binding are checked at the API and the
Gateway independently validates its JWT authorizer contract. IdP revocation behavior follows the provider's access-token semantics,
so use short access-token lifetimes where immediate revocation is required.

## Long-running browser invocation

Direct non-OBO Runtime turns use an authenticated asynchronous transport so model
and tool work is never constrained by API Gateway's 30-second integration limit.
The browser sends exactly one `POST /api/test-runtime-async`, polls the returned
caller-bound test ID with `GET`, and removes the scratch result with `DELETE` after
consumption. The worker revalidates the deployment owner and HTTP protocol before
Runtime dispatch, applies the same policy promotion and invocation-budget reservation
as the synchronous path, and uses a 540-second AgentCore read bound inside the
Deployment Lambda's 600-second timeout. A timeout or lost poll response never causes
the browser to replay the model/tool turn. Results retain session IDs and tamper-
resistant tool receipts; bearer tokens are not placed in the async event or DynamoDB.
Scratch rows carry a one-day recovery TTL, and poll/delete return an indistinguishable
404 for absent and cross-tenant IDs. OBO agents continue to use the immutable
production-slot endpoint because their request-local workforce token is never queued
or persisted.

## Production safety and promotion gate

Dev-like environments (`dev`, `development`, `test`, `sandbox`, `preview`, and
`local`) preserve optional Guardrails and the direct-production iteration loop.
Every other environment enforces the platform-managed `production-v1` Guardrail
baseline before any row, secret, execution, or AWS resource is created. Guardrail
creation always applies the platform's governed ownership tags, so both the
in-account `StepGuardrailsRole` and the documented cross-account deployment role
include a separate, request-tag-conditioned `bedrock:TagResource` grant on
`guardrail/*`; granting `CreateGuardrail` alone is insufficient when `tags` are
present. Missing Guardrail configuration is injected automatically;
caller-supplied content filters are raised to `HIGH`, and credential/common-PII
filters are retained at `ANONYMIZE` or stronger. The compatibility input `SSN`
is canonicalized to Bedrock's `US_SOCIAL_SECURITY_NUMBER`, alias/canonical
entries are deduplicated with `BLOCK` winning, and every emitted PII type is
checked against the pinned botocore `CreateGuardrail` enum. Custom denied
topics and word/regex filters remain.
Production rejects `existing` mode because READY status alone cannot attest that
an externally mutable Guardrail still meets this baseline. Guardrail trace is
disabled in generated Strands models because trace output can retain original
matched PII, regex content, or blocked model output even when the served response
is masked. Generated Runtime agents also call `ApplyGuardrail` around every tool
boundary: Strands `BeforeToolCallEvent` cancels blocked arguments before execution,
and `AfterToolCallEvent` replaces blocked results before they reach the model. The two
direct Converse tool paths use the same wrapper. Oversized content, malformed service
responses, hook import failure, and ApplyGuardrail outage all fail closed without
logging content. Guardrail screening runs before HITL so blocked sensitive arguments
are not copied into approval records. Managed Harness remains development-only: its
current `CreateHarness` model has no native model Guardrail field, an after-tool deny
retains the completed result, and an after-invocation hook cannot retract streamed
output. Any production deployment with a tool Gateway also receives the
platform-managed `production-v1` Cedar profile: `enabled=true`, `mode=ENFORCE`,
and `on_enforce_failure=fail_closed`. Missing Policy configuration is injected;
`disabled`, `LOG_ONLY`, and fail-open caller settings are overridden before side
effects and again inside the Policy step. Production tool planes therefore
require AgentCore Gateway; LiteLLM tool Gateway deployments remain available in
development but are outside this production support envelope because they cannot
attach AgentCore Policy. Model-only agents do not create an unnecessary policy
engine. Runtime and Harness execution roles never receive Gateway/Policy control-plane
mutation authority. Every dynamic, per-agent, shared, and CloudFormation-exported
execution role carries an explicit `Deny` for Gateway/target/Policy create, update,
delete, and synchronization actions; policy builders also reject exact or wildcard
`Allow` patterns that would match those actions before IAM publication. JIT
permission requests cannot grant the same control-plane mutations. Approval first
claims the row as `APPLYING`; rejection is then impossible until the deterministic
IAM write is finalized `APPROVED` or a definitive no-side-effect refusal releases it.
Ambiguous IAM outcomes remain leased and retryable rather than being recorded rejected.

Production-like environments also default new versions to **staging** and reject
an explicit direct production deploy before side effects. The
`production_evaluation_gate` context may enable the gate in development but may
not disable it in production; a false or malformed production value fails
synthesis and deploy admission. The server-authored gate bit is required on the
prepared Step Functions input and is consumed unchanged by code generation, IAM,
and Runtime configuration; the prepared-payload validator refuses a missing or
non-boolean value before execution. Gated Python Runtime code keeps
`agent.py` as the sole direct-code entrypoint and initializes the packaged AWS
ADOT distro in-process before Strands binds its tracer provider, even if canvas
observability is off. The `opentelemetry-instrument` entrypoint prefix is not
used: AgentCore validates that prefix as the Node.js package contract and rejects
a Python ZIP with `CREATE_FAILED`, even when the Python console script is present.
The production environment disables ADOT's prefork-worker deferral because
`BedrockAgentCoreApp` runs in the direct-code process; failure to install a real
SDK provider for the required X-Ray traces endpoint fails Runtime startup closed.
Its framework spans remain content-redacted. The injected evaluation/force-flush
wrapper is re-registered through `BedrockAgentCoreApp.entrypoint`; rebinding only
the module variable is insufficient because the SDK stores the original handler
when the decorator runs. Executable removal-mutation coverage proves that dropping
this second registration bypasses marker stripping, span capture, and flush. Only
the owner-checked staging slot of an immutable production-gated version can add an
internal marker, which generated code strips before execution. Every instrumented
invocation emits one metadata-only `invoke_agent` correlation span with
`session.id` under service name `<AgentCoreRuntimeName>.DEFAULT` in shared
`aws/spans`. Only an owner-authorized staging invocation adds the documented
`gen_ai.task.input/output` fields to that span. The production slot cannot accept
the marker, and the live matrix invokes a post-promotion canary, requires its
metadata span, and proves its prompt and expected response phrase never appear in
any record correlated by either session ID or trace ID. CloudWatch Transaction
Search must be active in each target account and Region; the live integration
harness can lease this setting temporarily. After first enabling the destination
or indexing rule, it waits AWS's documented ten-minute search-readiness window
before creating evaluation sessions. It restores the exact prior destination and
indexing percentage after the run, treats an already-restored or still-settling
target as an idempotent recovery state, and retains a validated 0600 recovery
file until cleanup is proven. Failure auto-cleanup may delete every external AWS
resource before a Runtime exists; after it proves every recorded resource absent,
it invokes the same owner/version/slot/trigger-fenced atomic name-claim release as
an explicit DELETE and re-reads the exact version row and slot pointers before
publishing `deleted`. Any unproven identity, surviving resource, release conflict,
or ambiguous read-back remains `delete_retained`. An idempotent DELETE also
reconciles historical `deleted` tombstones that still carry a version ID, and
never reports success while that claim remains. The standalone `mcp-server-runtime` is model-free:
it receives neither this model-evaluation gate nor an injected Bedrock Guardrail;
its production gate is authenticated MCP protocol and complete tool execution.

A deployment's terminal record and its version-slot pointer are separate fenced
DynamoDB writes. Clients that just observed `succeeded` may therefore boundedly
retry a slots read only while it returns `404`; authentication errors, server
errors, an incorrect pointer, or a missing pointer after 60 seconds fail closed.

Promote a production-gated version from the **Versions** panel:

1. Invoke the exact staging version for at least five representative sessions.
2. Supply each session ID with assertions, expected tool trajectory, or expected turns.
3. Start the AgentCore Batch Evaluation. The platform—not the browser—chooses the
   evaluator IDs and thresholds from the policy frozen onto the version.
4. Check and attest the completed job. Partial, failed, ignored, stale,
   low-scoring, wrong-runtime, wrong-account, or wrong-Region results are refused.
5. Promote. The version row's evidence digest, policy digest, and expiry are
   condition-checked in the same DynamoDB transaction that moves the production pointer.

Frozen policy and evidence documents preserve canonical numeric types across
DynamoDB by type-tagging each finite floating-point value before persistence and
decoding it before digest validation. This deliberately does not trust a DynamoDB
number's decimal exponent: the live service canonicalizes trailing-zero values such
as `5.0` to `5`. Integral counts, limits, schema versions, digest fields, and epochs
remain ordinary DynamoDB values so the slot transaction can condition-check them.
Malformed or reserved float tags fail closed, and `5` versus `5.0` remain distinct
canonical JSON after decoding.

Live privacy and evaluation canaries use ordinary customer-style factual requests
with semantic answer oracles. They never demand exact output phrasing, because the
production prompt-attack Guardrail intentionally redacts instruction-shaped requests.

The default policy requires five completed sessions, zero failed/ignored
sessions, zero evaluator failures, and scores of at least `0.70` from
`Builtin.GoalSuccessRate`, `Builtin.Correctness`, and `Builtin.Helpfulness`.
Operators can raise or otherwise version this policy with CDK context
`production_evaluation_policy_json`; invalid JSON or fields fail closed. Release
evidence expires after 24 hours by default.

### Target-Region production evaluation keys

Every cross-account or same-account cross-Region target registration requires a
full customer-managed KMS **key ARN** (aliases are refused). The admin API uses
the target session to call only `DescribeKey` and `GetKeyPolicy`: the key must
belong to the registered account and Region, be enabled, use
`SYMMETRIC_DEFAULT` with `ENCRYPT_DECRYPT`, and have the scoped AgentCore service
statement shown in `docs/cross-account-deploy-role.json`. No key material is
read or returned. The exact ARN is stored on the target row and then frozen on
both the deployment and version authority rows.

Promotion batch startup reads that immutable authority. It uses the stack key
only for a home-Region version; target versions use their registered target key.
A missing legacy value, a changed target row, or disagreement between the
version and deployment records fails closed before `StartBatchEvaluation`.
Re-register legacy targets and redeploy a new version to enable production
promotion; existing version authority is intentionally never backfilled.

Batch inputs/results are encrypted with the stack's dedicated rotating KMS key.
The key policy scopes the deployment Lambda and
`bedrock-agentcore.amazonaws.com` to this account's `batch-evaluate/*` ARN and
encryption context. In production-like environments the key uses
`RetainExceptOnCreate`: failed first-stack creation cleans it up, while later stack
deletes and replacements retain evidence independently from the UI. Dev/test/sandbox
and other throwaway environments destroy it with the stack. This prevents both orphaned
failed-create keys and accidental production evidence loss. A cross-account or same-account
cross-Region target must provide a target-Region evaluation key before it can
inherit production-promotion support; a home-Region key is never reused across
that boundary.

Emergency rollback remains available, but only as an atomic swap to the exact
previous production version. It cannot select an arbitrary unevaluated version.

## Append-only production audit

Both API Lambdas classify every write route and append correlated audit evidence.
Authenticated API writes use the shared middleware; public webhook deliveries
append only after HMAC/replay validation so unauthenticated traffic cannot create
audit rows. Production records an `attempt` before route execution; if that conditional
`PutItem` fails, the route returns 503 before application side effects. Outcomes
are separate immutable rows. The platform roles can append/query but are
explicitly denied update, delete, and batch-write access to the audit table.
Every row has a canonical SHA-256 digest verified on read, and incomplete
attempts are highlighted in the admin UI. Production events omit the `ttl`
attribute and the table is retained/deletion-protected; DynamoDB TTL processing
stays enabled so pre-upgrade and development rows carrying `ttl` still expire.

This is an append-only, tamper-evident projection inside the platform IAM
boundary, **not** an Object Lock/WORM compliance attestation against account
administrators. See [`docs/DATA_RETENTION.md`](docs/DATA_RETENTION.md) for the
pseudonymous-identifier and retention responsibilities.

## Production-slot runtime triggers

Cron, EventBridge, S3, and signed-webhook triggers bind to the exact production
version and never target staging implicitly. The Triggers panel checks the slot
first: a staging-only deployment is directed to the Versions tab to run the
required evaluation and promotion, rather than exposing controls that cannot be
used yet. Trigger delivery remains owner-scoped and version-pinned after later
promotions. The real delivery matrix gives its synthetic `g-users-default`
identity the orthogonal `g-admins-cost` group only for the run: agent budgets are
bound to the writer's `owner_sub`, while the cost group grants no trigger scope
and therefore cannot mask trigger RBAC. The matrix creates one unique agent-scoped
invocation budget before staging evaluation, then deletes and absence-checks it
in `finally`, so no reusable budget definition is left behind. A newly enabled
S3-to-EventBridge notification can take around five minutes to activate, and an
object uploaded before activation is not replayed later. The real delivery matrix
therefore writes uniquely keyed, AES256-encrypted probe objects once per minute
only while its exact S3 trigger is still pending, capped at 15 attempts. It never
substitutes `PutEvents` for the S3 source and still requires the product's
completed, seven-day-TTL delivery row; removing the bounded refire reproduces
the activation-loss failure. The lifecycle and
gallery integration cases use the same register-before-write tracker around every
Runtime invocation, including MCP discovery and tool calls; each unique Runtime
receives an owner-bound temporary budget that is deleted and absence-checked on
success or failure. Conversational gallery cases use customer-style requests that
pass the unchanged `HIGH` prompt-attack filter, then require a successful runtime-
generated tool receipt with exact SHA-256 argument digests before accepting the
semantic response oracle. The oracle accepts plain, fenced, and double-encoded
JSON plus a bounded Lambda proxy envelope whose integer `statusCode` accompanies
a string `body`; it does not recursively interpret arbitrary webpage content,
and any structured error found in the recognized envelope fails the run.
Demo customer tools advertise only non-PII customer IDs and do
not return or echo email addresses, so the production PII Guardrail remains strict
without erasing useful customer data. Full web search is provided by the Amazon
Bedrock AgentCore Gateway Web Search Tool (`connectorId: web-search`), pinned to
version `1.2.0`. AgentCore managed connector targets publish automatically and
reject `SynchronizeGatewayTargets`; the platform therefore requires exact pinned
configuration/version read-back plus `READY`, then proves the published
`AmazonWebSearch___WebSearch` tool through the Gateway MCP `tools/list` plane.
Queries stay within AWS, and result handling preserves source titles and HTTP(S)
links as citations. The `duckduckgo_search` identifier is
accepted only as a saved-canvas migration alias and is removed before Lambda
schema construction; no DuckDuckGo HTML/Lite scraper ships in generated agents,
Gateway tool Lambdas, standalone MCP runtimes, or CloudFormation bundles. Evaluation
uses ordinary low-entropy factual questions with
semantic answer oracles rather than prompt-injection-shaped “reply exactly”
commands or random credential-like markers; a Guardrail redaction placeholder is
a test failure, never accepted as an evaluation pass.

## Atomic invocation authorization

The Cost panel can create monthly owner, agent, or tag limits. In production,
every platform-mediated slot, direct test, authenticated async browser turn,
buffered stream, Function URL stream,
MCP, and durable-trigger invocation operation requires at least one applicable
limit. A bounded MCP handshake and tool call are one operation. The platform strongly reads applicable definitions and uses one DynamoDB
transaction to condition-check their current owner and value, increment every
matching monthly counter, and create a correlated reservation row. Concurrent
reserved plus committed units cannot exceed any matching limit.

The unit is configured with CDK context `budget_reservation_usd` (default `1.0`)
and passed as `BUDGET_RESERVATION_USD`. It is USD-denominated **invocation
authorization capacity**, not predicted or reconciled AWS cost. Once network
dispatch begins, both successful and failed Runtime responses commit the unit
because either can incur charges. Calls proven not dispatched release it. A
crashed or otherwise unknown reservation commits conservatively after one hour;
it is never released on uncertainty. Exhaustion returns HTTP `429` with
`Retry-After: 3600`; missing or unreadable production storage returns `503`
before Runtime dispatch.

This is deliberately not advertised as an AWS bill cap. It excludes downstream
tool/vendor charges, direct calls made with separately granted service credentials,
and variance between the configured unit and actual model/Runtime pricing. Production
A2A is refused because inbound and nested peer Runtime calls do not yet propagate a
reservation. OBO Runtime closes the direct bearer bypass: only the platform's SigV4
principal invokes it, after reservation, while the workforce JWT travels in a signed
custom header for request-local Gateway token exchange. Cost Explorer/Budgets and
service quotas remain necessary account-level controls.

Budget definitions and monthly counters contain pseudonymous owner IDs and aggregate
authorization values, not prompts or tokens. Settled reservation rows expire after 90
days; monthly counters expire after 400 days; definitions remain until deleted. See
[`docs/COSTS.md`](docs/COSTS.md), [`docs/DATA_RETENTION.md`](docs/DATA_RETENTION.md),
and [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md).

## Exact-commit release evidence

Production readiness is a measured release gate, not a repository-wide claim. The
machine-readable contract in [`release/production-readiness-controls.json`](release/production-readiness-controls.json)
requires positive, adversarial, and control-removal mutation evidence for every
material control, plus real live, CloudFront UI, rollback, and zero-residual teardown
evidence where applicable. The executable
[`release/production-readiness-traceability.json`](release/production-readiness-traceability.json)
plan pins each required kind to exact suite commands, committed test selectors, and
named live scenarios; finalization requires every mapped suite/scenario. This includes
tracked-file secret scanning, high-severity npm dependency audit, and the isolated
cfn-lint/Checkov/Terraform/ShellCheck gate over customer-exported artifacts. The
infrastructure nag gate reads each stack's CLI-consumed `additionalMetadataFile`
directly rather than depending on import-order-sensitive jsii conversion through
overlapping generated CDK schema distributions. The dated
[`release/emea-agentcore-regions.json`](release/emea-agentcore-regions.json) matrix
requires an independent core scenario in every documented EMEA AgentCore Region;
feature-specific scenarios run only where AWS documents that feature as available.

[`scripts/release_evidence.py`](scripts/release_evidence.py) starts only from a clean
Git commit, appends bounded digest-only events tied to mapped suites/scenarios, keeps
failures open until explicitly superseded, refuses generic or incomplete finalization,
and verifies the sealed event set from a separate detached clean clone. It stores no
command line, environment, credential, token, or raw AWS response. A final digest
proves exact source and evidence coverage; it does not fabricate live success or
constitute third-party/WORM attestation.
See [`release/README.md`](release/README.md) for the workflow and support matrix.

No candidate is production-ready until its current-commit run finalizes and verifies.
Passing unit tests, synthesis, a prior revision's AWS run, or one Region alone is
insufficient.

## Accessing the Platform

After deployment completes, the script prints two URLs:

- **Frontend** — `https://dXXXXXXXXXX.cloudfront.net` — the visual workflow builder.
- **Backend API** — `https://XXXXXXXXXX.execute-api.region.amazonaws.com` — the API Gateway endpoint (CloudFront routes `/api/*` here automatically).

You can retrieve these at any time from the CloudFormation stack outputs (`CloudFrontUrl`, `ApiGatewayUrl`, `S3BucketName`):

```bash
aws cloudformation describe-stacks --stack-name agentcore-workflow-dev \
  --region us-east-1 \
  --query "Stacks[0].Outputs" --output table
```

Substitute the region you deployed to. The stack, the user pool and the
distribution all live in that one region.

Every JWT-protected API route requires Cognito's
`aws.cognito.signin.user.admin` authorization scope. Use the Cognito **access
token** as the API bearer token; an ID token is rejected by API Gateway before
Lambda. `GET /health` and the HMAC-authenticated webhook ingress are the only
intentional exceptions.

### First sign-in — assign a persona

`COGNITO_USERS` pre-creates Cognito **users** and puts each one in `g-users-default` + `t-user`: a standard user who can build, deploy, invoke, and schedule their own agents and browse & clone the registry. The API enforces scopes by default (`RBAC_ENFORCE=true`), so a user in **no** group (for example one created in the Cognito console) gets `403` on every call until you assign one. Admin personas are always a manual grant:

> **Always pass `--region <the region you deployed to>`** — `us-east-1` below, but
> use `eu-central-1` if that is where you deployed. Without it the AWS CLI falls
> back to your shell/profile default region, the pool lookup returns empty, and
> `$POOL_ID` is blank, so `admin-add-user-to-group` fails with
> `Invalid length for parameter UserPoolId, value: 0`.

```bash
POOL_ID=$(aws cognito-idp list-user-pools --max-results 40 --region us-east-1 \
  --query "UserPools[?Name=='agentcore-workflow-dev-users'].Id | [0]" --output text)

# Full access (all scopes) + admin UI + registry approver:
for g in g-admins-super t-admin registry-admin; do
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \
    --username you@example.com --group-name "$g" --region us-east-1
done

# ...or a standard end-user who can build/deploy/invoke + browse & clone the registry:
for g in g-users-default t-user; do
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \
    --username you@example.com --group-name "$g" --region us-east-1
done
```

**Sign out and back in** after changing groups — scopes are read from the ID token at sign-in. See [Personas](docs/PERSONAS.md) and [Registry & RBAC](docs/REGISTRY_AND_RBAC.md) for the full model.

## Bring your own LiteLLM

If you already run a **LiteLLM proxy**, it can serve two roles here: as an **MCP
gateway** for individual agents, and as the **agent catalog** behind the Registry.
The two are independent — enable either, both, or neither.

For the gateway role there are **two different ways to wire it**, and they are not
the same thing, so the choice is worth making deliberately. Together with plain
AgentCore Gateway, an agent on the canvas has three supported shapes:

| On the canvas | What gets created | Use when |
|---|---|---|
| **Gateway node, Provider = AgentCore** *(default)* | A real AgentCore Gateway with your Lambda / OpenAPI / Smithy / MCP targets | The default. Nothing about it changes. |
| **Gateway node, Provider = LiteLLM** | No AgentCore Gateway at all — the agent talks straight to your proxy | LiteLLM *replaces* the gateway. Your proxy already aggregates every tool the agent needs. |
| **Gateway node, Provider = AgentCore, with a `Custom endpoint…` MCP target pointed at LiteLLM** | An AgentCore Gateway that carries your proxy as one `mcpServer` target | You want LiteLLM's tools **alongside** Lambda/OpenAPI targets, or you want AgentCore's inbound Cognito auth, semantic search and observability in front of it. |

The second and third are covered below; the third is a normal MCP target and needs
no LiteLLM-specific setting beyond the endpoint and key.

All of this is **additive**. AgentCore Gateway stays the default gateway, the
built-in DynamoDB catalog stays the default registry, and existing canvases,
stored flows and deployed agents are unaffected until someone explicitly opts in.
Nothing is migrated and nothing is deleted when you switch either one back.

### As the gateway itself, per agent

On the canvas, open the **Gateway** node and set **Provider** to
`LiteLLM MCP Gateway (your own proxy)`, then fill in:

| Field | Notes |
|---|---|
| **Base URL** | The proxy root (e.g. `https://litellm.example.com`), not an MCP path. `https` only. |
| **Virtual Key** | Sent as `x-litellm-api-key: Bearer <key>`. Write-only — minted into Secrets Manager on deploy and never returned to the browser or stored on the canvas. |
| **MCP Servers** *(optional)* | Comma-separated server aliases (`github, jira`) to scope this agent to. Blank means every server the key can see. |

Choosing `litellm` replaces the AgentCore target list entirely — there is no
Lambda/OpenAPI/Smithy target configuration on this path. At deploy time the
platform:

1. Runs the base URL through the same **SSRF guard** as every other outbound URL (`https`-only; private, loopback and link-local ranges rejected, including the instance-metadata address).
2. Mints the virtual key into Secrets Manager and drops the raw value before the deployment event is re-emitted.
3. **Probes the proxy** — `GET /v1/mcp/server`, then `GET /mcp-rest/tools/list` — and fails the deploy loudly if it serves zero tools, rather than shipping an agent with no tools.
4. Resolves the agent's MCP endpoint to `{base}/mcp/`, adding the `x-mcp-servers` header when you scoped it.

The generated agent authenticates with a **static bearer key**
(`GATEWAY_AUTH_MODE=static_bearer`) instead of the Cognito client-credentials
exchange AgentCore Gateway uses. Teardown deletes only the virtual-key secret —
your proxy is external, so the platform records it as informational and never
issues a delete against it.

> **The proxy must be reachable from the deploy Lambda.** The control plane has no
> VPC egress, so a VPC-private LiteLLM cannot be probed and the deploy will fail
> at step 3 even though a VPC-mode Runtime could reach it at invoke time.

There is also a platform-wide **fallback**, but read what it does before setting
it: it applies **only** to a gateway config that carries no provider field at all.
A per-agent choice always wins, and the canvas always writes one — so this row
changes nothing for agents built in the UI. What it does reach is configs that
omit the field: canvases saved before this feature existed, imported JSON, and
direct API deploys.

> **It is a tie-breaker, not a switch.** The row carries no base URL or virtual
> key, so a config it applies to has no LiteLLM connection details and its deploy
> fails with `LiteLLM gateway requires a base URL (litellm_base_url)`. On a stack
> with pre-existing canvases, setting this to `litellm` can therefore break
> AgentCore gateways that were working. Prefer the per-agent selector above.

Set it (there is no API route for this) with:

```bash
aws dynamodb put-item --region us-east-1 \
  --table-name agentcore-workflow-dev-tag-policy \
  --item '{"org_id":{"S":"default"},"sk":{"S":"SETTING#gateway_provider"},"value":{"S":"litellm"}}'
```

A per-agent choice on the canvas always wins over the default. A missing row, an
unreadable table, or an unrecognized value all resolve to `agentcore`, so the
existing behavior is what you get whenever anything is uncertain. The Lambda
environment variable `DEFAULT_GATEWAY_PROVIDER` overrides the row if set.

### As a target inside an AgentCore Gateway

The opposite trade from the section above: keep the AgentCore Gateway and hang the
LiteLLM proxy off it as one MCP target, so its tools sit next to your Lambda and
OpenAPI targets behind AgentCore's inbound Cognito auth.

Leave **Provider** on `AgentCore`, add a target of type **MCP Server**, pick
**`Custom endpoint…`**, and fill in:

| Field | Value for a LiteLLM proxy |
|---|---|
| **MCP Endpoint URL** | `https://litellm.example.com/mcp/` for every server the key can see, or `https://litellm.example.com/<alias>/mcp` to pin one |
| **Outbound Auth** | `API key` |
| **API Key** | The virtual key |
| **API Key Header** | Leave blank for `Authorization`, or set `x-litellm-api-key` |
| **Key Format** | `Bearer <key>` (the default) |

One thing differs from the provider path: server scoping is done **in the URL**,
not with the `x-mcp-servers` header — a Gateway target has no way to send an extra
header per request.

Readiness is still enforced. The deploy waits for the new target to finish
AgentCore's own `initialize` handshake against the proxy and **fails with the
reason** if it does not connect, so a wrong endpoint or key is a failed deploy
rather than an agent that quietly has no tools.

Tool names get **two** prefixes on this path — the Gateway prepends the target name,
and LiteLLM has already prepended its server alias — which can push the combined name
past the 64-character limit Bedrock enforces on tool names. Where that happens the
deployed agent shortens the name it shows the model (the tool's own name plus a short
unique suffix) and still calls the gateway by the full name, so nothing is dropped and
no configuration change is needed. Expect the shortened form in agent traces.

> **The key format matters here.** A custom MCP endpoint sends
> `Authorization: Bearer <key>` by default. That is what LiteLLM's MCP endpoint
> accepts — a bare `Authorization: <key>` with no scheme is rejected. Change
> **Key Format** to `Raw key` only for a server that documents a bare value
> (an `x-api-key` style server); it will break LiteLLM.

### As the agent registry

Open the **Registry** modal and find the **LiteLLM Registry** card, then enter the
proxy base URL and a virtual key:

- **Connect & test** — saves and probes the proxy **without** switching the catalog over, so you can confirm reachability first.
- **Connect & make authoritative** (or **Make authoritative** afterwards) — LiteLLM becomes the catalog.
- **Disconnect & use platform catalog** — reverts, and clears the stored connection, so the base URL has to be re-entered to reconnect.

These calls are **registry-admin-only** (`g-admins-registry`, `g-admins-super`, or the legacy `registry-admin` / `org-admin`); the API answers `403` for anyone else.
`verified: false` after a save is not an error — it means the control plane could
not reach the proxy, which is normal for a VPC-private LiteLLM.

**What LiteLLM can and cannot back.** LiteLLM exposes no create/update/delete API
for MCP server records — registration happens in its Admin UI or `config.yaml` —
and its records have no canvas snapshot, owner, or review state. So:

| Registry surface | With LiteLLM active |
|---|---|
| list / search / get | Served by LiteLLM — it is the catalog |
| Pre-deploy governance gate | Served by LiteLLM — present-and-enabled is the approval signal |
| publish / update / delete / clone from a canvas | Platform **sidecar** in DynamoDB — fully mutable |
| the same operations on a **projected** LiteLLM entry | **`501`**, naming LiteLLM, rather than silently accepting a write that would then diverge |

The read-only limit is therefore **per entry, not per operation**: rows projected
from LiteLLM are read-only, rows published from a canvas stay fully mutable, and
on a slug collision the sidecar row wins. `GET /api/registry/litellm-config`
returns this as machine-readable `capabilities`.

The governance gate stays **fail-closed** — if the catalog cannot be read, deploys
referencing an integration return `503` rather than proceeding ungoverned. Your
existing DynamoDB entries are never read from, written to, or deleted while
LiteLLM is active, and they are all still there when you disconnect.

Note that not every LiteLLM release reports an enablement flag. Where it does not,
presence in the server list *is* the approval signal, so remove a server from
LiteLLM to stop it being deployable rather than relying on a disable toggle.

### Verifying against a real proxy

`scripts/verify-litellm.py` exercises both paths against a live LiteLLM. It issues
reads only, and asserts that writes to projected entries are refused.

Its docstring has the full setup recipe — no Docker needed, but budget time and
read the two warnings there before starting. LiteLLM's MCP endpoints require a
Postgres database (without one they return `No connected db.`), and first startup
spends roughly half an hour baselining 158 schema migrations during which
`/health/liveliness` returns nothing at all and the proxy looks like it has
crashed. It hasn't.

Full details: [MCP Gateway Integration](docs/MCP_GATEWAY_INTEGRATION.md) for the
gateway path and the endpoint wire shapes, [Registry & RBAC](docs/REGISTRY_AND_RBAC.md)
for the catalog path, and [API Reference](docs/API_REFERENCE.md) for every
endpoint and configuration variable.

## Cleanup

```bash
# Tear down all resources (prompts for confirmation)
./scripts/cleanup.sh

# Tear down a specific environment
ENVIRONMENT_NAME=prod ./scripts/cleanup.sh

# Non-interactive teardown (CI / scripted) — skips the confirmation prompt
FORCE_DESTROY=true ./scripts/cleanup.sh

# Also sweep resources that carry no owner tag (see below — off by default)
CLEANUP_INCLUDE_UNTAGGED=1 ./scripts/cleanup.sh
```

The cleanup script validates credentials, deletes every AgentCore resource the
platform created (runtimes, gateways, memories, KBs, vector stores, Cognito
pools, secrets, IAM roles), empties the S3 buckets, runs `cdk destroy` on the
stack, and verifies all resources are removed.

Registered target evaluation keys are customer-owned prerequisites, not
platform-created resources. `cleanup.sh` does not schedule or delete them.
Before deleting a target key, retire every version that names its ARN and retain
the key for as long as its encrypted batch-evaluation evidence must remain
readable. If registration fails, check the key account/Region/state/spec first,
then compare its default policy and the target deployment-role policy with
`docs/cross-account-deploy-role.json`; do not work around the check with an alias
or the home-Region key.

### Safe to run with more than one deployment in the account

Deploying and deleting repeatedly is expected, and so is running two deployments
side by side — dev + prod, two teams, or the same environment in two regions. The
resources above have **account-global names** with no deployment identity in them
(Cognito `AgentCore*`, secrets under `agentcore-connector/` and `agentcore-otel/`,
IAM roles `AgentCoreMemory-*` and `AgentCoreRuntime-*`), so a sweep by name prefix
would delete a co-resident deployment's resources.

Everything the platform creates is therefore tagged
**`AgentCoreStack={project}-{env}-{region}`** at creation, and teardown deletes only
what carries its own value. The region is part of the identity because IAM roles are
not regional, so a us-east-1 teardown must not claim a Frankfurt deployment's roles.
`ManagedBy=agentcore-flows` is also present but is *not* the gate — it names the
product, so it is on every deployment's resources alike.

Two consequences worth knowing:

- **Untagged resources are skipped**, and teardown tells you what it left and why. A
  resource created before this tag existed looks identical to one belonging to another
  deployment; skipping costs a manual delete, deleting a foreign credential cannot be
  undone. Pass `CLEANUP_INCLUDE_UNTAGGED=1` to sweep those too — safe only if this is
  the account's sole deployment.
- **CloudFormation-owned resources are left to `cdk destroy`.** The shared runtime
  execution role (`AgentCoreRuntime-{project}-{env}[-{region}]-shared`) is skipped by
  the orphan sweep by design; deleting it out from under a running deployment costs a
  redeploy plus AgentCore's 17–20 minute IAM propagation wait.

Two escape hatches exist in `cleanup.sh` and both are **off by default** for this
reason. `CLEANUP_INCLUDE_UNTAGGED=1` additionally sweeps untagged resources.
`CLEANUP_INCLUDE_FOREIGN_RUNTIMES=1` sweeps *every* AgentCore runtime, gateway,
memory, policy engine and credential provider in the region regardless of owner —
use it only in a single-tenant account you are willing to empty.

Customer-owned resources are a separate authority boundary from teardown
ownership. A live Knowledge Base deployment will not use an existing KB, bucket,
vector index, OpenSearch collection, Aurora cluster, Lambda, KMS key, or
credential source merely because its ARN was supplied. Its owner must first add
`AgentCoreFlowsAccess=allow`; optionally add `OwnerSubHash` to bind the opt-in to
one Cognito caller. Credential values are copied into a deployment-bound target
secret, and the source secret is neither granted to the runtime nor deleted with
the deployment. See [Deployment Internals](docs/DEPLOYMENT_INTERNALS.md#live-deploy-authority-boundary).

`scripts/verify-cleanup-ownership.sh` proves this against real AWS: it plants decoys
in every swept namespace — owned by this stack, owned by a different stack, and
untagged — runs the real sweep, and asserts only its own are gone. It removes
everything it plants. Run it in a scratch account, not production; it executes the
real teardown sweep.

## Running Tests

```bash
cd backend && pip install -e ".[dev]" && pytest   # backend unit + property tests
cd infra && pip install -r requirements.txt && pytest tests/ -v   # CDK assertions
cd frontend && npm install && npm test            # frontend tests
```

Integration tests run against a real deployed stack — see [Development](docs/DEVELOPMENT.md).

## Documentation

| Doc | Contents |
|-----|----------|
| [Embeddable Chat](docs/EMBEDDABLE_CHAT.md) | Versioned browser SDK/Web Component, token-provider contract, CORS/CSP, events, testing and cleanup |
| [Enterprise Capabilities](docs/ENTERPRISE_CAPABILITIES.md) | Versioning & rollback, Cedar policy enforcement, evaluation, cost analytics, registry, prompt library, durable triggers, connectors, HITL, governance & FinOps |
| [Security & Hardening](docs/SECURITY_HARDENING.md) | Infrastructure hardening, CDK-NAG, tenant isolation, SSRF guards, pre-commit hooks |
| [Observability](docs/OBSERVABILITY.md) | Per-canvas and platform-level OTEL modes, OTEL deploy configuration |
| [Deployment Internals](docs/DEPLOYMENT_INTERNALS.md) | Infrastructure & agent deploy flows, gateway tool pipeline, code architecture, CFN export, packaging, templates, project structure |
| [API Reference](docs/API_REFERENCE.md) | Every API endpoint, configuration variables, SSM parameters |
| [Registry & RBAC](docs/REGISTRY_AND_RBAC.md) | Agent registry roles, approval workflow, persona assignment |
| [Personas](docs/PERSONAS.md) | Platform-wide group → scope model |
| [RBAC Rollout](docs/RBAC_ROLLOUT.md) | Enforcement, the advisory escape hatch, and upgrading an existing pool |
| [Costs](docs/COSTS.md) | AWS resources created + infrastructure pricing estimates |
| [Development](docs/DEVELOPMENT.md) | Local development, full test matrix, tech stack |
| [Data Retention](docs/DATA_RETENTION.md) | TTLs, PII posture, audit access |
| [MCP Catalog](docs/MCP_CATALOG.md) | External MCP catalog servers as Gateway targets |
| [MCP Gateway Integration](docs/MCP_GATEWAY_INTEGRATION.md) | MCP protocol details for gateway-connected agents |

## License

MIT-0 (MIT No Attribution). See [LICENSE](LICENSE).
