# API Reference & Configuration

Every API endpoint the platform exposes, plus deploy-time configuration variables, Lambda environment variables, and SSM parameters.

[← Back to README](../README.md)

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

#### AWS Agent Registry federation (opt-in)

Federates deployed agents into the **AWS Agent Registry** — a GA AWS service in
its own right (it is no longer part of `bedrock-agentcore`). Requires the backend
to run **boto3 >= 1.43.66**, the first release carrying the `agent-registry`
service models, and the `agent-registry:*` IAM actions.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/registry/aws-config` | Federation status: `{enabled, registry_id, available, sdk_supported}` |
| `POST` | `/api/registry/aws-config` | **Admin only** — enable federation with a `registry_id` (reachability validated before persisting) |
| `GET` | `/api/registry/aws-search?q=` | Discovery search across the registry (`SearchDiscoverableRegistryRecords`) |

`sdk_supported: false` means this deployment's boto3 predates the GA API, so no
`agent-registry` client can be built — a redeploy, not a configuration change.
`POST` returns `400` naming the SDK in that case rather than blaming the
`registry_id`.

#### LiteLLM as the catalog backend (opt-in)

Makes a LiteLLM proxy the **authoritative** catalog in place of the internal
DynamoDB one. Additive: the default backend is `dynamodb` and nothing above
changes until an admin activates this. See
[Registry & RBAC](REGISTRY_AND_RBAC.md#bring-your-own-registry--litellm-as-the-catalog).

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/registry/litellm-config` | Active backend + config: `{provider, configured, base_url, api_key_ref, verified, capabilities}` — never the key itself |
| `POST` | `/api/registry/litellm-config` | **Admin only** — save `{base_url, api_key, activate}`; the key is minted into `agentcore-registry/` and dropped |
| `DELETE` | `/api/registry/litellm-config` | **Admin only** — revert to the platform catalog (DynamoDB entries were never touched) |
| `GET` | `/api/registry/litellm-servers` | The MCP servers LiteLLM serves, with enablement. Returns `{configured: false, servers: []}` when LiteLLM is not configured — not an error |

`activate: false` saves and probes the config **without** switching the catalog
over, so reachability can be tested first. `verified: false` means the control
plane could not probe the proxy — normal for a VPC-private LiteLLM, since the
control plane has no VPC egress, and not an error.

Because LiteLLM has no write API for MCP server records, entries **projected from
LiteLLM** are read-only: publish, update, delete, approve, reject and clone return
**`501`** naming LiteLLM rather than silently accepting a write that would then
diverge. Entries published from a canvas live in the platform sidecar and stay
fully mutable — the limit is per entry, not per operation. `capabilities`
(including `read_only_sources`) on `GET /litellm-config` is the machine-readable
form of that. The pre-deploy governance gate stays **fail-closed**: if the catalog
cannot be read, deploys referencing an integration return `503`.

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

## Configuration

Deploy-time variables consumed by `./scripts/deploy.sh` and passed as CDK context parameters to the infrastructure stack:

| Variable | Default | Description |
|----------|---------|-------------|
| `ENVIRONMENT_NAME` | `dev` | Environment identifier (e.g., `dev`, `staging`, `prod`) |
| `AWS_REGION` | `us-east-1` | Target AWS region |
| `PROJECT_NAME` | `agentcore-workflow` | Project name used for resource naming and tagging |
| `COGNITO_USERS` | *(carried forward)* | Comma-separated emails for pre-created Cognito users (e.g., `user1@example.com,user2@example.com`). **Users are created in NO group → no scopes → read-only until you assign a persona** (see [Registry & RBAC](REGISTRY_AND_RBAC.md)). Each email becomes a custom resource whose **deletion deletes the Cognito user**, so dropping an email is how you offboard someone. Because an omitted variable is indistinguishable from an intentionally emptied one, leaving it **unset on a redeploy carries the already-provisioned users forward** rather than deleting them — `deploy.sh` prints a warning naming them. Pass `COGNITO_USERS=none` to genuinely remove them all. A re-provisioned user gets a **new emailed temporary password** and loses their group memberships. This carry-forward lives in `scripts/deploy.sh`, so bypassing it — running `npx cdk deploy` or `cdk diff` directly — plans the deletion again; pass `--context cognito_users=a@b.com,...` yourself in that case. |
| `OTEL_ENDPOINT` | *(unset)* | OTLP HTTP endpoint for platform-level observability (e.g. `https://cloud.langfuse.com/api/public/otel`). When set, every platform Lambda + every deployed agent exports traces here. Per-canvas Observability nodes can still add resource attributes additively but cannot override the endpoint. |
| `OTEL_AUTH_SECRET_ARN` | *(unset)* | ARN of a Secrets Manager secret holding the precomputed `Authorization` header value (e.g. `Basic <base64>`). Created by `scripts/bootstrap-otel-secret.sh`. Required when `OTEL_ENDPOINT` is set. |
| `OTEL_SAMPLE_RATE` | `1.0` | Trace sampling ratio (0.0–1.0). |
| `OTEL_SERVICE_NAME_PREFIX` | `{PROJECT_NAME}` | Prefix prepended to `service.name` resource attribute on every span. |

## Environment Variables (Lambda)

| Variable | Description |
|----------|-------------|
| `DEPLOYMENT_TABLE_NAME` | DynamoDB table name for deployment state |
| `WORKFLOWS_TABLE_NAME` | DynamoDB table name for workflow definitions |
| `STATE_MACHINE_ARN` | Step Functions state machine ARN for deployment orchestration |
| `APP_AWS_REGION` | AWS region for service calls |
| `TOOL_GENERATOR_MODEL_ID` | Claude model ID for AI Tool Generator (default: `us.anthropic.claude-sonnet-5`) |

## SSM Parameters

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
