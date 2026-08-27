# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed — AWS Agent Registry: preview → GA
Agent Registry graduated out of AgentCore into its own AWS service. The rename is
a **silent** break: the deprecated `bedrock-agentcore-control` model still exposes
the Registry operations with the old `descriptorType` parameter, so preview code
keeps "succeeding" against a shim under an IAM prefix it no longer has. Migrated
end-to-end:
- boto3 clients `bedrock-agentcore-control`/`bedrock-agentcore` →
  `agent-registry-control`/`agent-registry`; IAM actions `bedrock-agentcore:*` →
  `agent-registry:*` (both planes sign as `agent-registry`)
- `descriptorType` → `recordType`, with the enum renamed
  `MCP|A2A|CUSTOM|AGENT_SKILLS` → `MCP|AGENT|CUSTOM|SKILL` (preview spellings are
  still accepted as input aliases)
- Descriptors reshaped: `a2a.agentCard.inlineContent` → `a2aAgentCard.data`,
  `custom.inlineContent` → `custom.data`, `schemaVersion` → `dataSchemaVersion`;
  added `mcpServer` and `agentSkillsDefinition` builders
- Data-plane `SearchRegistryRecords` → `SearchDiscoverableRegistryRecords`, with
  the GA structured filter shape (`{"recordType": {"$in": [...]}}`)
- `boto3 >= 1.43.66` is now a hard floor (first release carrying the
  `agent-registry` service models) in both `pyproject.toml` and
  `requirements-lambda.txt`
- `GET /api/registry/aws-config` gained `sdk_supported`, and `POST` now returns a
  400 naming the SDK instead of blaming the `registry_id`, so an under-pinned
  bundle is distinguishable from a bad registryId

All of the below was verified against the live GA service, not just the boto3
models: a throwaway registry, every descriptor builder submitted through the
shipping adapter, and the approval lifecycle exercised end to end.

### Fixed — found by live verification against GA
- **Every redeploy silently failed to re-register.** `name` + `recordVersion` is a
  uniqueness key and `recordVersion` is `"1.0"` for everything the platform
  registers, so the *second* deployment of an agent raised `ConflictException`
  inside the best-effort auto-register handler. The symptom was a governance record
  frozen at the first deployment's runtime ARN and endpoint — stale forever, with
  nothing surfaced anywhere. `register()` is now an upsert (falling back to
  `UpdateRegistryRecord`), which needs the new
  `agent-registry:UpdateRegistryRecord` grant on the `status_update` step role.
  Note updating content demotes a record `APPROVED` → `DRAFT`, so an upsert cannot
  slip changed content past an old approval — a redeployed integration must be
  re-approved, which is the fail-closed reading.
- **`available()` reported a still-provisioning registry as usable.** It returned
  True the instant `GetRegistry` succeeded, but a registry in
  `CREATING`/`UPDATING`/`DELETING` rejects `CreateRegistryRecord` with
  `ConflictException`. Enabling federation on a freshly created registry — the
  common sequence — therefore passed validation and then raced into that conflict
  on the first deploy. Now gated on `READY`, with a new `registry_status()` that
  keeps "not READY" distinct from "could not ask"; `POST /aws-config` returns 409
  ("still provisioning") instead of a 400 blaming the registryId.
- **Search results could show a stale `APPROVED` badge.** The data plane is a
  search index, not the record store: a record demoted to `DRAFT` keeps being
  served as `APPROVED` for many minutes (still drifting 20 minutes after
  demotion). Combined with the upsert this is reachable on the ordinary redeploy
  path. `GET /api/registry/aws-search` now reconciles every hit's status against
  the control plane and reports `status_authoritative: false` — dropping `status`
  rather than serving the index's copy — when it cannot. Approval *gating* always
  read the control plane and was never affected; a new AST-level guard test keeps
  it that way.
