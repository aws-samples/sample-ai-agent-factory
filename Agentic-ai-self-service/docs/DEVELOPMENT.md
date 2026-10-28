# Local Development & Testing

Running the platform locally, the full test-suite matrix, and the tech stack.

[← Back to README](../README.md)

## Quick start (Makefile)

```bash
make install   # backend + infra + frontend deps
make dev       # both dev servers (UI on :5173, API on :8000)
make test      # backend + infra + frontend test suites
make lint      # ruff + eslint
make typecheck # pyright + tsc
```

Run `make help` for the full target list.

## Local Development

For contributors who want to run the platform locally without deploying to AWS. The backend falls back to in-memory storage when `DYNAMODB_TABLE_NAME` is not set **and** the process is not running inside Lambda (detected via `AWS_LAMBDA_FUNCTION_NAME`). Inside Lambda the missing table env var raises `RuntimeError` at module load, so a misconfigured deploy fails to initialize rather than silently dropping writes.

```bash
# Backend
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,deploy]"
cp .env.example .env
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# Frontend
cd frontend
npm install
cp .env.example .env
npm run dev
```

The UI opens at `http://localhost:5173`. The backend API runs at `http://localhost:8000`.

Note: Local mode uses in-memory storage (workflows are lost on restart) and requires AWS credentials for agent deployment features.

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
python3 -m pip install -r requirements-dev.txt
pytest tests/ -v

# Exercise the same CLI synthesis path deploy.sh uses.
export CDK_PYTHON="$(command -v python3)"
npx cdk synth --quiet
```

Verifies the synthesized CloudFormation template contains expected serverless resources (API Gateway, Lambda, Step Functions, DynamoDB) and does NOT contain removed resources (VPC, ECS, ALB, ECR, CodeBuild).

Use the same Python interpreter for dependency installation and synthesis.
`npx` may otherwise prepend another Python installation to `PATH`. `cdk.json`
honours `CDK_PYTHON`, `deploy.sh` and `cleanup.sh` set it automatically, and the
CDK app fails before synthesis if that interpreter's `aws-cdk-lib`,
`constructs`, or `cdk-nag` version differs from the exact pin in
`infra/requirements.txt`.

### Integration Tests

Integration tests perform real AWS API calls with zero mocking. They require:
- Valid AWS credentials with permissions for API Gateway, Lambda, Step Functions, DynamoDB, AgentCore, IAM, Cognito
- A deployed stack (run `./scripts/deploy.sh` first)
- Environment variables: `API_GATEWAY_URL`, `AWS_REGION`, and a real Cognito
  access token with `agent:read`, `agent:write`, `invoke`, `trigger:read`,
  `trigger:write`, `cost:read`, and `cost:write` scopes. For the trigger-delivery
  matrix, issue this token to one synthetic user in `g-users-default`, `t-user`,
  and `g-admins-cost`. Agent budgets are bound to their writer's `owner_sub`, so
  a separate FinOps identity would not authorize this user's invocation.
  `g-admins-cost` grants no trigger capability, so trigger authorization still
  comes only from `g-users-default`. The matrix registers cleanup before creating
  its unique agent-scoped budget and verifies deletion in `finally`.
- The trigger-delivery matrix also requires direct test-runner permission to
  read the platform triggers table, publish a custom EventBridge event, inspect
  EventBridge rules and webhook secrets, query shared `aws/spans`, and
  create/delete one temporary private S3 bucket. Set
  `INTEGRATION_TRIGGERS_TABLE_NAME` to the exact deployed table name (normally
  `<project>-<environment>-triggers`). CloudWatch Transaction Search is an AWS
  prerequisite for Batch Evaluation. By default the matrix fails closed unless
  the Region is already `ACTIVE/CloudWatchLogs` with indexing above 0%. A
  controlled throwaway account may set `INTEGRATION_MANAGE_TRANSACTION_SEARCH=true`
  plus an absolute `INTEGRATION_TRANSACTION_SEARCH_STATE_FILE`; the fixture then
  creates one exact X-Ray-to-Logs resource policy, raises indexing to 100%,
  waits AWS's documented ten-minute first-enable search-readiness window, and
  only then lets the test generate evaluation sessions. Teardown restores and
  verifies the captured destination/indexing and policy absence. The durable
  state file is retained if restoration cannot be proven; set `INTEGRATION_RESTORE_TRANSACTION_SEARCH_STATE=true` on the next run to
  validate that file's account, Region, schema, ranges, mutation flags, and
  policy ownership, restore it, and only then acquire a fresh lease. Recovery
  refuses an unrelated or modified resource policy before changing any
  account-global X-Ray setting. In production-like environments the matrix
  invokes ordinary low-entropy factual prompts through the owner-checked
  staging slot of an immutable production-gated version. Only that trusted
  route adds a private marker; generated code
  strips it before agent execution and emits one content-bearing `invoke_agent`
  span with the documented `gen_ai.task.input/output` attributes. Framework
  capture remains redacted. After promotion, the matrix invokes a unique
  production canary and proves its prompt and expected response phrase are absent
  from every correlated shared-span record before testing trigger delivery. It also rejects Guardrail
  redaction placeholders, waits for exact session ID, documented
  `<AgentCoreRuntimeName>.DEFAULT` service name, trace ID, and span ID, then
  waits the documented ingestion-settle window before starting and attesting
  Batch Evaluation and promoting the exact version.

```bash
cd backend

