# Costs & AWS Resources

What the CDK stack creates in your account and what running the platform infrastructure costs.

[← Back to README](../README.md)

## AWS Resources Created

The CDK stack (`infra/stacks/platform_stack.py`) creates:

- **API Gateway HTTP API** -- Routes `/api/workflows/*` to Workflow Lambda, `/api/deploy`, `/api/test-runtime`, `/api/runtime/*`, `/api/generate-tool` to Deployment Lambda. HTTPS by default, CORS configured.
- **Workflow Lambda** -- FastAPI app wrapped with Mangum. Handles workflow CRUD, validation, import/export.
- **Deployment Lambda** -- Handles deploy initiation, status polling, runtime testing, runtime deletion (full cleanup), AI tool generation via Claude Sonnet on Bedrock, and CloudFormation template generation/export. 120s timeout for LLM calls.
- **Step Functions State Machine** -- Orchestrates multi-step deployments: validate -> [mcp_server?] -> [knowledge_base?] -> [gateway?] -> [memory?] -> [policy?] -> codegen -> IAM -> runtime configure -> runtime launch -> [evaluation?] -> [auth?] -> status update. 3 retries with exponential backoff per step.
- **Step Lambdas** -- Individual Lambda functions for each deployment step (validate, codegen, IAM, gateway, knowledge_base, mcp_server, memory, policy, evaluation, runtime_configure, runtime_launch, auth, status_update).
- **DynamoDB Tables** -- Workflows + Deployments (TTL + GSI on workflow_id/runtime_id), plus the enterprise-feature stores: `agent-versions`, `runtime-slots`, `agent-registry`, `prompt-library`, `hitl-requests` (24h TTL), `triggers`, `usage-events` (90d TTL), `budget`, `audit` (90d TTL), `permission-requests`, the shared org-config table (tag policies + VPC profiles + approval policies), and `flows`. Each user-data table carries an `owner_sub` GSI for owner-scoped list queries.
- **Cognito User Pool Groups** -- `registry-admin` / `registry-developer` for the registry two-persona approval model, plus the RBAC groups `t-admin`/`t-user` (tenant role) and `g-admins-*`/`g-users-*` (resource scopes). See [Registry & RBAC](REGISTRY_AND_RBAC.md).
- **EventBridge Rules** -- `policy-sweep` (rate 5 min — converges pending Cedar `ENFORCE` permits to `ACTIVE`) and `cost-reconcile` (rate 24 h — sweeps budgets for month-to-date breaches). Both invoke the Deployment Lambda with a sentinel payload.
- **S3 Bucket** -- Frontend static assets (CloudFront OAI access only) + AgentCore dependency bundles + deployment code artifacts.
- **CloudFront Distribution** -- HTTPS, SPA routing (404/403 -> index.html), API Gateway as additional origin for `/api/*`.
- **SSM Parameters** -- CORS origins, AWS region, DynamoDB table name under `/agentcore-workflow/{env}/`.
- **IAM Roles** -- Least-privilege per function: Workflow Lambda gets DynamoDB workflows + SSM read; Deployment Lambda gets DynamoDB deployments + bedrock-agentcore + `bedrock:ListFoundationModels`/`ListInferenceProfiles` (live model catalog) + `cloudwatch:PutMetricData` (budget breach) + JIT `iam:PutRolePolicy` scoped to `AgentCore*` roles + cleanup permissions (Cognito, Lambda, STS); Step Lambdas get full deployment permissions (IAM, Lambda, Cognito, S3, bedrock-agentcore).
- **CloudWatch Log Groups** -- Lambda and Step Functions execution logs.

All resources are tagged with `environment` and `project` for cost tracking.

## Infrastructure Pricing

This section covers the cost of running the **platform infrastructure** itself (API Gateway, Lambda, Step Functions, DynamoDB, S3, CloudFront, etc.). It does **not** include AgentCore deployment costs (Runtime compute, Gateway invocations, Memory storage, Bedrock model inference) which are billed separately by AWS Bedrock AgentCore.

All prices are for **us-east-1 (N. Virginia)** as of 2025. See the linked AWS pricing pages for the latest rates.

### Per-Service Breakdown