- **Descriptor content contracts corrected** (each one an outright rejection by the
  live schema validator, reported only as an unactionable descriptor-wide error):
  A2A card skills require *all* of `id`/`name`/`description`/`tags` (empty `tags`
  is fine, absent is not) and the card requires `url`; `mcpServer.data` is an MCP
  server.json whose `name` must be namespaced `<namespace>/<server>` (a bare name
  is rejected) with `description` and `version` required; `agentSkillsDefinition`
  must omit `dataSchemaVersion` entirely, unlike every other descriptor; and both
  the tools and skills payloads must be objects (`{"tools": [...]}`), never bare
  arrays. Under-specified inputs are now normalized rather than forwarded.
- `UpdateRegistryRecord` takes a different shape from `CreateRegistryRecord` —
  every branch and scalar leaf is wrapped in an `optionalValue` patch envelope.
  Passing the create shape fails in botocore's *client-side* validation, never
  reaching AWS, and on the deploy path that lands in the best-effort handler.

### Fixed
- **Auto-register on deploy never worked**: the `status_update` step Lambda — the
  role that actually calls `CreateRegistryRecord` — had no registry permissions at
  all, so every federation attempt was an `AccessDenied` swallowed by the
  best-effort handler. The exception cause is now logged rather than discarded.
- **An unqueryable registry was indistinguishable from a rejected integration.**
  Gating swallowed every error into "nothing is approved", so an `AccessDenied` on
  `agent-registry:ListRegistryRecords` — or a registryId typo — rendered as a 403
  telling the operator their integrations had been *rejected*, sending them to fix
  a governance record when the fault was an IAM policy. Absent data and negative
  data are now distinct: `list_records_strict()` raises `RegistryQueryFailed`,
  which surfaces as a 503 naming the registry as unreachable. Gating stays
  fail-closed for a *successful* query that finds no approval.
- **`list_records()` returned only the first page**, so fail-closed integration
  gating could block a deploy against an integration that *is* `APPROVED` further
  down the list. Now follows `nextToken`, and pushes the `APPROVED` narrowing
  server-side via the GA `filters` parameter.
- Registry adapter degrades instead of raising when the bundled boto3 predates GA
  (`boto3.client()` raising `UnknownServiceError` used to 500
  `GET /api/registry/aws-config`).
- Descriptor `data` payloads are now checked against the service's 102400-**byte**
  cap (measured in bytes, not characters) with an error naming which descriptor
  overflowed. AWS's `ValidationException` identifies neither, and on the deploy
  path it lands in a best-effort handler that would reduce it to a log line.
- `frontend/src/services/api.ts` carried a second, independent declaration of
  `getAwsRegistryConfig()`'s return type; only `tsc -b` (project references, as CI
  runs it) surfaced the mismatch — `tsc -p` on the root project did not.

### Added
- GitHub Actions CI: ruff lint/format, backend unit tests with coverage floor,
  CDK assertion tests + `cdk synth` (cdk-nag gate), frontend lint/typecheck/tests/build
- Dependabot for npm, pip, and GitHub Actions; `SECURITY.md` vulnerability policy
- Pyright (basic mode, advisory) and wider ruff rule set (`I`, `B`, `UP`)
- Committed `frontend/package-lock.json` for reproducible builds (`npm ci`)

### Fixed
- README/`.env.example` no longer instruct deploying to `us-west-2`, which
  `deploy.sh` rejects (the WAF WebACL is CLOUDFRONT-scoped and requires
  `us-east-1`)
- Stale CDK assertion tests updated to the current architecture (14 DynamoDB
  tables, 3 S3 buckets, no `States.TaskFailed` retry, CloudFront Function SPA
  routing instead of CustomErrorResponses)

### Fixed — full-matrix verification (12 live-found deploy/runtime defects)
Every deployable pattern was verified end-to-end against real AWS —
**94 patterns PASS with canary evidence, 0 FAIL, 0 PARTIAL** (the remaining
294 are BLOCKED by design: non-Bedrock frameworks / third-party
IdPs / SaaS creds / customer VPC infra, each code-cited). Fixes:
- Web-crawler KB verified end-to-end (example.com → ingest → index → agent
  retrieves the crawled content); the ingestion wait is bounded to the SFN
  task budget and an in-progress crawl is treated as success, not failure
