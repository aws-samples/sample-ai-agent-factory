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
| Deployment state | DynamoDB | Durable deployment/ownership records; deleted tombstones expire after 30 days |
| AgentCore services | Bedrock AgentCore | Runtime, Gateway, Memory, Knowledge Base, Evaluation, Policy, Observability |
| Configuration | SSM Parameter Store | Runtime config under `/agentcore-workflow/{env}/` |
| Logging | CloudWatch Logs | Lambda and Step Functions execution logs |
| Platform infrastructure | AWS CDK (Python) | Single stack under `infra/`, all platform resources defined as code |
| Exported agent stacks | Raw CloudFormation YAML | Hand-built template emitted by `cfn_template_generator.py` — **not** CDK, no `cdk synth` |

### Two different infrastructure-as-code surfaces

These are frequently confused, so to be explicit:

| | How the **platform** is deployed | What the platform **emits for customers** |
|---|---|---|
| Tooling | AWS CDK (Python), `infra/` | `CfnTemplateGenerator`, `backend/src/app/services/cfn_template_generator.py` |
| Produced by | `npx cdk deploy` (synthesizes a template) | A hand-built Python `dict` serialized with `yaml.dump` |
| Consumer | The team operating this platform | An external customer, with no access to this repo |
| CDK bootstrap needed | Yes | No |
| Custom resources | CDK-synthesized helpers (`Custom::LogRetention`, `Custom::S3AutoDeleteObjects`, `Custom::CDKBucketDeployment`) plus a Cognito user provisioner in `infra/stacks/platform/cognito_auth.py` | Exactly four, all served by `cfn_provider/handler.py` — see [CloudFormation Export](#cloudformation-export) |

There is **no CDK path for the exported agent stack**, and none is planned. The export is
deliberately plain CloudFormation so that customers can consume it with their own tooling
(Terraform's `aws_cloudformation_stack`, CloudFormation StackSets, or the CLI) without
taking a CDK dependency or needing a bootstrap stack.

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
   Creates backend/agentcore-deps/mcp-lean.zip (standalone FastMCP, no Strands/model stack)

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
    │   Records the runtime, then applies 30-day retention to its DEFAULT
    │   AgentCore CloudWatch log group before waiting for readiness
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
    │   Applies 30-day retention to each tool Lambda's /aws/lambda/<function>
    │   log group before that function's Gateway Target exists
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
    │       gateway/memory results above; records harness_id/arn; then creates/adopts
    │       /aws/bedrock-agentcore/runtimes/<backing-runtime-id>-DEFAULT for the runtime
    │       AgentCore hosts the harness on and applies 30-day retention, fatal on
    │       failure). SKIPS codegen,
    │       IAM, ConfigureRuntime, and LaunchRuntime, then rejoins at the
    │       evaluation/auth/status_update tail.
    │   Otherwise (runtime, default) → GenerateCode (Step 12 below).
    │
    ├── Step 12: GenerateCode (30s timeout)   [runtime mode]
    │   Generates Strands Agent code (provider-aware model init + multi-agent)
    │   Merges gateway credentials into code (from gateway step output)
    │   Downloads the classified dependency bundle from S3
    │     (base.zip, strands-mcp.zip, or mcp-lean.zip)
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
    │   Records the exact runtime ID, then creates/adopts
    │     /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT
    │     and applies 30-day retention; failure is fatal
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
    │   ├── bedrock-agentcore-control.delete_memory(), then GetMemory polled to absence
    │   └── A Memory still DELETING when one invocation's ~6-minute confirmation
    │       budget ends is not a retention: the teardown runs again in a fresh
    │       background invocation (up to four more, about 30 minutes in all) while
    │       the record stays "deleting", and only then records delete_retained
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
    │   ├── Delete Cognito User Pool
    │   └── Tool Lambda log groups are left in place with their retention
    │       policy, like the runtime's below
    │
    ├── Destroy Agent Runtime
    │   bedrock-agentcore-control.delete_agent_runtime()
    │   Runtime log groups are left in place with their retention policy so
    │   deleting a deployment does not erase its audit trail
    │
    └── Update DynamoDB status → DELETED
```

## Dynamic Gateway Tool-to-Lambda Pipeline

This is the core innovation of the platform. When a user creates a custom deployment and selects tools (DuckDuckGo Search, Wikipedia, Weather, Web Page Fetcher), those tools are **not embedded** as Python functions inside the agent code. Instead:

```
User selects tools in UI
    |
    v
Backend selects the matching Lambda family:
  "AgentCoreDynamicTools" for web + canonical order tools, or
  "AgentCoreCustomerSupportTools" for the legacy assistant contract
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

| Canvas tool ID | Gateway-advertised name | Lambda/target family | Implementation |
|----------------|-------------------------|----------------------|----------------|
| `duckduckgo_search` | `duckduckgo_search` | DynamicTools | DuckDuckGo Instant Answer API |
| `wikipedia_search` | `wikipedia_search` | DynamicTools | Wikipedia REST API |
| `weather_api` | `get_weather` | DynamicTools | Open-Meteo Geocoding + Weather API |
| `web_page_fetcher` | `fetch_webpage` | DynamicTools | stdlib `urllib.request` |
| `get_order` | `get_order` | DynamicTools | Mock order database |
| `get_customer` | `get_customer` | DynamicTools | Mock customer database |
| `list_orders` | `list_orders` | DynamicTools | Mock order listing |
| `process_refund` | `process_refund` | DynamicTools | Mock refund processing |
| `check_order_status` | `check_order_status` | CustomerSupportTools | Legacy assistant order-status lookup |
| `lookup_customer` | `lookup_customer` | CustomerSupportTools | Legacy assistant customer lookup |
| `search_knowledge_base` | `search_knowledge_base` | CustomerSupportTools | Legacy assistant support-article search |
| `get_return_policy` | `get_return_policy` | CustomerSupportTools | Legacy assistant return-policy lookup |
| `knowledge_base` | `knowledge_base_query` | Per-deployment KB target | Bedrock Knowledge Base RetrieveAndGenerate |

The search/weather/customer tools use **zero external dependencies** -- only Python standard library. No Lambda layers, no custom runtimes, instant cold starts. The Knowledge Base tool uses boto3 (bundled in the Lambda runtime).

### How Tool Schemas Work

Each tool has an MCP-compliant schema registered in `GATEWAY_TOOL_SCHEMAS`:

```python
GATEWAY_TOOL_SCHEMAS = {
    "duckduckgo_search": {
        "name": "duckduckgo_search",
        "description": "Search the web using DuckDuckGo...",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The search query"}},
            "required": ["query"],
        },
    },
    # ... wikipedia_search, weather_api, web_page_fetcher
}
```

When deploying, only schemas executable by the selected Lambda family are
included. The dedicated Knowledge Base target is never advertised through
`AgentCoreDynamicTools`:

```python
selected_schemas = [
    GATEWAY_TOOL_SCHEMAS[tid] for tid in gateway_tools if tid in GATEWAY_TOOL_SCHEMAS and tid != "knowledge_base"
]
# This list goes into the gateway target's toolSchema.inlinePayload
```

## Agent Code Architecture

Generated runtime artifacts have two distinct protocol families:

1. **HTTP conversational runtimes** use `BedrockAgentCoreApp` for the AgentCore
   HTTP contract. Depending on the selected template/provider, the artifact
   uses a Strands Agent or a boto3 Converse tool loop. Provider-aware artifacts
   initialize the selected Bedrock, OpenAI, Anthropic, Gemini, or other supported
   model adapter.
2. **Standalone MCP runtime (Template 6)** uses `FastMCP` with streamable HTTP on
   port 8000. It exposes typed tools directly and deliberately instantiates no
   language model, Strands Agent, or Converse loop.
3. **Multi-agent orchestration** applies only to compatible HTTP runtimes. Graph,
   Swarm, and Workflow patterns are generated with `strands.multiagent`.

For gateway-enabled agents (Templates 2-5):
- Tools are discovered at startup via MCP `initialize` -> `notifications/initialized` -> `tools/list`
- OAuth2 tokens are acquired via `client_credentials` grant (Cognito)
- Gateway credentials are injected as environment variables by the runtime configure step (`COGNITO_*`)

For MCP Server Gateway Target (Template 5):
- An HTTP Agent Runtime calls an AgentCore Gateway
- The Gateway targets a second Runtime that serves the order tools over MCP
- OAuth2 authenticates Gateway-to-MCP-runtime discovery and invocation

For the standalone MCP Server Runtime (Template 6):
- Weather, search, and SSRF-guarded URL-fetch functions are registered as
  `FastMCP` tools
- No Gateway, Lambda, or Cognito resources are created
- Product discovery and calls use `/api/test-mcp-runtime/tools` and
  `/api/test-mcp-runtime/call`; the generic HTTP chat endpoint refuses this
  protocol instead of forwarding an incompatible request

Provider-specific packages are determined at code generation time from `PROVIDER_PACKAGES` mapping (e.g., `openai` provider adds `strands-agents strands-agents-tools openai`). Dependencies are bundled into `code.zip` at deploy time from pre-built dependency bundles, avoiding any pip-install during the 30-second AgentCore init window.

## Live-Deploy Authority Boundary

The durable `POST /api/deploy` path validates caller-supplied AWS references
before Step Functions receives them. An ARN that parses correctly identifies a
resource; it does not authorize the platform to read, mutate, or grant access to
that resource.

### Runtime credentials

Model-provider and per-canvas OTEL source secrets are live-described in their
source account. User-managed sources must be in the expected namespace and carry
the exact `AgentCoreStack` and `OwnerSubHash` bindings. The operator-configured
platform OTEL source is trusted only because its ARN comes from server-side SSM
configuration, not the request. Each accepted value is copied unchanged to a
new target-account `agentcore-connector/*` secret tagged with:

- `AgentCoreStack`
- `OwnerSubHash`
- `DeploymentId`
- `Purpose`

The deployment manifest records only that copy. Step Functions, the runtime IAM
policy, generated code, and teardown all consume the copied ARN; the long-lived
source ARN is not granted to the runtime and is never deleted as part of the
agent lifecycle. If staging or the manifest write fails, already-created copies
are compensation-deleted before the workflow starts.

### Customer-owned Knowledge Base resources

The owner must tag each customer resource
`AgentCoreFlowsAccess=allow`. An optional `OwnerSubHash` narrows the opt-in to
one authenticated caller. The Knowledge Base step performs a live service read
before its first IAM mutation and checks the target account/region and ARN
resource family where applicable. The checks cover:

- existing Bedrock Knowledge Bases
- S3 data buckets
- S3 Vectors buckets and their pre-created `float32` / 1024-dimension /
  cosine-distance indexes
- OpenSearch Serverless collections and compatible vector indexes
- Aurora PostgreSQL clusters
- transformation Lambda functions
- KMS keys
- Confluence, Salesforce, SharePoint, and RDS credential source secrets

Customer credential source secrets are copied into a deployment-bound target
secret before the state-machine input is serialized. The Bedrock Knowledge Base
role receives only the copied ARN. Platform-created KB resources use the
stronger `AgentCoreStack` + `DeploymentId` + caller binding, including on
same-name conflict and retry paths.

The Bedrock Knowledge Base service role also has a confused-deputy guard in its
trust policy (`aws:SourceAccount` and a region/account-scoped Knowledge Base
`aws:SourceArn`). For S3 Vectors it receives only the selected index's data-plane
actions; bucket/index provisioning remains with the deployment step role.

These controls apply to the live platform deployment path. The CloudFormation
export below is a customer-owned artifact whose roles and resource parameters
are evaluated in the account where the bundle is deployed.

## CloudFormation Export

Any template or free-form diagram can be exported as a self-contained CloudFormation stack. The export generates a downloadable zip containing everything an external user needs to deploy the agent without access to the platform.

### What's in the Download

| File | Purpose |
|------|---------|
| `template.yaml` | CloudFormation template with all AWS resources |
| `agent-code/agent.py` | Generated agent code |
| `cfn-provider.zip` | Custom Resource Lambda backing all four custom resources (code packaging, runtime log group governance, OAuth2 credential provider, Cedar policy) |
| `tool-lambdas.zip` | Gateway tool Lambda implementations (if gateway tools are used) |
| `custom-tools.zip` | AI-generated/custom tool Lambda implementations (if custom tools are used) |
| `agent-code/mcp_server.py` | Generated MCP server code (MCP Server pattern only) |
| `build-dependency-bundle.sh` | Reproducible aarch64/Python 3.13 dependency-bundle builder |
| `deploy.sh` | One-command deploy script (`./deploy.sh <stack-name> <region> <s3-bucket>`) |
| `teardown.sh` | One-command teardown script (`./teardown.sh <stack-name> <region>`) |
| `README.md` | Setup instructions and prerequisites |

### How It Works

1. **User clicks "Download CloudFormation"** in the deploy panel (or calls `POST /api/generate-cfn-template`)
2. `CfnTemplateGenerator.generate()` builds:
   - A CloudFormation template with AgentCore Runtime, Gateway, Cognito, Memory, Evaluation, and IAM resources as needed
   - A `Custom::AgentCodePackage` resource (backed by the cfn-provider Lambda) that downloads a pre-built dependency bundle from S3, merges the agent code into it, and uploads the final `code.zip` at deploy time
   - A target-platform dependency recipe; `deploy.sh` builds and uploads the bundle automatically when it is not already in the selected S3 bucket
3. The download zip is returned to the browser
4. The external user runs `./deploy.sh my-agent us-east-1 my-s3-bucket` to deploy

Full component templates can exceed CloudFormation's 51,200-byte inline template
limit. The generated `deploy.sh` therefore always passes its artifacts bucket to
`aws cloudformation deploy --s3-bucket` and stages the template under
`cfn-assets/<stack-name>/cloudformation/`; `teardown.sh` already purges that
stack-owned prefix.

For Terraform, do not use `aws_cloudformation_stack.template_body = file(...)`.
Upload `template.yaml` to S3 and use `template_url`, then stage the remaining bundle
artifacts and supply the same code keys and digests shown in the generated parameter
table. The generated README contains a minimal HCL wrapper. This matters even if a
small runtime-only export currently fits inline: adding a gateway, MCP server,
knowledge base, evaluation, or tools can take the same Terraform module over the
limit.

### Custom Resources in the Exported Stack

The exported template is plain CloudFormation and uses native `AWS::BedrockAgentCore::*`
types wherever they exist. Four things cannot be expressed natively, so they are Custom
Resources. All four are served by the **single** `cfn-provider.zip` Lambda that the
template creates, and the dispatch lives in `backend/src/app/services/cfn_provider/handler.py`:

| Custom resource | When emitted | What it does |
|-----------------|--------------|--------------|
| `Custom::AgentCodePackage` | Always | Downloads the prebuilt dependency bundle, merges the generated agent code into it, uploads the final `code.zip`, and writes the effective governance tags onto that S3 object. Deletes the object on stack delete. |
| `Custom::RuntimeLogGroup` | Always: one per log group (`-DEFAULT` plus one per endpoint) plus one `Mode: sweeper` per runtime | Applies `LogRetentionInDays`, optional `CustomerManagedKeyArn`, and the effective governance tags to the CloudWatch log groups AgentCore creates for the runtime. Tag changes are read back and verified. Its Delete deletes and verifies its group, but CloudFormation only sends it when the export's retention stamping says so: under Delete with the stack, for a replaced runtime's old groups after a successful update, and on the rollback of a failed first create; under Retain a stack delete skips it. |
| `Custom::OAuth2CredentialProvider` | MCP-server path only | Creates the AgentCore OAuth2 credential provider that authenticates the Gateway to the MCP Server Runtime, reconciles and verifies its effective governance tags, and computes the URL-encoded MCP endpoint (CFN has no url-encode intrinsic). |
| `Custom::AgentCorePolicy` | When a policy is configured | Creates the Cedar policy attached to the PolicyEngine, with idempotent reuse if the policy already exists. Policy children do not support tags; the owning `AWS::BedrockAgentCore::PolicyEngine` carries them. |

`Custom::RuntimeLogGroup` is the one that is not about a missing CFN type.
`AWS::BedrockAgentCore::Runtime` has no logging or encryption properties at all, and the
service creates `/aws/bedrock-agentcore/runtimes/<runtimeId>-<endpointName>` itself at
stack-create time — one group per endpoint, including `-DEFAULT` — with no retention and
no customer key. Those groups hold what the agent was asked and answered. They cannot be
declared as `AWS::Logs::LogGroup` either: by the time the stack could adopt them they
already exist and belong to nobody, which fails the create with "already exists". So the
resource creates-or-adopts its group by name. One resource governs one group and its physical id
derives from the group name, so a replaced runtime replaces exactly the affected resources and
`UpdateReplacePolicy` governs the old groups; under Retain a stack delete skips the resource and the
audit trail survives (`teardown.sh` prints the `aws logs delete-log-group` command for removing them
deliberately), under Delete the groups go with the stack.

Deletion order is handled by the sweeper: every per-group resource GetAtts its runtime, so CloudFormation
deletes it before the runtime -- while AgentCore can still write and recreate the group. The `Mode: sweeper`
resource is what the runtime `DependsOn` (created first, deleted last); its `Generation` is a digest of
`AgentRuntimeName` (the runtime's replacement property), so a renamed runtime replaces its sweeper and the
old one runs after the old runtime. Per-group resources record their resolved names as one SSM parameter
each under `/agentcore-cfn/<stack>/<stack-id-digest>/<runtime>/<generation>/groups/`; on Delete the sweeper
reads them (or, if none were recorded, recovers the runtime id from the stack's own resource record and
refuses unless it carries this generation's name), deletes each group, waits, re-checks until nothing comes
back, and removes the entries. Each endpoint `DependsOn` its group's governance resource so the endpoint
deletes first.

That is the complete set. Anything else matching `Custom::` in this repository belongs to
the platform's own CDK stack under `infra/` and is never shipped to a customer.

### Naming and Governance Tags

The CloudFormation route accepts a declarative naming profile and resolved governance
tags. This is the JSON-safe equivalent of a pluggable naming function:

```json
{
  "resourceTags": {
    "CostCentre": "ECB-42",
    "Environment": "production"
  },
  "tagProfile": "regulated-production",
  "namingProfile": {
    "prefix": "ecb",
    "resourceNames": {
      "gateway": "{prefix}-{deployment}-gw",
      "runtime": "{prefix}_{deployment}_agent",
      "runtimeRole": "{prefix}-{deployment}-{suffix}-runtime-role"
    }
  }
}
```

- `namingProfile` applies only to `POST /api/generate-cfn-template`. The live deploy
  and standalone Python export routes reject it rather than silently ignoring it.
- `prefix` is 1–12 lowercase alphanumeric characters and must begin with a letter.
  Leaving the profile absent preserves the legacy physical-name formulas.
- With a profile, `DeploymentName` is limited to 20 lowercase alphanumeric
  characters. User-controlled component names are shortened with a deterministic
  hash tail only when needed to satisfy the target service's limit.
- Without a profile, the legacy `AllowedPattern` remains in place and the template
  adds a component-specific `DeploymentName.MaxLength`. The generator calculates
  that ceiling from every emitted physical-name expression, including the actual
  custom-tool suffixes, and `deploy.sh` derives a value to the same limit. A rich
  canvas with a maximum-length custom tool can therefore publish a tighter limit
  than a runtime-only canvas; the resource names themselves do not change.
- Every override needs `{deployment}`. Component families also need `{component}`;
  stack-unique domains, vector buckets, and IAM roles need `{suffix}`.
- Supported resource families are:
  - identity: `cognitoUserPool`, `cognitoResourceServer`, `cognitoClient`,
    `cognitoDomain`
  - gateway/tools: `gateway`, `gatewayTarget`, `toolLambda`, `customToolLambda`
  - knowledge base: `knowledgeBase`, `knowledgeBaseDataSource`,
    `knowledgeBaseToolLambda`, `vectorBucket`, `vectorIndex`
  - AgentCore: `memory`, `policyEngine`, `policy`, `evaluation`, `runtime`,
    `runtimeEndpoint`, `guardrail`
  - MCP: `mcpCognitoUserPool`, `mcpCognitoResourceServer`, `mcpCognitoClient`,
    `mcpCognitoDomain`, `mcpCredentialProvider`, `mcpRuntime`, `mcpEndpoint`
  - roles: `runtimeRole`, `gatewayRole`, `toolLambdaRole`,
    `knowledgeBaseToolRole`, `knowledgeBaseRole`, `memoryRole`,
    `mcpRuntimeRole`, `evaluationRole`
- The generator applies each resolved name to its coupled IAM resource patterns,
  Lambda grants, OAuth scopes, runtime environment values, Cedar action IDs,
  outputs, and governed log-group qualifiers. Long component names retain a readable
  prefix plus a deterministic hash instead of being clipped into collisions.
- The effective naming map is recorded in
  `Metadata.AgentCoreFlowsNamingProfile` in `template.yaml`.
- Profile-derived role names use `{deployment}` plus the stack-unique `{suffix}` and
  do not depend on the length of the CloudFormation stack name. Legacy explicit role
  names do include that stack name. The template states their calculated safe limit
  (currently 47 characters); `deploy.sh` automatically sets
  `UseExplicitRoleNames=false` for a longer stack and refuses an explicitly forced
  unsafe value. Direct CloudFormation and Terraform callers must set that parameter
  themselves when their stack name exceeds the documented limit.
- If `tagProfile` is supplied, the API resolves it through the same tag-policy store
  used by live deployment, merges explicit `resourceTags`, enforces required/default
  tags, and then emits the concrete result. A resolution failure stops the export;
  it never returns a silently untagged bundle.
- Concrete tags are added to every generated resource type whose CloudFormation
  schema supports tags. Resources such as Cognito clients, Gateway targets, and
  Lambda permissions have no tag property; their owning pool, gateway, or function
  carries the tags instead.
- Custom Resources do not form an exemption. `ResourceTags` carries the effective set
  to the provider Lambda, which tags the merged S3 `code.zip`, the AgentCore OAuth2
  provider, and AgentCore's runtime-created log groups. OAuth and log-group updates
  preserve unrelated tags, remove only keys present in the prior stack properties,
  and read the result back before reporting success.
- The S3 object is the tightest sink at 10 tags. An 11-tag export is rejected before
  generation or staging; no tag is silently dropped. OAuth providers and log groups
  retain their 50-tag service limit, including unrelated tags already present when an
  update is reconciled.

### Prerequisites for External Users

- AWS CLI v2 configured with credentials
- An S3 bucket to host deployment artifacts
- `zip`, PyPI access, and either pip 24.2 or newer or a `python3` that can create a
  virtualenv, to build the AgentCore dependency bundle. Older pips cannot resolve the
  recipe for the runtime's Python 3.13 (the macOS Command Line Tools pip is 21.2.4), so the
  script then builds with a current pip in a temporary virtualenv. The build is pinned to
  the versions the platform deploys (`backend/agentcore-deps-constraints.txt`). The included
  `deploy.sh` invokes `build-dependency-bundle.sh` automatically when the bundle is
  absent. Restricted/offline environments can build once on an approved host and place
  the ZIP beside `deploy.sh`, or pre-stage it under the documented S3 key.

### Supported Patterns

The CFN generator supports all six built-in gallery templates and free-form
diagrams with any supported combination of:

| Component | CFN Resources Created |
|-----------|----------------------|
| Runtime only | Runtime, Endpoint, IAM Role, `Custom::AgentCodePackage` |
| + Gateway | MCP Gateway, Gateway Targets (including the gateway node's configured Lambda targets), Tool Lambda, Cognito User Pool/Client/Domain/ResourceServer |
| + Memory | AgentCore Memory, Memory IAM Role |
| + Evaluation | Online Evaluation Config, Evaluation IAM Role |
| + Knowledge Base | Bedrock Knowledge Base, Data Source, KB IAM Role, KB Tool Lambda + Target |
| + Policy Engine | Policy Engine (attached to Gateway), `Custom::AgentCorePolicy` for each Cedar policy |
| + MCP Server | Second Runtime (MCP protocol), MCP Server code, OAuth2 Credential Provider |
| LiteLLM Gateway | Runtime and endpoint configured for the external LiteLLM MCP URL, pinned server aliases, and a Secrets Manager virtual-key reference; no AgentCore Gateway, Cognito, or tool Lambda is created |

Targets configured on the gateway node itself are exported the way the platform deploys them. A Lambda
target becomes a gateway target named `cfgtgt-lambda-<index>`, serving the entry's inline tool schema or the
platform's single pass-through tool; its ARN is a `ConfiguredLambdaTarget<index>Arn` parameter that defaults
to the configured function, and the gateway role may invoke exactly that ARN (an identity grant, so the stack
never edits a function it does not own; a function in another account must also allow the role). The empty
row a new gateway node starts with is not a target.

A setting the template cannot express is refused with a 400 that names it, never dropped: guardrails, SaaS
connectors, external MCP servers, a target account or region, a substantive identity or observability block,
and an OpenAPI spec or Smithy model configured as a gateway target.

## Lambda Dependency Packaging

Lambda functions run on Amazon Linux (x86_64), so native Python packages like `pydantic-core` must be compiled for that platform -- not your local macOS/arm64. Dependencies are pre-installed into `backend/lib/` and bundled by CDK alongside the source code.

AgentCore Runtime agents run on aarch64 (ARM) with a different set of
dependencies. `install-agentcore-deps.sh` creates three pre-built bundles:

- `base.zip` — `bedrock-agentcore` + `boto3` for model/provider paths that do
  not need Strands or MCP.
- `strands-mcp.zip` — the base runtime plus Strands and MCP dependencies for
  conversational agents that consume MCP tools.
- `mcp-lean.zip` — the standalone FastMCP dependency set for protocol-native
  MCP runtimes, without a Strands or model stack.

The deployment path classifies the generated source, downloads the matching
bundle from S3, and merges it with the generated code.

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
- Tools: eight `DynamicTools` operations discovered through the MCP Gateway:
  DuckDuckGo search, Wikipedia search, weather, webpage fetch, order lookup,
  customer lookup, order listing, and refund processing
- Auth: Cognito OAuth2 (client_credentials grant)
- Components: Runtime + Gateway + Identity (16 CFN resources)

### Template 3: Customer Support Assistant (Advanced)
- Agent: BedrockAgentCoreApp + Strands Agent + MCP Gateway tools
- Tools: the four legacy `CustomerSupportTools` operations:
  `check_order_status`, `lookup_customer`, `search_knowledge_base`, and
  `get_return_policy`
- Auth: Cognito OAuth2 (client_credentials grant)
- Components: Runtime + Gateway + Identity + Memory + Observability

### Template 4: Customer Support Blueprint (Advanced)
- Agent: BedrockAgentCoreApp + Strands Agent + MCP Gateway tools
- Tools: the four canonical `DynamicTools` operations: `get_order`,
  `get_customer`, `list_orders`, and `process_refund`
- Auth: Cognito OAuth2 (client_credentials grant)
- Components: Runtime + Gateway + DynamicTools Lambda + Memory

### Template 5: MCP Server Gateway Target (Intermediate)
- Architecture: Agent Runtime → MCP Gateway → MCP Server Runtime (multi-runtime chain)
- Tools: hosted on the MCP Server Runtime, invoked through the Gateway as an MCP target
- Auth: Cognito OAuth2 between Agent and MCP Server via Gateway
- Components: 2 Runtimes + Gateway + Identity
- Use case: Decouple tool hosting from agent logic via MCP protocol
- Notes: the generated MCP server binds **port 8000** (the AgentCore MCP-runtime ingress contract) and uses a lean `mcp`-only dependency bundle so it cold-starts within the Gateway's ~30s tool-discovery probe; the gateway step pre-warms the runtime and retries `UpdateGatewayTarget`. Verified live end-to-end (target `READY`, agent calls the MCP tool through the gateway).

### Template 6: MCP Server Runtime (Intermediate)
- Runtime: standalone `FastMCP` server, not a conversational agent
- Tools: Weather (Open-Meteo), Web Search (DuckDuckGo), and SSRF-guarded URL Fetcher
- Components: MCP-protocol Runtime only; no model, Gateway, tool Lambda, or Cognito
- Dependencies: the lean `mcp` AgentCore bundle rather than a Strands/model bundle
- Test surface: product-owned MCP discovery and tool-call routes, not `/api/test-runtime`
- Use case: host typed tools directly with the smallest supported MCP runtime

### Custom Deployment (free-form)
- Framework: Strands Agents (with provider selection from 13 providers)
- Multi-agent: Optional Graph, Swarm, or Workflow orchestration patterns
- Tools: Any combination of built-in tools + AI-generated custom tools + SaaS connectors, deployed as Gateway Targets
- Components: User-configured — hand-wire any valid combination of Runtime/Gateway/Memory/Identity/Policy/Guardrails/Observability/Connectors on the canvas (no `templateId`); the backend deploys whatever is wired

### Harness Mode (config-driven, no canvas required)
- Authoring: declare model + instructions + connected gateway/connectors + memory
- Deploy: `deploymentMode: "harness"` — runs `create_harness` instead of codegen/runtime steps, reusing the shared gateway/memory steps
- Logs: the harness runs on a runtime AgentCore creates for it; that runtime's DEFAULT log group gets the platform's 30-day retention at deploy and is left to expire on teardown, like every runtime's
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
|   |   |   +-- triggers.py               # version-pinned EventBridge/S3/cron/webhook trigger lifecycle API
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
|   |   |   +-- cfn_provider/             # Custom Resource Lambda for CFN stacks (code packaging + runtime log groups + OAuth2 credential provider + Cedar policy)
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
