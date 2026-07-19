# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
