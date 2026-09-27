# Enterprise Agentic AI Platform Blueprint on AWS

[![license](https://img.shields.io/badge/license-MIT--0-blue)](LICENSE)
[![AWS CDK](https://img.shields.io/badge/AWS%20CDK-TypeScript-orange)](https://aws.amazon.com/cdk/)

A multi-account AWS CDK reference architecture for deploying governed generative-AI agents with Amazon Bedrock AgentCore. It provides a central inference boundary, workstream-owned tool execution, AWS Agent Registry governance, pipeline promotion gates, tenant isolation, guardrails, centralized observability, and dependency-ordered cleanup.

> **Important:** This repository is sample code, not an AWS service or an AppSec-reviewed product. It deploys real, billable AWS resources. Review the architecture, IAM policies, data handling, quotas, and costs for your organization before using it with production or regulated workloads.

![D-03 two-Gateway architecture](assets/d03-two-gateway.svg)

## What this blueprint provides

The supported reference path uses three account roles:

| Account role              | Responsibilities                                                                                                           |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Management and Governance | AWS Organizations controls, CloudWatch OAM sink, centralized audit and log archive                                         |
| Platform                  | AWS Agent Registry, the shared AgentCore inference Gateway, Bedrock Guardrails, Cognito M2M, and both deployment pipelines |
| Workstream                | AgentCore Runtime, Memory, a workstream Tool Gateway, per-agent roles, and application execution                           |

Generated agents have exactly two outbound application paths:

1. `MCPClient` signs requests with AWS SigV4 to the workstream-owned AgentCore Tool Gateway. The Gateway exposes only Registry-approved tools and invokes exact Platform Lambda aliases through its service role.
2. `LiteLLMModel` obtains a short-lived token through AgentCore Identity and Cognito M2M, then calls the Platform AgentCore Inference Gateway. A mandatory request interceptor applies the stage Guardrail before the Bedrock Mantle target is invoked.

Generated-agent code must not invoke Bedrock or Lambda directly.

## Support envelope

The complete reference flow is live-validated in `eu-west-1` (Ireland), including:

- Platform and Workload pipelines through production.
- AWS Agent Registry record resolution and approval checks.
- `LiteLLMModel`, `MCPClient`, AgentCore Identity, Runtime, Memory, and both Gateways.
- Benign and adversarial Guardrail requests.
- Exact HTTP 429 behavior for an unallocated model.
- Cross-account Runtime denial.
- Runtime update cancellation, rollback to the prior agent version, and re-run to green with no failed sampled sessions.
- Evaluation thresholds for regression, response quality, tool success, refusal behavior, first-token latency, and per-prompt cost.
- CloudWatch OAM access to linked Platform and Workstream logs and metrics.
- Gateway application-log and OTEL span correlation for admitted and throttled requests when Transaction Search is enabled.
- Dependency-ordered teardown and independent zero-residual inventories.

Ireland is the current EMEA reference Region because AWS Agent Registry is available there. Treat every other Region as unvalidated until you run the same service-availability, policy, pipeline, adversarial, rollback, observability, and teardown gates independently.

The following remain outside the Ireland support envelope:

- Legacy direct-Bedrock evaluation, online-evaluation, ECS LiteLLM, and direct circuit-breaker paths that rely on cross-Region inference profiles.
- VPC Lattice private endpoints.
- Automatic retirement of the Lambda Cedar wrapper; it remains a rollback and defense-in-depth control.
- Transaction Search as a default. It is account-wide and billable, so customers opt in deliberately.

## Prerequisites

- Node.js 20 or later.
- Python 3.12 or later.
- AWS CLI v2.
- AWS CDK v2.
- Three AWS accounts or equivalent isolated account roles.
- AWS Organizations or explicit trusted-account configuration for the OAM sink.
- A GitHub repository and AWS CodeConnections connection.
- Access to the selected Bedrock Mantle model in the target Region.
- Administrator access for initial bootstrap only. Pipeline deployments use generated scoped execution policies.

## Install and validate

```bash
npm ci
npm run build
npm test
npm run lint
npm run scrub
```

For infrastructure changes, also synthesize the exact topology you intend to deploy:

```bash
export AWS_REGION=eu-west-1
export AWS_DEFAULT_REGION="$AWS_REGION"
export CDK_DEFAULT_REGION="$AWS_REGION"

npx cdk synth --strict \
  --context stage=pipeline \
  --context agenticai/pipelineSelection=platform \
  ...
```

Set all three Region variables. The CDK CLI derives the child process Region from its SDK session, so setting only `CDK_DEFAULT_REGION` is not sufficient.

## Configuration

The CDK application reads `agenticai/*` context values. Never commit real account IDs, secret ARNs, tokens, or generated Registry context files to a public repository.

Core Platform context:

- `agenticai/githubRepo`
- `agenticai/githubBranch`
- `agenticai/githubConnectionArn`
- `agenticai/organizationId`
- `agenticai/auditAccountId`
- `agenticai/logArchiveAccountId`
- `agenticai/platformNonprodAccountId`
- `agenticai/platformProdAccountId`
- `agenticai/workloadAccountIds`
- `agenticai/inferenceModelRateLimits`
- `agenticai/auditOamSinkArn`

Core Workload context:

- `agenticai/tenantId`
- `agenticai/agentId`
- `agenticai/applicationId`
- `agenticai/costCentre`
- `agenticai/workloadNonprodAccountId`
- `agenticai/workloadProdAccountId`
- `agenticai/workloadNonprodAvailabilityZones`
- `agenticai/workloadProdAvailabilityZones`
- `agenticai/enableGaRegistryConsumer=true`
- `agenticai/gaRegistryExpectedToolIds`
- `agenticai/gaRegistryNonprodContextFile`
- `agenticai/gaRegistryProdContextFile`
- `agenticai/workstreamGatewayRegion`
- `agenticai/enablePipelineRuntimeMemory=true`
- `agenticai/agentImageVariant=generated-agent`
- `agenticai/generatedAgentInference`

Keep environment-specific context outside source control and inject it from your deployment system.

## Bootstrap with scoped policies

Generate one execution policy per account and Region:

```bash
python3 pipelines/bootstrap/render-cfn-execution-policy.py platform \
  --account-id <PLATFORM_ACCOUNT> \
  --region eu-west-1 \
  --target-account-id <PLATFORM_ACCOUNT> \
  --target-account-id <WORKSTREAM_ACCOUNT> \
  --target-account-id <MANAGEMENT_ACCOUNT> \
  --connection-arn <CODECONNECTIONS_ARN> \
  > platform-policy.json

python3 pipelines/bootstrap/render-cfn-execution-policy.py workstream \
  --account-id <WORKSTREAM_ACCOUNT> \
  --region eu-west-1 \
  > workstream-policy.json

python3 pipelines/bootstrap/render-cfn-execution-policy.py management \
  --account-id <MANAGEMENT_ACCOUNT> \
  --region eu-west-1 \
  > management-policy.json
```

Validate each document before creating or updating it:

```bash
aws accessanalyzer validate-policy \
  --region eu-west-1 \
  --policy-type IDENTITY_POLICY \
  --policy-document file://platform-policy.json
```

Create the documents under the same local managed-policy name in each account, then run:

```bash
export AWS_REGION=eu-west-1
export AWS_DEFAULT_REGION="$AWS_REGION"
export CDK_DEFAULT_REGION="$AWS_REGION"
export CFN_EXECUTION_POLICY_NAME=AgenticAICdkExecutionPolicyEuWest1

bash pipelines/bootstrap/bootstrap-cross-account.sh
```

Do not use `AdministratorAccess` as the CloudFormation execution policy.

## Deployment sequence

### 1. Deploy the Platform producer

Create or update `AgenticAI-PlatformPipelineStack` with Gateway invoke permissions disabled:

```text
agenticai/enableGaGatewayInvokePermissions=false
```

Run the Platform pipeline. It creates the environment Registries, governance records, tool aliases, Guardrails, inference Gateways, and Management stacks.

### 2. Approve Registry records

Use the ownership-checking utility after reviewing the processed CloudFormation descriptors:

```bash
python3 scripts/live-agent-registry-spike/approve_pipeline_registry.py verify ...
python3 scripts/live-agent-registry-spike/approve_pipeline_registry.py apply ...
```

The utility requires all records to match their templates before it submits or approves any record.

### 3. Resolve Workload Registry context

Resolve one non-secret context file per environment:

```bash
python3 pipelines/resolve_ga_registry_context.py \
  --account-id <PLATFORM_ACCOUNT> \
  --region eu-west-1 \
  --environment nonprod \
  --application-id <APPLICATION_ID> \
  --agent-id <AGENT_ID> \
  --tenant-id <TENANT_ID> \
  --cost-centre <COST_CENTRE> \
  --expected-tool-id tool-echo \
  --expected-tool-id tool-ping \
  --source-revision "$(git rev-parse HEAD)" \
  --output <NONPROD_CONTEXT_FILE>
```

Repeat for production. The resolver is read-only and fails if ownership tags, record state, descriptor digests, or target ARNs differ.

### 4. Create the Workload pipeline

Deploy only the Workload pipeline root. The pipeline—not a local developer command—owns every Workstream mutation.

The first stage creates stable Workstream roles and pauses at `GatewayPermissionReady`.

### 5. Grant exact tool-alias permissions

Read the nonproduction and production `GatewayServiceRoleArn` outputs. Update the Platform pipeline root with:

```text
agenticai/enableGaGatewayInvokePermissions=true
agenticai/gaGatewayServiceRoleArns=[<NONPROD_ROLE_ARN>,<PROD_ROLE_ARN>]
```

Run the Platform pipeline and verify each Lambda alias policy names its matching role ARN. Only then approve `GatewayPermissionReady`.

### 6. Promote the generated agent

The Workload pipeline deploys nonproduction, invokes the deployed Runtime through the mandatory evaluation gate, and pauses before production. Review the evaluation output and adversarial evidence before approving production.

## Security controls

- Scoped CDK execution policies generated per account and Region.
- AWS Organizations SCPs for model, Region, Guardrail, Registry, Gateway, and deployment boundaries.
- Mandatory Bedrock Guardrail request interceptor on the inference Gateway.
- Model allow-list on the Gateway execution role.
- Cognito M2M and AgentCore Identity short-lived credentials.
- AWS_IAM authentication for the workstream Tool Gateway.
- Registry-approved tool descriptors and exact alias ARNs.
- Exact Workstream role principals on Platform Lambda alias policies.
- Actor-scoped AgentCore Memory and customer-managed KMS keys.
- Digest-bound ECR image scanning that blocks every Critical or High finding.
- Human approval after deployed-runtime evaluation.
- Five allocation tags on taggable resources: `application-id`, `agent-id`, `tenant-id`, `cost-centre`, and `environment`.
- CloudWatch OAM links for centralized Logs, Metrics, and Traces.

Native AgentCore Gateway rate limits are approximate, fail-open traffic shaping. They are not an authorization or hard-quota boundary. Use IAM/SCP controls and account-level Bedrock quotas for those guarantees.

## Testing

Common local gates:

```bash
npm run build
npm test
npm run lint
npm run scrub

python3 -m pytest tests/adversarial/unit -q
python3 -m pytest scripts/test_final_teardown.py scripts/test_residue_inventory.py -q
```

Live gates are intentionally fail-closed. A missing credential, probe, resource, or expected denial cannot be reported as success.

For any behavior-changing revision, validate at minimum:

1. strict synth and clean cdk-nag reports;
2. deployment through the reviewed pipelines;
3. authorized positive calls;
4. exact adversarial denials;
5. a mutation proving the test catches removal of the control;
6. rollback and re-run to green;
7. centralized logs/metrics/traces;
8. final dependency-ordered teardown and independent inventory.

## Observability

The Platform pipeline root and each distinct Workstream account/Region create one OAM source link to the Management sink. A same-account nonproduction/production profile shares one link to avoid OAM cardinality conflicts.

Gateway application logs require a CloudWatch Logs delivery. Gateway spans require Transaction Search and a `TRACES` delivery. Transaction Search changes account-wide settings and incurs cost; enable it deliberately, verify ingestion, and restore or retain it according to your operating model.

## Cost

Costs depend on model traffic, Runtime duration, log retention, VPC endpoints, and account-level security services. Use cost-allocation tags and Cost and Usage Reports for attribution. Cost Explorer account totals are not automatically blueprint costs when accounts host unrelated workloads.

Recommended controls:

- per-application AWS Budgets;
- account-level Bedrock quotas;
- model routing by quality/latency need;
- bounded CloudWatch retention;
- monthly CUR reconciliation by allocation tags.

## Cleanup

Always retire Platform alias grants before deleting Workstream roles:

1. Update the Platform pipeline with `agenticai/enableGaGatewayInvokePermissions=false`.
2. Run it through production.
3. Verify all four alias policies no longer name a current or stale Workstream role principal.

Then run the fail-closed teardown in this order:

```bash
python3 scripts/final_teardown.py \
  --account-role workstream \
  --expected-account <WORKSTREAM_ACCOUNT> \
  --region eu-west-1

# After reviewing the dry run:
python3 scripts/final_teardown.py \
  --account-role workstream \
  --expected-account <WORKSTREAM_ACCOUNT> \
  --region eu-west-1 \
  --apply

python3 scripts/final_teardown.py \
  --account-role platform \
  --expected-account <PLATFORM_ACCOUNT> \
  --region eu-west-1 \
  --apply

python3 scripts/final_teardown.py \
  --account-role management \
  --expected-account <MANAGEMENT_ACCOUNT> \
  --region eu-west-1 \
  --apply
```

Finally, measure each account directly:

```bash
python3 scripts/residue_inventory.py \
  --expected-account <ACCOUNT_ID> \
  --region eu-west-1 \
  --global
```

Expected terminal state is zero live project resources. Customer-managed keys can remain in AWS's seven-day pending-deletion window. Object Lock can make data intentionally undeletable until its retention period expires; inspect the plan before deploying those optional constructs.

## Repository layout

```text
apps/          account and deployment-stage stacks
bin/           CDK application entry point
blueprints/    reference agent applications
packages/      reusable constructs and governance modules
pipelines/     Platform and Workload pipelines plus bootstrap helpers
scripts/       verification, recovery, and cleanup utilities
tests/         conformance, adversarial, integration, smoke, and teardown tests
assets/        architecture diagrams
```

## Contributing

See the repository-level [contribution guidelines](https://github.com/aws-samples/sample-ai-agent-factory/blob/main/CONTRIBUTING.md) and [code of conduct](https://github.com/aws-samples/sample-ai-agent-factory/blob/main/CODE_OF_CONDUCT.md). Report security issues through the [AWS vulnerability reporting process](https://aws.amazon.com/security/vulnerability-reporting/), not a public issue.

## License

This project is licensed under the MIT-0 License. See [LICENSE](LICENSE).
