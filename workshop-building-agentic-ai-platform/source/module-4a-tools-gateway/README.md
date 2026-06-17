# Module 4: AgentCore Gateway Integration Layer

## Overview

Module 4 adds a **governed tool access layer** (Path B) on top of Module 3's MCP Registry. It uses Amazon Bedrock AgentCore Gateway to provide JWT authentication, group-based access control, audit logging, and Bedrock Guardrails -- capabilities that the NGINX reverse proxy (Path A) cannot offer.

### The Problem

- Module 3's NGINX proxy (Path A) is fast but has no governance: no audit trail, no content screening, no per-team access control
- Lambda-backed tools can't be served through NGINX
- Platform teams need to control which workstream teams can access which tools
- Tool output may contain PII or harmful content that should be screened before reaching agents

### The Solution: Dual-Path Architecture

```
                            ┌─── Path A (Direct) ──────────────────────────┐
                            │   CloudFront → ALB → NGINX → Docker MCP      │
Agent ──┤                   │   Fast, no governance, Docker servers only    │
        │                   └──────────────────────────────────────────────┘
        │
        │                   ┌─── Path B (Governed) ─────────────────────────┐
        └───────────────────│   AgentCore Gateway (JWT auth)                │
                            │   → Request Interceptor (audit + access ctrl) │
                            │   → Tool Target (Lambda / MCP / HTTP)         │
                            │   → Response Interceptor (guardrails)         │
                            │   Governed, audited, Lambda-native            │
                            └───────────────────────────────────────────────┘
```

| Capability | Description |
|-----------|-------------|
| **AgentCore Gateway** | Managed MCP endpoint with CUSTOM_JWT auth and semantic search |
| **Group-Based Access Control** | Cognito groups (`gateway-admins`, `gateway-developers`) filter tool visibility and block unauthorized calls |
| **Audit Logging** | Every `tools/call` and `tools/list` logged to CloudWatch with actor, tool, timestamp (optional DynamoDB) |
| **Bedrock Guardrails** | Tool output screened for PII, harmful content before reaching agents |
| **Lambda-Native Targets** | Lambda functions invoked directly by the gateway (no HTTP proxy needed) |
| **Tag-Based Sync** | Only Registry tools tagged for the gateway are synced (platform controls selection) |
| **Registry Bridge** | Sync Lambda reads Module 3's Registry API and creates/removes gateway targets |

### What Module 4 Does NOT Have

This is intentionally a **thin integration layer**:

