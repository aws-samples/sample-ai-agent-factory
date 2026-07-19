# Security & Hardening

Infrastructure, IAM, tenant-isolation, and operational hardening applied throughout the platform.

[← Back to README](../README.md)

## Infrastructure Hardening

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

## CDK-NAG (AWS Solutions Checks)

CDK-NAG (`cdk_nag.AwsSolutionsChecks`) runs during every `cdk synth` to flag security best-practice violations. Suppressions are scoped per-construct via `NagSuppressions.add_resource_suppressions(<construct>, [...], apply_to_children=True)` in `PlatformStack._apply_nag_suppressions()` — never stack-wide. Each suppression names the specific construct that legitimately needs the exception (e.g. shared runtime exec role for IAM4/IAM5, Cognito user pool for COG2/COG4/COG8, the State Machine for SF1) so a future contributor adding a wildcard policy to an unrelated construct fails the build instead of silently absorbing the finding. Unsuppressed violations cause synthesis to fail.

## Reliability & Operational Hardening

- **RemovalPolicy gating** — DynamoDB tables, S3 buckets, and the Cognito user pool default to `RETAIN` in production. Set `ENVIRONMENT_NAME` to one of `dev|test|sandbox|preview|ephemeral` (or export `AGENTCORE_ALLOW_DESTROY=true`) to switch to `DESTROY` + `auto_delete_objects=True` for fast iteration. Guards against accidental data loss on `cdk destroy` against a long-running prod stack.
- **runtime_id-index GSI** — `DeploymentsTable` has a GSI keyed on `runtime_id` so `DELETE /api/runtime/{id}` and `POST /api/test-runtime` resolve via O(1) Query instead of O(N) Scan. Falls back to paginated Scan when the GSI is absent (covers stacks deployed before the GSI was added).
- **Cleanup-failure aggregation** — `handle_delete_runtime` tracks per-resource cleanup failures (`mcp_server_runtime`, `policy_engine`, `memory`, `guardrail`, `gateway`, `kb_lambda`, `knowledge_base`) and only returns `success=true` when **all** cleanups succeed. Prevents reporting success when a Cognito pool / KB / guardrail leaks.
- **Idempotent guardrail creation** — `guardrails_step` catches `ResourceAlreadyExistsException` from `create_guardrail`, then either updates the existing guardrail in place or retries with a UUID-suffixed name. Step Functions retries no longer break a partially-deployed flow.
- **Gateway deploy rollback** — `deploy_gateway` tracks partial state (Cognito client info, gateway ID, tool Lambdas, custom-tool roles) and runs `cleanup_gateway_resources` on any mid-flow exception before re-raising. No more orphan Cognito pools / Lambdas after a transient AgentCore error.
- **SSRF guard on Gateway URL fetches** — Any URL the gateway deployer follows is validated before `urlopen` against a 21-network IPv4/IPv6 denylist (loopback, link-local incl. IMDS `169.254.169.254` and Lambda creds `169.254.170.2`, RFC1918, CGNAT, multicast, ULA, IPv4-mapped IPv6). DNS resolution is performed up-front so hostname rebinding (`evil.com → 169.254.169.254`) cannot bypass the check. Optional `OIDC_DISCOVERY_HOST_ALLOWLIST` env var pins discovery hosts to an operator-approved set. Same defense applied to the embedded `_do_fetch_webpage` tool Lambda.
- **OTEL secret namespace lock** — User-supplied `auth_header_secret_arn` (per-canvas Observability node) is validated against `^arn:aws:secretsmanager:.*:secret:agentcore-otel/.*` before being granted to the runtime IAM role. Foreign ARNs are rejected at the API boundary; tenant cannot trick the runtime into reading + exfiltrating arbitrary secrets via OTLP headers. Secrets created via `POST /api/observability/credentials` are tagged with `owner_sub` (Cognito sub) so cross-tenant ownership is auditable.
- **Tenant isolation hardening** — The `X-Test-Sub` header bypass is removed from `services/auth.py`; tests inject sub via FastAPI `dependency_overrides`. `assert_owner` returns 404 for None-owner records (no legacy-data bypass). Flow/workflow listing uses strict `owner_sub == caller_sub` equality (no None-coalescing fallback that previously surfaced legacy rows in every tenant's list).
- **MCPClient wiring proof gate** — When a runtime is configured with `GATEWAY_URL` but `MCPClient.list_tools_sync()` returns an empty list, the runtime raises `RuntimeError("Gateway MCPClient returned 0 tools…")` at first invocation rather than letting the agent bluff a canary out of the system prompt. This makes silent gateway-wiring failures (an agent that "passes" tests without ever reaching its tools) structurally impossible.
- **Cedar ENFORCE policy enforcement** — Fail-closed, converge-in-place Cedar policy enforcement on Gateway tools. See the full write-up in [Enterprise Capabilities — Cedar ENFORCE](ENTERPRISE_CAPABILITIES.md#agent-lifecycle--quality).
- **DDB GSI NULL-key safety** — `DeploymentState` serializer omits None-valued optional fields (`runtime_id`, `gateway_url`, `completed_at`, etc.) via `model_dump(mode="json", exclude_none=True)` so the `runtime_id-index` GSI accepts the initial intake write. Pairs with the runtime_id-index GSI for cost-bounded delete/test/invoke lookups.
- **Frontend ErrorBoundary** — `frontend/src/components/ErrorBoundary.tsx` wraps the app root. A render-time exception shows a recoverable banner with reset/reload buttons instead of a blank screen.
- **Auto-save error toast** — `useAutoSave` exposes `lastSaveError` so a transient save failure renders a dismissable toast instead of being clobbered by a subsequent successful read.

## Pre-commit Hooks

`.pre-commit-config.yaml` includes:
- `detect-secrets` -- Prevents accidental secret commits (API keys, passwords) with a baseline file
- `detect-private-key` -- Blocks commits containing private keys
- `check-added-large-files` -- Rejects files over 1MB
- `no-commit-to-branch` -- Prevents direct commits to `main`
- `ruff` -- Python linting and formatting checks
- Standard checks: trailing whitespace, end-of-file fixer, YAML/JSON validation, merge conflict markers

Install with `pip install pre-commit && pre-commit install`. Run manually with `pre-commit run --all-files`.

## Static analysis

The codebase is scanned with static-analysis security tooling; known hardening items are tracked as issues.