| Service | Pricing Model | Unit Price | AWS Free Tier |
|---------|--------------|------------|---------------|
| [API Gateway HTTP API](https://aws.amazon.com/api-gateway/pricing/) | Per-request | $1.00 / million requests | 1M requests/mo (12 months) |
| [Lambda](https://aws.amazon.com/lambda/pricing/) | Per-request + per-GB-second | $0.20 / million requests + $0.0000166667 / GB-second | 1M requests + 400,000 GB-s/mo (always free) |
| [Step Functions](https://aws.amazon.com/step-functions/pricing/) | Per state transition | $0.000025 / transition ($25 / million) | 4,000 transitions/mo (always free) |
| [DynamoDB On-Demand](https://aws.amazon.com/dynamodb/pricing/on-demand/) | Per-request + per-GB storage | $1.25 / million writes, $0.25 / million reads, $0.25 / GB-mo | 25 GB storage (always free) |
| [S3 Standard](https://aws.amazon.com/s3/pricing/) | Per-GB storage + per-request | $0.023 / GB-mo, $0.005 / 1K PUT, $0.0004 / 1K GET | 5 GB + 20K GET + 2K PUT/mo (12 months) |
| [CloudFront](https://aws.amazon.com/cloudfront/pricing/) | Per-request + per-GB transfer | $1.00 / million HTTPS requests, $0.085 / GB transfer | 1 TB transfer + 10M requests/mo (always free) |
| [CloudWatch Logs](https://aws.amazon.com/cloudwatch/pricing/) | Per-GB ingested + stored | $0.50 / GB ingestion, $0.03 / GB-mo storage | 5 GB ingestion + 5 GB storage/mo (always free) |
| [SSM Parameter Store](https://aws.amazon.com/systems-manager/pricing/) | Standard parameters | Free | Up to 10,000 standard parameters |
| [IAM](https://aws.amazon.com/iam/) | All resources | Free | Unlimited |

### Monthly Cost Estimates

Estimates assume all agent deployment activity flows through Step Functions (the primary path from the UI). Each deployment triggers ~16 state transitions and ~16 Lambda invocations.

| Service | Low Usage | Moderate Usage |
|---------|-----------|----------------|
| | *~100 deploys, ~1K API requests/mo* | *~1,000 deploys, ~10K API requests/mo* |
| API Gateway HTTP API | $0.00 | $0.01 |
| Lambda (3 functions + 11 step handlers) | $0.00 | $0.00 |
| Step Functions | $0.00 | $0.30 |
| DynamoDB (2 tables, on-demand) | $0.00 | $0.01 |
| S3 (2 buckets, ~0.6–2 GB) | $0.02 | $0.07 |
| CloudFront (1 distribution) | $0.00 | $0.00 |
| CloudWatch Logs | $0.00 | $0.00 |
| SSM Parameter Store (5 params) | $0.00 | $0.00 |
| IAM | $0.00 | $0.00 |
| **Total** | **~$0.02/mo** | **~$0.39/mo** |

> With the AWS Free Tier (first 12 months or always-free tiers), this platform costs effectively **$0.02/month** at low usage and under **$0.40/month** at moderate usage. Even without any free tier, moderate usage stays under **$2/month**. The serverless, pay-per-request architecture means you pay nothing when the platform is idle.

### What's NOT Included

The table above covers **platform infrastructure only**. The following are billed separately by AWS and depend on your agent usage:

- **Amazon Bedrock model inference** — per-token pricing varies by model (e.g., Claude Sonnet, Nova)
- **AgentCore Runtime compute** — billed per-second while the runtime is active
- **AgentCore Gateway invocations** — per-request to the MCP gateway
- **AgentCore Memory storage** — per-GB for conversation memory
- **Bedrock Knowledge Base** — per-query for RetrieveAndGenerate, vector store costs (OpenSearch Serverless / RDS Aurora), S3 storage for source documents
- **AgentCore Code Interpreter / Browser** — per-session usage
- **Cognito** — first 50,000 MAUs are free; OAuth2 machine-to-machine auth adds $0.0065/token request beyond 250/mo
- **Custom tool Lambda functions** — same Lambda pricing as above, billed per invocation

Refer to the [Amazon Bedrock AgentCore pricing page](https://aws.amazon.com/bedrock/agentcore/pricing/) for current rates.