- No DynamoDB tables for tool storage (tools live in Module 3's Registry)
- No FastAPI API (Module 3's Registry API is the control plane)
- No Streamlit UI (Module 3's UI handles registration and browsing)
- No approval workflows (tools are available once registered in the Registry)

---

## Architecture

### Components

| Component | File | Purpose |
|-----------|------|---------|
| **Sync Lambda** | `handlers/sync_lambda.py` | Reads Registry API, creates gateway targets |
| **Request Interceptor** | `handlers/interceptors.py` | Audit logging + group-based access control |
| **Response Interceptor** | `handlers/interceptors.py` | Field sanitization + Bedrock Guardrails + group-based tool filtering |
| **Demo Tool Lambda** | `handlers/demo_tool.py` | MCP-compatible `search-knowledge-base` tool |
| **Gateway Sync Service** | `services/gateway_sync.py` | AgentCore Gateway target CRUD via boto3 |
| **Registry Client** | `services/registry_client.py` | M2M-authenticated Registry API client (urllib) |
| **Gateway Creator** | `create_gateway.py` | Creates/updates the AgentCore Gateway itself |
| **CDK Stack** | `cdk/agentcore_gateway_stack.py` | All infrastructure (4 Lambdas, EventBridge, IAM, Cognito groups) |

### Data Flow

```
Module 3 Registry API
    │
    │ GET /api/servers (M2M auth)
    ▼
Sync Lambda (every 5 min or manual)
    │
    │ Filter by SYNC_FILTER_TAGS
    │ Build target config from proxy_pass_url scheme
    │   lambda:// → Lambda target
    │   http(s):// → Skipped (uses Path A)
    ▼
AgentCore Gateway
    │
    │ Agent sends tools/list or tools/call (Cognito JWT)
    ▼
Request Interceptor
    │ 1. Decode JWT → extract actor + cognito:groups
    │ 2. Log audit event to CloudWatch (+ DynamoDB if AUDIT_TABLE_NAME set)
    │ 3. If tools/call + TOOL_ACCESS_POLICY set:
    │    check group has access → block if unauthorized
    ▼
Tool Target (Lambda / MCP)
    │
    ▼
Response Interceptor
    │ 1. If tools/list + TOOL_ACCESS_POLICY set:
    │    filter tools by caller's group permissions
    │ 2. Strip internal fields (embedding, gatewayTargetId, etc.)
    │ 3. If BEDROCK_GUARDRAIL_ID set:
    │    screen text content → replace if intervened
    ▼
Agent receives response
```

### Access Control Model

The `TOOL_ACCESS_POLICY` environment variable (JSON) maps Cognito groups to tool name patterns:

```json
{
  "gateway-admins": ["*"],
  "gateway-developers": ["product-*", "order-*", "search-*"]
}
```

- **Request interceptor**: Blocks `tools/call` if caller's groups don't match
- **Response interceptor**: Filters `tools/list` to only show accessible tools
- **No policy set**: All authenticated users see all tools (fail-open, backward compatible)

### Cross-Account Pattern (Platform vs Workstream)

In production, Module 4 sits in the **platform account** alongside Modules 2 and 3. Workstream accounts access tools via HTTPS + JWT:

```
PLATFORM ACCOUNT                    WORKSTREAM ACCOUNT
┌────────────────────┐              ┌────────────────────┐
│ Module 3: Registry │              │ Module 5: Agent    │
│ Module 4: Gateway  │◄──HTTPS+JWT──│ (Strands/AgentCore)│
│ Module 2: LLM GW  │              │                    │
│ Cognito (identity) │              │                    │
└────────────────────┘              └────────────────────┘
```

Workshop reality: single AWS account, but architecture STRUCTURED as if multi-account. Cognito group enforcement works the same way.

---

## AWS Resources Created

| Resource | Service | Purpose |
|----------|---------|---------|
| `agentcore-gateway-sync` | Lambda | Registry → Gateway target sync (Python 3.12, 256 MB, 120s) |
| `agentcore-gateway-request-interceptor` | Lambda | Audit + access control (Python 3.12, 256 MB, 10s) |
| `agentcore-gateway-response-interceptor` | Lambda | Guardrails + filtering (Python 3.12, 256 MB, 10s) |
| `workshop-search-knowledge-base` | Lambda | Demo MCP tool (Python 3.12, 128 MB, 10s) |
| EventBridge Rule | EventBridge | Triggers sync every 5 minutes |
| `workshop-agentcore-gateway-role-<region>` | IAM Role | Assumed by AgentCore Gateway |
| `gateway-admins` | Cognito Group | Full access to all gateway tools |
| `gateway-developers` | Cognito Group | Access to tagged tool subsets |

---

## Prerequisites

- **Module 3 deployed** -- CDK imports 5 CloudFormation exports from Module 3
- **AWS CLI** configured with credentials
- **Python 3.12+**
- **Node.js 18+** and **AWS CDK CLI** (`npm install -g aws-cdk@2.147.0` — version matched to the workshop IDE bootstrap)
- **CDK Bootstrap** run once per account/region

---

## Repository Structure

```
source/module-4a-tools-gateway/
├── handlers/                 # Lambda handlers
│   ├── sync_lambda.py        # Registry API → Gateway target sync
│   ├── interceptors.py       # Request (audit + ACL) + Response (guardrails + filtering)
│   ├── demo_tool.py          # search-knowledge-base MCP tool
│   ├── register_tools.py     # Helper: register Lambda/OpenAPI tools in Registry
│   └── register_gateway.py   # Helper: register gateway itself in Registry
├── services/                 # Shared service layer
│   ├── gateway_sync.py       # AgentCore Gateway target CRUD (boto3)
│   └── registry_client.py    # M2M-authenticated Registry API client (urllib)
├── cdk/                      # Infrastructure as Code
│   ├── app.py                # CDK app entry point
│   └── agentcore_gateway_stack.py  # Full stack definition
├── create_gateway.py         # Creates/updates the AgentCore Gateway
├── notebooks/                # Per-step Jupyter notebooks
│   ├── 01-two-paths.ipynb
│   ├── 02-deploy-stack.ipynb
│   ├── 03-register-tools.ipynb
│   ├── 04-sync-catalog.ipynb
│   ├── 05-test-both-paths.ipynb
│   ├── 06-bedrock-guardrails.ipynb
│   └── 07-register-gateway.ipynb
├── tests/
│   ├── conftest.py           # sys.path setup + aws_credentials fixture
│   ├── unit/                 # 42 unit tests
│   └── integration/          # E2E tests (require deployed stack)
├── requirements.txt
└── README.md                 # This file
```

---

## Quick Start

### 1. Run Unit Tests

```bash
cd source/module-4a-tools-gateway
pip install -r requirements.txt
pytest tests/unit/ -v
```

Expected: **48 passed**.

### 2. Deploy the Infrastructure

The workshop uses a CloudFormation stack (`workshop-tools-gateway-stack`) instead of CDK.
The CDK stack in `cdk/` is a developer reference only — participants do not run it.

For workshop use, the stack is pre-provisioned by Workshop Studio via `static/cfn/tools-gateway/workshop-tools-gateway-stack.yaml`.

For local development with CDK:

```bash
cd cdk
pip install -r requirements.txt
cdk deploy AgentCoreGatewayStack --require-approval never
```

### 3. Create the AgentCore Gateway

```bash
cd ..
python create_gateway.py
```

### 4. Follow the Notebooks

Open `notebooks/01-two-paths.ipynb` and work through all 7 notebooks in order.

---

## Environment Variables

### Sync Lambda

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_ID` | _(empty)_ | AgentCore Gateway ID (set after create_gateway.py) |
| `REGISTRY_URL` | _(empty)_ | Module 3 Registry API URL |
| `M2M_SECRET_NAME` | _(empty)_ | Secrets Manager secret with M2M credentials |
| `CLOUDFRONT_URL` | _(empty)_ | Module 3 CloudFront URL |
| `SYNC_FILTER_TAGS` | _(empty)_ | Comma-separated tags to filter; empty = sync all |

### Request Interceptor

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIT_TABLE_NAME` | _(empty)_ | DynamoDB table for audit logs |
| `TOOL_ACCESS_POLICY` | _(empty)_ | JSON: `{"group": ["pattern", ...]}` |

### Response Interceptor

| Variable | Default | Description |
|----------|---------|-------------|
| `BEDROCK_GUARDRAIL_ID` | _(empty)_ | Bedrock Guardrail ID |
| `BEDROCK_GUARDRAIL_VERSION` | `DRAFT` | Guardrail version |
| `TOOL_ACCESS_POLICY` | _(empty)_ | JSON: `{"group": ["pattern", ...]}` |

---

## Security

### Authentication

- AgentCore Gateway uses CUSTOM_JWT authorizer with Module 3's Cognito OIDC discovery URL
- M2M auth to Registry API via static token or Cognito `client_credentials` grant

### Authorization

- Group-based access control via `TOOL_ACCESS_POLICY` (fnmatch patterns)
- Request interceptor blocks unauthorized `tools/call` with MCP error (-32600)
- Response interceptor filters `tools/list` results by caller's Cognito groups

### Gateway Target Security

- Lambda ARN validation (must start with `arn:aws:lambda:`)
- Private/internal URL blocking (localhost, 169.254.x.x, 10.x.x.x, etc.)
- `credentialProviderConfigurations` set to `GATEWAY_IAM_ROLE` on all targets

### Response Sanitization

- Internal fields stripped: `gatewayTargetId`, `embedding`, `securityScanResult`, `createdBy`, `healthCheckMessage`, `lastHealthCheck`
- Bedrock Guardrails screen tool output for PII and harmful content (fail-open if unavailable)
