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
| `COGNITO_USERS` | *(none)* | Comma-separated emails for pre-created Cognito users (e.g., `user1@example.com,user2@example.com`). **Users are created in NO group → no scopes → read-only until you assign a persona** (see [Registry & RBAC](REGISTRY_AND_RBAC.md)). |
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
