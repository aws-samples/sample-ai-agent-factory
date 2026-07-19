# Deployment Internals

How the platform deploys itself and your agents — the Step Functions pipeline, gateway tool plumbing, code generation, CloudFormation export, packaging, templates, and teardown.

[← Back to README](../README.md)

## Architecture Components

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

Lambda code is packaged automatically by CDK from the `backend/` directory -- no Docker build or ECR push required.

## Agent Deployment (UI → Step Functions)

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

## Agent Deletion Flow

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
|   |   |   +-- registry.py              # Agent registry + two-persona approval (RBAC)
|   |   |   +-- prompts.py                # Prompt library (versions + promote + resolve)
|   |   |   +-- hitl.py                   # Human-in-the-loop approval queue
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
|   |   |   +-- policy_promoter.py        # Converge a fail-closed ENFORCE Cedar permit to ACTIVE in place (update_policy) on invoke/status touchpoints
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
|   |   |   +-- auth/                     # AuthWrapper (Amplify Authenticator) + login hero
|   |   |   +-- canvas/                   # WorkflowCanvas (React Flow)
|   |   |   +-- deploy/                   # DeployPanel + tabs: VersionsList, EvaluationResultsPanel,
|   |   |   |                             #   ObservabilityPanel, CostPanel, TriggersPanel; Publish-to-Registry; authoring-mode toggle
|   |   |   +-- harness/                  # Harness authoring form (model/instructions/memory/tools) for deploymentMode=harness
|   |   |   +-- hero/                     # Animated hero (gradient, glass badge) for login/empty-state
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
+-- docs/                                 # Documentation + architecture diagram (see README "Documentation" section)
+-- .gitignore
+-- .pre-commit-config.yaml               # Security scanning and code quality hooks
+-- .secrets.baseline                     # detect-secrets baseline (false positive allowlist)
+-- README.md
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