- Generated memory agents now retrieve long-term memory records across sessions
  (`retrieve_memories` was never called); memory+knowledge-base canvases no
  longer silently drop KB retrieval
- `CreateMemory` retries the IAM trust-policy propagation race; failed deploys
  no longer leak gateways (targets deleted before the gateway)
- OpenSearch Serverless KBs: `aoss:BatchGetCollection` scoped correctly
  (account-level API); BDA parsing uses the correct
  `supplementalDataStorageConfiguration` shape + bucket-root URI + role grants
- Knowledge-base deploys are idempotent on retry (`CreateDataSource` /
  `StartIngestionJob` conflict-adopt); KB step role gains
  `ListDataSources`/`GetDataSource`
- KB-backed runtime deletion is now asynchronous — returns immediately with a
  `delete_status` pointer instead of timing out API Gateway's 29s cap (503);
  double-delete is tolerated
- Cedar ENFORCE policy engine self-heals a regressed `UPDATE_FAILED` permit
  (previously could stay deny-all forever if no touchpoint fired); the
  scheduled sweep reconciles ENFORCE engines against live policy status
- `GET /evaluation-config` resolves custom-named online-evaluation configs by
  CloudWatch target (not just the `eval_<id>` name heuristic)
- `list_gateways` conflict recovery is paginated (multi-page accounts)

### Added — multi-target gateways & custom MCP endpoints
- One gateway node can now carry **multiple targets of different families**
  (Lambda ARNs, external MCP servers, OpenAPI specs, Smithy models) via a
  repeatable target-array editor; the deploy creates one gateway target per
  entry with family-appropriate outbound credentials
- The MCP-server picker gained a **Custom endpoint…** option (any https MCP
  URL with none / API-key / OAuth2-CC / IAM SigV4 outbound auth, SSRF-validated)
- Generate Agent emits gateway nodes with the required `targetType`/
  `targetConfig` (deterministic spec normalization — no more "Target Type is
  required" errors after Apply to Canvas)

### Fixed — gateway deploy/teardown hardening (live-verified end-to-end)
- **"AddPermission … The provided principal was invalid"** on multi-target and
  multi-gateway deploys: the orphaned-permission prune was inert because the
  gateway step role lacked `lambda:GetPolicy`; granted, and the prune now warns
  instead of silently swallowing AccessDenied
- OpenAPI targets in the multi-target path no longer request
  `GATEWAY_IAM_ROLE` (AgentCore rejects it); public specs omit the credential
  block, API-key/OAuth are honored
- Shared singleton tool Lambdas (`AgentCoreDynamicTools` /
  `AgentCoreCustomerSupportTools`) are released by **reference count** on every
  teardown path (user delete, failure auto-cleanup, manifest) — tearing down
  one gateway no longer breaks other live gateways sharing the Lambda, and the
  Lambda is deleted when the last gateway releases it (including the
  empty-policy vs missing-function `ResourceNotFoundException` ambiguity)
- Failed gateway deploys release everything they provisioned (no orphan
  gateway/role/Cognito/grants)
- Bedrock Converse calls omit `temperature` for Claude Sonnet 5+ / Opus 5 /
  Fable models (param deprecated → ValidationException broke Generate Agent)
- Chat panel always renders the message input on a fresh session

## [0.1.0] - 2026-07-17

Initial public sample: visual drag-and-drop workflow builder for Amazon Bedrock
AgentCore with Step Functions-orchestrated deployment, gateway/tool wiring,
memory, knowledge bases, guardrails, observability, evaluations, enterprise
governance (RBAC/ABAC, Cedar policies, approvals, budgets), and manifest-driven
teardown.