# Set required environment variables
export API_GATEWAY_URL="https://XXXXXXXXXX.execute-api.us-east-1.amazonaws.com"
export AWS_REGION="us-east-1"
export API_BEARER_TOKEN="<Cognito access token for g-users-default + t-user + g-admins-cost>"
export INTEGRATION_TRIGGERS_TABLE_NAME="<project>-<environment>-triggers"
# Throwaway-account automation only: capture, enable, then restore Transaction Search.
export INTEGRATION_MANAGE_TRANSACTION_SEARCH="true"
export INTEGRATION_TRANSACTION_SEARCH_STATE_FILE="$PWD/../build/transaction-search-state.json"
# Recovery only after an interrupted/failed cleanup retained that exact state file:
# export INTEGRATION_RESTORE_TRANSACTION_SEARCH_STATE="true"
# Optional: raise the default 12-minute per-delivery wait for a slow account.
export INTEGRATION_TRIGGER_TIMEOUT_SECONDS="900"

# Run integration tests only
pytest -m integration -v

# Run a specific integration test
pytest -m integration tests/integration/test_deployment_lifecycle.py -v
pytest -m integration tests/integration/test_template_deployments.py -v
pytest -m integration tests/integration/test_trigger_delivery_matrix.py -v
```

The gallery matrix deploys all six built-in templates. HTTP-agent templates are
invoked through `/api/test-runtime`; the standalone MCP server is exercised
through `/api/test-mcp-runtime/tools` and `/api/test-mcp-runtime/call`, including
every advertised tool and an unknown-tool refusal. The tests use
template-specific semantic response oracles rather than accepting any non-empty
model response. Every accepted deployment is registered for cleanup immediately,
and the run fails unless its durable deployment record reaches
`delete_status=deleted`.

The trigger matrix deploys one HTTP runtime and creates cron, custom
EventBridge, S3, and signed webhook triggers through the authenticated product
API. It fires each real source and requires a completed DynamoDB delivery row;
that marker is written only after the AgentCore invocation returns. The signed
webhook is then replayed byte-for-byte with the same timestamp, delivery ID, body,
and HMAC: the first response must report `duplicate=false`, the byte-identical replay
`duplicate=true`, and the same ID with different validly signed bytes must return exact
`409`; only one worker delivery may complete. Every conversational lifecycle/gallery
case and the trigger matrix creates a unique owner-bound agent budget before its first
Runtime invocation, registers the key before the write, and deletes and
absence-checks it during teardown. The trigger matrix then deletes each trigger
through the product API, proves the EventBridge rule or webhook secret is no longer
active, removes the temporary S3 bucket, and requires the runtime's durable deleted
tombstone. Completed worker-delivery rows and queued webhook
ingress markers remain only as seven-day, TTL-bounded deduplication evidence.

### Live verification scripts

Standalone probes for the paths whose unit tests can only assert what we *believe*
an external system returns. Each drives the shipped product code against the real
thing and prints a PASS/FAIL line per check, exiting non-zero on any failure.

| Script | Proves | Needs |
|---|---|---|
| `scripts/verify-external-mcp.py <catalog-slug>` | A real AgentCore Gateway targeting a real external MCP, invoked end-to-end, then torn down | AWS credentials |
| `scripts/verify-mcp-protocol.py` | For every explicitly configured protocol version, a product-created MCP endpoint rejects missing/invalid auth, validates legacy initialize/session or 2026-07-28 stateless discovery/routing, paginates structurally valid `tools/list` descriptors, checks required fields and supported types in result content blocks, executes every configured canary call, and rejects unknown tools without counting a server crash as fail-closed | `MCP_VERIFY_URL`, `MCP_VERIFY_BEARER_TOKEN`, `MCP_VERIFY_PROTOCOL_VERSIONS_JSON`, and a non-secret `MCP_VERIFY_CALLS_JSON` matrix |
| `scripts/verify-litellm.py [base_url] [key]` | The LiteLLM gateway + registry path against a real LiteLLM proxy: payload shapes, parsers, fail-loud readiness gate, catalog projection, governance gate, sidecar merge | A LiteLLM proxy (setup recipe is in the script's docstring); `AGENT_REGISTRY_TABLE_NAME` to also exercise the real merge |
| `scripts/verify-otel.py` | Platform OTEL wiring reaches the configured OTLP endpoint | A deployed stack with `OTEL_ENDPOINT` set |

The product currently pins MCP `2025-11-25`, `2025-06-18`, and `2025-03-26`.
Keep the verifier's stricter `2026-07-28` path for re-testing the managed
service; do not advertise that version until its live `server/discover`
response includes the mandatory `resultType`.

`verify-litellm.py` issues only reads and refused writes, so it is safe to point at
a live registry table. See
[MCP Gateway Integration](MCP_GATEWAY_INTEGRATION.md#live-verification) for what it
found that mocks could not.

### Frontend Tests

```bash
cd frontend
npm install
npm test
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 19, @xyflow/react 12, Zustand 5, Tailwind CSS 4, Vite |
| Backend | FastAPI, Mangum, Pydantic 2, boto3 |
| Agent Framework | Strands Agents SDK (strands-agents, strands-agents-tools) |
| Agent Runtime | BedrockAgentCoreApp for HTTP agents; standalone FastMCP for protocol-native MCP runtimes |
| Model Providers | Bedrock (default), OpenAI, Anthropic, Gemini, Mistral, Ollama, Groq, DeepSeek, Together, LiteLLM, SageMaker, Writer, LlamaAPI |
| Multi-Agent | strands.multiagent (Graph, Swarm, Workflow patterns) |
| Orchestration | AWS Step Functions (Standard Workflows) |
| Testing | Pytest + Hypothesis (backend properties), Pytest + real AWS (integration), Vitest + fast-check (frontend), CDK assertions (infra) |
| Deployment Target | AWS Bedrock AgentCore (Runtime, Gateway, Knowledge Base, Memory, Evaluation, Policy, Browser, Identity, Observability) |
| Platform Infrastructure | AWS CDK (Python), API Gateway HTTP API, Lambda, Step Functions, DynamoDB, S3, CloudFront, SSM, Cognito |

## Future Enhancements

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
