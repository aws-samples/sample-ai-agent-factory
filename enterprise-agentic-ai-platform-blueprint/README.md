# Enterprise Agentic AI Platform Blueprint on AWS

![version](https://img.shields.io/badge/version-1.0.0-blue) ![tests](https://img.shields.io/badge/tests-passing-brightgreen) ![packages](https://img.shields.io/badge/packages-35-blue) ![cdk-nag](https://img.shields.io/badge/cdk--nag-clean-brightgreen) ![license](https://img.shields.io/badge/license-MIT--0-blue)

A multi-account AWS CDK blueprint for running enterprise agentic AI workloads on **Amazon Bedrock AgentCore**, with org-level guardrails, tenant isolation, guardrailed inference, per-tool Cedar authorisation, and per-application cost attribution.

This is one of the samples in [`aws-samples/sample-ai-agent-factory`](https://github.com/aws-samples/sample-ai-agent-factory) — it is the **governed platform foundation** an organisation stands up once, so that agent-building teams have a secured landing zone to deploy onto. See [§1.2](#12-how-this-fits-with-the-other-samples-in-this-repository) for how it relates to the sibling samples.

> **Status.** Sample / reference content published under MIT-0. It is **not** an AppSec-reviewed product — run your own security review before deploying to any regulated or customer-facing environment, and read [§15](#15-known-limitations-and-honest-disclaimers) for what has and has not been verified against live AWS. The repository includes Jest conformance tests, an AWS-free adversarial harness, offline evaluation tests, integration suites, and fail-closed teardown tests. The D-03 v3 + gap-closure surface was verified end-to-end on a real two-account deploy in `us-east-1` (MCP `tools/list` + `tools/call` through the CUSTOM_JWT gateway, per-developer Cedar entitlement allow/deny), then torn down to zero residuals. Full history in [`CHANGELOG.md`](CHANGELOG.md).
>
> **This deploys real, billable AWS resources** across multiple accounts — see [§8 Cost](#8-cost) before deploying and [§16 Cleanup](#16-cleanup) when you are done.

> **Accepted target state — implementation in progress.** The revamp converges D-01 and D-03 into one topology: one consolidated Management/Governance account, environment-isolated Platform accounts, and per-workstream accounts. The golden-path inference boundary is Amazon Bedrock AgentCore Gateway with a Bedrock Mantle inference target; generated agents use `LiteLLMModel` against its OpenAI-compatible endpoint. The `us-west-2` inference compatibility spike and reviewed Platform pipeline both passed Cognito M2M, 49-model discovery, streaming and non-streaming inference; the isolated spike also proved exact HTTP 429 rate limiting and zero-residue cleanup. The pipeline-created production inference Gateway and target reached `READY`, its rate limit reached `ACTIVE`, its baseline Guardrail reached `READY`, and the Management/Governance Log Archive is live. An isolated PolicyEngine run on exact commit `46c3a62` passed eight strict policies, a 20-case `sub`/group matrix, filtered `tools/list`, direct-call denial, exact JWT rejection statuses, mode rollback, and independent zero-residual inventory. The GA Agent Registry API contract passed on exact commit `8e66dc3`; pipeline-owned R1 then deployed and explicitly approved both environments. On 2026-09-21 pipeline-owned R2 commit `3870e0e` completed cross-account Registry resolution, exact role/permission handoff, nonproduction and production Tool Gateway deployment, MCP positives and denial twins, no-op redeployment, fail-closed status drift, terminal-record recovery, and dependency-ordered teardown. Teardown hardening commit `7774299` removed every exact service-created log group, and independent inventory found zero unintended residue. Pipeline-owned Gateway PolicyEngine commit `f45a12c` passed nonproduction and production deployment, positive and direct-policy-denial twins, in-place semantic-search removal, mode rollback, exact principal restoration, fail-closed teardown, grant retirement, and zero unintended residue; the Lambda Cedar wrapper remains intentionally retained. An isolated Runtime and Memory compatibility campaign on exact commit `88d5381` passed a zero-finding digest-pinned ARM64 image build, Memory `ACTIVE`, Runtime `READY`, exact invocation and short-term event round trips, Runtime-before-Memory teardown, grant retirement, and independent zero-active-residue inventory. Pipeline-owned Runtime/Memory commit `442de00` then passed environment-qualified role and permission handoff, exact-digest zero-finding image admission, nonproduction and production Gateway/Runtime/Memory deployment, Runtime and Memory round trips, MCP and bypass twins, live content-address rejection, full no-op redeployment, dependency-ordered teardown, grant retirement, exact log/image cleanup, and zero unintended residue. The deployed Runtime intentionally remained an inert compatibility handler. See [`evidence/live/2026-09-21-agentcore-runtime-memory-compatibility-spike.md`](evidence/live/2026-09-21-agentcore-runtime-memory-compatibility-spike.md), [`evidence/live/2026-09-22-pipeline-agentcore-runtime-memory.md`](evidence/live/2026-09-22-pipeline-agentcore-runtime-memory.md), and [`evidence/live/2026-09-21-pipeline-agentcore-policyengine.md`](evidence/live/2026-09-21-pipeline-agentcore-policyengine.md). Remaining release gates include generated-agent `LiteLLMModel`/`MCPClient` proof through that pipeline; a safe pipeline-level induced RuntimeMemory rollback and live High-finding image rejection; a live legacy-consumer rollback; matching-principal wrong-ExternalId and wrong-session-name twins; SCP 01–12 organization soak; EMEA coverage; load, concurrency, quota, soak, chaos, upgrade, and interrupted-deployment campaigns; Gateway OTEL span correlation; and a measured 24-hour cost baseline. See [`docs/architecture/target-state-architecture.md`](docs/architecture/target-state-architecture.md), [ADR-0016](docs/adr/ADR-0016-agentcore-gateway-inference-supersedes-litellm-proxy.md), [`evidence/live/2026-09-18-agentcore-gateway-spike.md`](evidence/live/2026-09-18-agentcore-gateway-spike.md), [`evidence/live/2026-09-19-platform-pipeline-deployment.md`](evidence/live/2026-09-19-platform-pipeline-deployment.md), [`evidence/live/2026-09-19-policyengine-compatibility-spike.md`](evidence/live/2026-09-19-policyengine-compatibility-spike.md), [`evidence/live/2026-09-19-agent-registry-compatibility-spike.md`](evidence/live/2026-09-19-agent-registry-compatibility-spike.md), [`evidence/live/2026-09-20-pipeline-ga-agent-registry-r1.md`](evidence/live/2026-09-20-pipeline-ga-agent-registry-r1.md), and [`evidence/live/2026-09-21-pipeline-ga-agent-registry-r2.md`](evidence/live/2026-09-21-pipeline-ga-agent-registry-r2.md).

![Architecture](assets/architecture-diagram.png)

> _Drawio source: [`assets/architecture-diagram.drawio`](assets/architecture-diagram.drawio)._

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Deviations](#3-deviations)
4. [AWS Services Used](#4-aws-services-used)
5. [Prerequisites](#5-prerequisites)
6. [Deployment](#6-deployment)
7. [Running the Guidance](#7-running-the-guidance)
8. [Cost](#8-cost)
9. [Operations](#9-operations)
10. [Security](#10-security)
11. [Choice architecture](#11-choice-architecture)
12. [Compliance](#12-compliance)
13. [Multi-account topology](#13-multi-account-topology)
14. [Architecture Decision Records](#14-architecture-decision-records)
15. [Known limitations and honest disclaimers](#15-known-limitations-and-honest-disclaimers)
16. [Cleanup](#16-cleanup)
17. [Contributors and License](#17-contributors-and-license)

---

## 1. Overview

Agentic AI platforms at enterprise scale need the same controls ordinary systems need — identity, network isolation, audit, cost attribution, tenancy — plus agentic-specific ones: model allow-listing, Bedrock Guardrails on every inference call, Cedar micro-policies on AgentCore Gateway, memory-namespace isolation, and evaluation gates before promotion.

This blueprint delivers all of the above as a deployable AWS CDK app spanning a real AWS Organization, in two mutually-exclusive deployment patterns.

**Default distributed pattern (D-01)** — `apps/workload-account/`. Everything lives in the workload account: baseline SCPs 01–08 at the OU, per-account VPC (11 interface VPCEs + 1 S3 gateway, no IGW/NAT), baseline Bedrock Guardrail, LiteLLM in the inference path with a triple-gate guardrail enforcement (SCP + IAM deny + VPCE policy), AgentCore Runtime/Gateway/Identity/Memory/Registry, API Gateway as the primary auth boundary, CloudWatch cross-account observability, CDK Pipelines with a mandatory evaluation gate, and three Strands agent blueprints.

**Centralised-platform alternative (D-03 v3)** — `apps/platform-account/` + `apps/workload-account/lib/d03-workload-agent-stack.ts`. A platform-governed, per-workstream AgentCore Gateway is deployed **into** the workstream account, removing the cross-account Runtime→Gateway hop while keeping platform governance via three layers:

1. **Synth** — the target SSOT is the GA AWS Agent Registry (`packages/agent-registry/`). R1 added native `AWS::AgentRegistry::Registry` and `RegistryRecord` resources, custom governance documents, a conditioned `RegistryReaderRole`, and versioned SSM discovery parameters alongside the unchanged DynamoDB rollback path. R2 is now pipeline/live-verified in both environments: developers commit stable tool IDs, the Workload synth resolves approved records cross-account, and deploy-time validation pins status, descriptor digest, and target ARN before Gateway targets are created. The retained legacy consumer has passed strict rollback synthesis but has not yet been redeployed live from the R2 revision.
2. **Deploy** — SCP-09 denies `bedrock-agentcore:Create/Update/Delete*` on Gateway resources from every principal except environment-qualified, pipeline-created `AgenticAI-D03-*-GatewayAdmin` roles in the configured Workstream accounts.
3. **Runtime** — the Gateway service role's identity policy lists the exact N subscribed tool ARNs (no wildcards); SCP-10 denies `lambda:InvokeFunction` to any non-catalogued ARN.

Cross-account Bedrock calls go through a `BedrockCallerRole` (`sts:ExternalId` + `aws:PrincipalArn` + `RoleSessionName` trust conditions); per-tenant Application Inference Profiles carry CUR attribution tags in place of `sts:TagSession` (which does not propagate across role chains — `BUG-005`).

### 1.1 Day-in-the-life — how a developer ships an agent

Workstream accounts are the developer's primary surface; the platform account holds governance + the central Registry and developers do not log into it.

1. **Onboarding (platform team, one-time).** `D03PlatformCoreStack` provisions three Identity Center permission sets per workstream — `AgenticAI-WS-Dev-<ws>` (deploy + observability + Registry consumer), `-Ro-` (read-only), `-Apv-` (pipeline approve).
2. **Discover + subscribe.** `agenticai registry search` / `subscribe` appends stable tool ids to `cdk.context.json`; the Workload synth resolves each environment's generated RegistryRecord ID through versioned SSM parameters, requires `APPROVED`, and emits exact cross-account Lambda alias ARNs into the service role (no wildcards).
3. **Build + eval + submit.** `agenticai dev eval` runs the same 7-category scoring the CI gate runs; `agenticai submit` renders the PR body. The pipeline runs Source → Synth → Deploy(nonprod) → Evaluation Gate → Manual Approval → 5 % canary + soak → Prod.
4. **Per-developer entitlement.** A Curator can pin `metadata.allowedGroups` on a record; the Gateway then runs in `CUSTOM_JWT` mode and the per-tool Lambda's Cedar wrapper (`@agenticai/tool-cedar-wrapper`) denies on `cognito:groups` mismatch before user code runs. The pipeline-owned AgentCore Gateway PolicyEngine path has now passed IAM-principal behavior parity, rollback, production deployment, and zero-residual teardown. The legacy wrapper remains intentionally active as a rollback and defense-in-depth control until maintainers make a separate retirement decision.

### 1.2 How this fits with the other samples in this repository

`sample-ai-agent-factory` collects complementary samples covering different layers of an AI Agent Factory. This one is the **platform foundation** — the multi-account landing zone, org guardrails, and governance surface. It is deliberately infrastructure-heavy and assumes a real AWS Organization.

| Sample                                                                                | Layer                | Relationship to this blueprint                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------------------------------------------------------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`workshop-building-agentic-ai-platform/`](../workshop-building-agentic-ai-platform/) | Learn the foundation | Closest neighbour. A guided 300-level workshop over the same building blocks (LLM Gateway via LiteLLM, MCP Gateway + Registry, Strands agents) in a **single account**. **Start there** if you want to understand the pattern hands-on; come here when you need the multi-account, SCP-governed, CI/CD-gated production form of it.                                                                                                                                                                                                                                                            |
| [`Agentic-ai-self-service/`](../Agentic-ai-self-service/)                             | Build agents         | The builder experience that sits **on top of** a foundation like this one. It gives teams a visual canvas for authoring and deploying AgentCore agents; this blueprint provides the governed accounts, model allow-list, guardrails, and cost attribution those agents deploy into.                                                                                                                                                                                                                                                                                                            |
| [`enterprise-mcp-governance-gateway/`](../enterprise-mcp-governance-gateway/)         | Govern tool calls    | Overlapping but distinct depth on per-tool-call authorisation. That sample evaluates Cedar in the AgentCore Gateway's **PolicyEngine in `ENFORCE` mode** and is the better reference for the request-path interceptor and OAuth 3LO connector patterns. This blueprint now has pipeline/live-verified native PolicyEngine enforcement and intentionally retains Cedar **inside each tool Lambda** as a rollback and defense-in-depth control (§3.3); it also adds the org-level layers around that boundary — SCP-09/10/11, the Registry as tool SSOT, and synth-time subscription validation. |

Pick this sample if your question is _"how do I govern agentic AI across many accounts and many teams?"_. Pick one of the others if your question is _"how do I learn this?"_, _"how do I ship an agent quickly?"_, or _"how do I authorise a single tool call?"_.

---

## 2. Architecture

See `assets/architecture-diagram.png` (editable `.drawio` source alongside). Control-level detail below.

### 2.1 Account topology

| Role                     | OU                  | Purpose                                                                                                              |
| ------------------------ | ------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Management               | Root                | AWS Organization, OUs, SCPs 01-12 (01-08 baseline; 09-10 D-03 Gateway; 11 Registry; 12 developer permission sets)    |
| Log Archive              | Security            | CloudTrail org trail + CUR + CWL cross-account destination                                                           |
| Audit                    | Security            | CloudWatch OAM sink, Security Hub master                                                                             |
| Platform non-prod / prod | AgenticAI-Platform  | Guardrail Admin, Registry, CDK Pipelines, central AgentCore inference Gateway, Cognito M2M, native model rate limits |
| Workload non-prod / prod | AgenticAI-Workloads | Per-application agent stacks                                                                                         |
| SCP Sandbox              | AgenticAI-Sandbox   | Soaks new SCPs before promotion                                                                                      |

CloudWatch Logs destination access policies are service-specific: `Principal.AWS` lists each sender's 12-digit account ID. IAM root ARNs are not equivalent here and are rejected by `PutDestinationPolicy`.

### 2.2 Network

Per workload account (`packages/agentic-vpc/`): VPC with 3 AZs, **private-isolated subnets only** (no IGW/NAT); interface VPCEs for AgentCore (data/control/gateway), Bedrock (runtime/management), ECR, CloudWatch, STS, KMS + S3 gateway endpoint (11 interface + 1 gateway). Endpoint policies scoped to the local account root; the Bedrock Runtime endpoint restricts `InvokeModel`/`Converse` to the allow-listed model ARNs and denies when `GuardrailIdentifier` is Null. VPC Flow Logs → CMK-encrypted log group.

### 2.3 Bedrock governance

- **Model allow-list SSOT** — `PLATFORM_ALLOWED_MODELS` (`packages/platform-baselines/`) = Claude Sonnet 4.5 + Haiku 4.5, flowing into SCP-01, the Bedrock VPCE policy, the LiteLLM router config, and AgentCore execution-role IAM. A conformance test diffs all four for drift.
- **Guardrail triple-gate** — every invocation carries a `GuardrailIdentifier`, enforced by SCP-02 (org), an IAM identity-policy deny (`Null: bedrock:GuardrailIdentifier` twinned with `ForAnyValue:StringNotEquals` positive allow-list), and the Bedrock VPCE policy.
- **Guardrail profiles** — Baseline (mandatory default: HIGH content filters, prompt-attack detection, AU TFN/Medicare/BSB regex, PII BLOCK/ANONYMIZE), Internal Tool, Customer-Facing (both opt-in, platform-approved).
- **Segregation of duties** — `GuardrailAdminRole` in the platform account only; SCP-05 (`ArnNotLike` on role + assumed-role session forms) denies guardrail mutation everywhere else.
- **Model Invocation Logging** — CMK-encrypted `/agenticai/bedrock-invocations`, text-only delivery; under D-03 the record carries the per-tenant `inferenceProfileArn`.

### 2.4 AgentCore stack

- **Runtime** (`packages/agentcore-runtime/`) — per-agent execution role, immutable-tag ECR repo, CMK log group. Under D-03 the role is trusted by `bedrock-agentcore.amazonaws.com`.
- **Central inference Gateway** (`packages/platform-inference-gateway/`) — native `AWS::BedrockAgentCore::Gateway`, Bedrock Mantle inference target, Cognito client-credentials JWT authorizer, explicit model RPM/TPM entries, and a zero-rate wildcard fallback. `LiteLLMModel` calls `<GatewayUrl>/inference/v1` with `<InferenceTargetName>/<provider-qualified-model-id>`; the Cognito client secret is never output.
- **Workstream tool Gateway — legacy placeholder** (`packages/agentcore-gateway/`) — API Gateway HTTP v2 + Cognito JWT authorizer + WAFv2 + VPC Link and an internal ALB. It remains architectural debt until replaced by a real per-workstream AgentCore Gateway in the next vertical-slice stage.
- **Identity** (`packages/agentcore-identity/`) — Cognito User Pool + Token Vault CMK; 12-char password minimum, email verification, deletion protection, 1h access-token TTL.
- **Memory** (`packages/agentcore-memory/`) — per-tenant CMK; namespace template static at synth (only `{actorId}`/`{memoryStrategyId}`/`{sessionId}` vary at runtime); confused-deputy grant closed with `aws:SourceAccount` + `aws:SourceArn`.
- **Registry — tool SSOT migration** (`packages/agent-registry/`) — the Platform pipeline synthesizes environment-isolated GA Registries, versioned `CUSTOM` governance records, pipeline-owned tool aliases, a conditioned `RegistryReaderRole`, and versioned SSM discovery parameters alongside the unchanged DynamoDB rollback path. R2's stable tool-ID subscriptions, Platform-side context resolution, stable Workstream role stage, explicit Lambda-permission handoff, and deploy-time `APPROVED` + descriptor-digest + target-ARN validation are pipeline/live-verified in nonproduction and production. The legacy consumer and DynamoDB tables remain available for rollback until a live rollback deployment passes.
- **Per-developer entitlement** (`packages/tool-cedar-wrapper/`) — a record may carry `metadata.allowedGroups`; when set the Gateway is forced into `CUSTOM_JWT` and the per-tool Cedar bundle binds each permit to a `CognitoGroup`. The pipeline-owned AgentCore Gateway PolicyEngine path has passed IAM-principal behavior parity, rollback, production deployment, and zero-residual teardown. The Lambda wrapper remains active as a deliberate rollback and defense-in-depth control pending a separate maintainer retirement decision.

### 2.5 Other constructs

- **RAG** (`packages/rag/`) — per-tenant CMK source bucket, versioned + access-logged + SSL-enforced, scoped `kbs/<tenant>/<kb>/` prefix, bucket policy denies any request not arriving via the workload VPCE.
- **LiteLLM (D-01)** (`packages/litellm-gateway/`) — per-account ECS Fargate behind an internal ALB, task-role allow-list + deny-on-null-guardrail, master key from Secrets Manager (CMK, injected via ECS `secrets:`).
- **Tenancy** (`packages/agentic-app/`) — per-app IAM role, per-app SG, memory namespace locked at synth, cost-allocation tags for per-app CUR.
- **Observability** (`packages/observability/`) — OAM source link to the Audit account, per-app dashboard + guardrail/latency alarms.
- **Cost** (`packages/cost-allocation/`) — per-app Budget filtered by `application-id`, alerts at 80 % ACTUAL + 100 % FORECASTED.
- **CI/CD** (`pipelines/`) — self-mutating platform pipeline + per-app workload pipeline with the mandatory sequence _Source → Synth → Deploy(nonprod) → Evaluation Gate → Manual Approval → Deploy(prod)_.

Evaluation-gate thresholds (defaults, overridable via `cdk.context.json`): regression pass ≥ 95 %, guardrail violation ≤ 1 %, LLM-as-judge quality ≥ 85 %, tool success ≥ 98 %, first-token p99 ≤ 1500 ms.

---

## 3. Deviations

Every conscious divergence from the source spec is recorded with the affected clauses, rationale, residual risks, and compensating controls. All three are publishable.

### 3.1 D-01 — LiteLLM in the inference path

Agents call Bedrock through a per-workload-account LiteLLM deployment rather than directly. The inference boundary shifts inside the account; the per-account quota/CUR/audit boundary is preserved.

- **Rationale.** Virtual-key per-team budgets (429-on-exceed), per-team cost attribution, unified observability, reuse of mature existing code.
- **Compensating controls.** Guardrails enforced three ways (LiteLLM `default_on` + IAM deny-on-null + VPCE policy); model allow-list SSOT; agent identity forwarded via Bedrock session tags for CloudTrail attribution; per-account deployment enforced at synth; PrivateLink-only; CMK everywhere.

### 3.2 D-02 — IaC authored in AWS CDK, not Terraform

Infrastructure is authored in AWS CDK (TypeScript + Python), synthesising CloudFormation. The spec's Terraform examples are re-implemented as CDK constructs with equivalent control semantics — same SCP bodies, VPCE policies, resource-based policies, Cedar policies, CMK wiring, guardrail attachment.

- **Compensating controls.** Every deviating construct cites the spec § it implements; cdk-nag `AwsSolutionsChecks` + `NIST80053R5Checks` Aspects mandatory; build-time SCP-size check; every suppression carries an inline `SEC-0NN` marker with owner, rationale and compensating control.

### 3.3 D-03 — Centralised-platform pattern

LiteLLM, API Gateway, WAF, Cognito, AgentCore Gateway, Registry, shared base-image ECR, and experiment-tracking DynamoDB live in the platform account and are consumed cross-account by agents on AgentCore Runtime in workload accounts. Memory stays in the workload account.

- **Rationale.** Central AI Platform team owns LiteLLM/Gateway/guardrails; product teams own agents. One deployment to upgrade; faster workload onboarding; consolidated guardrail enforcement.
- **Residual risks + compensating controls.** Platform SPOF → multi-AZ + per-env isolation + evaluation-gate-gated pipeline; per-workload quota → LiteLLM virtual-key budgets; **per-workload CUR** → platform-owned per-tenant Application Inference Profiles (the real fix for `BUG-005`: `sts:TagSession` does not survive role chaining); cross-account AssumeRole → `ExternalId` + `PrincipalArn`/`RoleSessionName` conditions; JWT replay → `tenantId` claim asserted against `sts:SourceAccount`; shared Registry/ECR tampering → per-tenant scoping + platform-pipeline-only writes; **per-developer scoping** → Cedar entitlement (`allowedGroups` + `CUSTOM_JWT` + `@agenticai/tool-cedar-wrapper`, fail-closed). **Gateway PolicyEngine migration**: the Workload pipeline's opt-in `LOG_ONLY`/`ENFORCE` path passed behavior parity, mode rollback, production deployment, in-place semantic-search removal, fail-closed teardown, and zero unintended residue on exact commit `f45a12c`. The wrapper remains intentionally active until a separate maintainer decision retires that rollback and defense-in-depth control.
- **Equivalence obligations.** Guardrail-on-every-call, per-workload cost attribution, per-workload audit trail, model allow-list SSOT, tenancy isolation, and network isolation are all preserved and CI-asserted. Two-account live verification (2026-05-01): 12/12 behavioural assertions PASS; re-verified end-to-end 2026-07-02.

New deviations require product-owner sign-off documenting: affected spec clauses, rationale, residual risks, compensating controls, equivalence obligations, and the CI conformance tests that assert them.

---

## 4. AWS Services Used

Amazon Bedrock · Amazon Bedrock AgentCore (Runtime, Gateway, Identity, Memory, Registry) · Bedrock Guardrails · Bedrock Application Inference Profiles · AWS Organizations · AWS Control Tower · AWS IAM · IAM Identity Center · AWS STS · Amazon API Gateway · AWS WAF · Amazon Cognito · Amazon VPC + PrivateLink · Amazon ECR · AWS KMS · AWS CloudTrail · Amazon CloudWatch (+ cross-account OAM) · Amazon S3 (+ S3 Vectors) · AWS Service Quotas · AWS CodePipeline / CodeBuild · AWS Lambda · AWS Step Functions · AWS Cost and Usage Report · AWS Security Hub · Amazon GuardDuty · AWS Config · Amazon Inspector · AWS Secrets Manager · Amazon DynamoDB · Amazon ECS (Fargate, Graviton) · AWS Certificate Manager · AWS Budgets · Amazon Verified Permissions (Cedar).

---

## 5. Prerequisites

- **AWS Control Tower** landing zone (documented hard prerequisite).
- **AWS CLI** ≥ 2.15, **Node.js** ≥ 20 LTS, **Python** ≥ 3.12, **AWS CDK** ≥ 2.150.0.
- Bedrock model access approved for Claude Sonnet 4.5 + Haiku 4.5 in the target account(s).
- IAM Identity Center user with management-account access for the initial deploy.
- A GitHub repo + AWS CodeStar Connections V2 connection (CI/CD only).
- Corporate VPC CIDR allocation (or accept defaults `10.20.0.0/16`, `10.21.0.0/16`).

---

## 6. Deployment

### 6.1 One-time setup

```bash
git clone https://github.com/aws-samples/sample-ai-agent-factory.git
cd sample-ai-agent-factory/enterprise-agentic-ai-platform-blueprint
npm ci
npm run build
npm test     # full Jest suite must pass

# Populate cdk.context.json with your account IDs + emails + CIDRs, then bootstrap:
export CDK_DEFAULT_ACCOUNT=<MGMT_ACCT>
export CDK_DEFAULT_REGION=us-west-2
npx cdk bootstrap "aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION" --qualifier hnb659fds
```

> **Least privilege.** Set the CDK CloudFormation execution policy to a customer-managed policy scoped to the services these stacks provision — do **not** use `AdministratorAccess`. See `pipelines/bootstrap/bootstrap-cross-account.sh` (requires `CFN_EXECUTION_POLICY_ARN`); the required scope is documented inline there. The Platform-account execution policy must additionally allow `iam:PassRole` on each target account's exact `cdk-hnb659fds-deploy-role-<account>-<region>` ARN with `iam:PassedToService=codepipeline.amazonaws.com`, and its exact `cdk-hnb659fds-cfn-exec-role-<account>-<region>` ARN with `iam:PassedToService=cloudformation.amazonaws.com`; CodePipeline validates both role classes when the cross-account pipeline is created. The Workstream execution policy must allow `iam:PassRole` on its exact pipeline-created/CDK Provider waiter roles with `iam:PassedToService=states.amazonaws.com`; this enables bounded Step Functions waiters without granting arbitrary service pass-through. When pipeline-owned Runtime/Memory is enabled, the Workstream execution policy must also allow native Runtime/Memory create, read, tag, and delete actions on the exact environment-qualified resource families; `iam:PassRole` only on the exact `AgenticAI-D03-<environment>-<tenant>-<agent>-runtime` role with `iam:PassedToService=bedrock-agentcore.amazonaws.com`; and Memory cryptography/grant actions only on the exact Memory CMK with `kms:ViaService=bedrock-agentcore.<region>.amazonaws.com`. The same pipeline-owned Runtime/Memory path also needs `iam:PassRole` on the exact `AgenticAI-D03-<environment>-<tenant>-<agent>-imgscan` image-scan-gate role with `iam:PassedToService=lambda.amazonaws.com`; that role's own policy is limited to `ecr:DescribeImages`, `ecr:StartImageScan`, and `ecr:DescribeImageScanFindings` on the exact bootstrap container-assets repository, and grants no image, tag, or repository deletion. When Gateway PolicyEngine is enabled, the same execution role also needs `kms:CreateGrant`, `kms:Decrypt`, `kms:GenerateDataKey`, and `kms:DescribeKey` on the exact PolicyEngine CMK, constrained by `kms:ViaService=bedrock-agentcore.<region>.amazonaws.com` and the `aws:bedrock-agentcore-policy:policy-engine-arn` encryption context. The Management execution policy must allow Kinesis stream provisioning, lifecycle management of the exact `AgenticAI-LogArchive-CWLDestinationRole`, and `iam:PassRole` on that role only with `iam:PassedToService=logs.amazonaws.com`. For reversible nonproduction buckets, scope IAM role lifecycle plus managed-policy attach/detach to `Nonprod-LogArchive-CustomS3AutoDeleteObjects*`, pass that generated role only to `lambda.amazonaws.com`, and scope Lambda lifecycle actions to the matching function prefix.

> **Worked example.** [`examples/reference-deployment-us-west-2/`](examples/reference-deployment-us-west-2/) is a complete 7-account `us-west-2` walkthrough with a fully populated `cdk.context.json` template (placeholder account ids), the Phase 1 → 8 deploy sequence, and the matching teardown. Use it as the concrete reference for the abstract steps below.

### 6.2 Path A — Default distributed (D-01)

1. Deploy Org + OUs + SCPs sandbox-first; run `bash scripts/scp-sandbox-soak.sh` (all four denial tests must pass) before attaching SCPs to the Workloads OU.
2. Provision accounts via Control Tower Account Factory (Log Archive, Audit, Sandbox, platform ×2, workload ×2).
3. `bash pipelines/bootstrap/bootstrap-cross-account.sh` (with a scoped `CFN_EXECUTION_POLICY_ARN`).
4. Set `agenticai/inferenceModelRateLimits` to a JSON array of provider-qualified model IDs and positive RPM/TPM allocations. Example: `[{"qualifiedModelId":"openai.gpt-oss-120b","requestsPerMinute":10,"tokensPerMinute":10000}]`. The construct appends a zero-rate `*` fallback; omit the Gateway target-name prefix from each rate-limit key and use the `InferenceTargetName` output to construct target-qualified invocation routes.
5. `npx cdk deploy --context stage=pipeline ... AgenticAI-PlatformPipelineStack AgenticAI-WorkloadPipelineStack` — the pipeline self-mutates and deploys platform + workload stacks with the evaluation gate + manual approval.

The CodeConnections source is the parent `aws-samples/sample-ai-agent-factory` repository. Each pipeline synth step therefore enters `enterprise-agentic-ai-platform-blueprint/` before running npm/CDK commands; it also accepts a standalone checkout where this blueprint is already the repository root, and fails closed for any other source layout. After validating every nested stage assembly, a nested checkout moves the completed `cdk.out` back to the CodeBuild source root expected by `ShellStep`.

Both root pipelines use explicit CMK-encrypted artifact buckets with key rotation, five allocation tags, 30-day object expiry, seven-day incomplete-upload cleanup, and automatic object deletion on stack rollback or teardown. The Platform root also owns and uses the stable `AgenticAI-PlatformPipelineRole`; its ARN is wired directly into `GuardrailAdminRole` trust, so pipeline mode never depends on a pre-created or placeholder principal. When upgrading an existing pipeline that used CDK's generated role, review the root change set and repoint any external trust, KMS, or SCP references from the generated ARN before it is retired; one Platform pipeline per account is the supported cardinality. KMS keys use AWS's minimum seven-day pending-deletion window. The first Platform stage owns the shared Audit and Log Archive stacks; the production Platform stage does not create a second copy in the same Management/Governance account. If Platform nonproduction and production deliberately map to one account, nonproduction owns the stable `AgenticAI-GuardrailAdmin` role, production imports it, and only the regional production guardrail name gains a `-prod` suffix. Normal separate-account deployments retain the stable unsuffixed names in each account.

### 6.3 Path B — Centralised platform (D-03)

All Platform and Workstream mutations flow through their pipelines. Do **not**
run `cdk deploy` against a Workstream Gateway stack directly.

1. Deploy the Platform pipeline with R2 code and
   `agenticai/enableGaGatewayInvokePermissions=false` (the default). This
   creates environment-qualified tool Lambdas/aliases, updates Registry records
   to version `2.0.0`, and extends `RegistryReaderRole`; it deliberately does
   not reference Workstream role principals that do not exist yet.
2. Explicitly approve the updated Registry records after the template-bound
   preflight passes.
3. Resolve one non-secret context file per Platform environment. For separate
   Platform accounts, run each command with credentials for that environment:

```bash
python3 -m venv "$KIROCREW_SCRATCH/ga-registry-resolver"
"$KIROCREW_SCRATCH/ga-registry-resolver/bin/pip" install \
  --disable-pip-version-check \
  -r pipelines/requirements-ga-registry-resolver.txt

HEAD="$(git rev-parse HEAD)"
"$KIROCREW_SCRATCH/ga-registry-resolver/bin/python" \
  pipelines/resolve_ga_registry_context.py \
  --account-id '<PLATFORM_NONPROD_ACCOUNT>' --region us-west-2 \
  --environment nonprod --application-id demo --agent-id primary \
  --tenant-id demo --cost-centre engineering \
  --expected-tool-id tool-echo --expected-tool-id tool-ping \
  --source-revision "$HEAD" \
  --output "$KIROCREW_SCRATCH/ga-registry-nonprod.json"

"$KIROCREW_SCRATCH/ga-registry-resolver/bin/python" \
  pipelines/resolve_ga_registry_context.py \
  --account-id '<PLATFORM_PROD_ACCOUNT>' --region us-west-2 \
  --environment prod --application-id demo --agent-id primary \
  --tenant-id demo --cost-centre engineering \
  --expected-tool-id tool-echo --expected-tool-id tool-ping \
  --source-revision "$HEAD" \
  --output "$KIROCREW_SCRATCH/ga-registry-prod.json"
```

4. Create/update the Workload pipeline root with
   `agenticai/pipelineSelection=workload`,
   `agenticai/enableGaRegistryConsumer=true`, stable tool IDs, both context-file
   paths, both Workstream account/AZ tuples, and the Platform account IDs. This
   root-stack operation creates the pipeline only; the pipeline owns every
   Workstream mutation.
5. The Workload pipeline deploys three stable roles per environment in its
   `RegistryRoles` stage, exposes each exact `GatewayServiceRoleArn` stack
   output, then stops at `GatewayPermissionReady`.
6. Read the two `GatewayServiceRoleArn` outputs from the deployed nonproduction
   and production role stacks. Re-run the Platform pipeline with
   `agenticai/enableGaGatewayInvokePermissions=true` and
   `agenticai/gaGatewayServiceRoleArns` set to a JSON array containing exactly
   those two ARNs (one `AgenticAI-D03-nonprod-*-gw-svc` and one
   `AgenticAI-D03-prod-*-gw-svc`). The Platform synth validates their accounts,
   role-name shapes, uniqueness, and environment cardinality, then adds each
   permission only to its matching environment aliases; it never reconstructs
   a principal from Platform-side tenant or agent settings.
7. Approve `GatewayPermissionReady`. The Workload pipeline then deploys the
   nonproduction Gateway. Its validator assumes `RegistryReaderRole`, requires
   `APPROVED`, and compares both the live descriptor SHA-256 and the live target
   ARN to the exact synth-wired values before any target is created. After live
   nonproduction `tools/list` and `tools/call` proof, approve the dedicated
   `ProdGatewayApproval` action. GA mode deliberately omits the app evaluation,
   canary, and soak actions that require stacks it does not deploy; legacy/full
   agent mode retains those gates unchanged.

Gateway PolicyEngine migration is opt-in and defaults to `OFF`, which emits the
exact R2 rollback template. For the current `AWS_IAM` Workload pipeline path,
configure exact pathless caller-role ARNs separately for each environment and
start in `LOG_ONLY`:

```json
{
  "agenticai/gatewayPolicyEngineMode": "LOG_ONLY",
  "agenticai/gatewayPolicyEngineNonprodIamRoleArns": [
    "arn:aws:iam::<WORKLOAD_NONPROD_ACCOUNT>:role/<EXACT_RUNTIME_ROLE>"
  ],
  "agenticai/gatewayPolicyEngineProdIamRoleArns": [
    "arn:aws:iam::<WORKLOAD_PROD_ACCOUNT>:role/<EXACT_RUNTIME_ROLE>"
  ]
}
```

The stack converts each IAM role to the stable
`AgentCore::IamEntity::"arn:aws:sts::<account>:assumed-role/<role>"` principal,
compiles one strict `FAIL_ON_ANY_FINDINGS` / `ACTIVE` policy per tool against
its exact `<TargetName>___<ToolName>` action and Gateway ARN, and encrypts the
engine and child policies with a rotating customer-managed KMS key. Creation
orders exact Gateway-role permissions → six-minute propagation gate →
`LOG_ONLY` association → targets → exact target readiness → policies → requested
mode. AgentCore validates Cedar actions against the Gateway's live target schema,
so policies cannot precede their targets. The readiness waiter signs the modeled
trailing-slash `GetGatewayTarget` URI for each service-minted ID, requires its
exact expected name and `READY` status, and fails immediately on identity drift,
terminal status, or any `*_PENDING_AUTH` state. Optional semantic search is
kept only in the exact `OFF` rollback template: live `ENFORCE` testing showed
`tools/list` empty and direct calls policy-denied for an unpermitted principal,
while the search tool still returned both unauthorized schemas. Exact commit
`f45a12c` then proved that an in-place `UpdateGateway` removed the existing
search configuration without replacing any Gateway, engine, target, policy, or
CMK. In both environments the built-in search action was absent for permitted
and unpermitted principals and disclosed zero tool metadata. Association uses a
signed, idempotent convergence loop that retries only the live-proven transient
`Access denied while calling GetPolicyEngine` validation response; unrelated
validation errors fail immediately. The Gateway role scopes both KMS actions to
the exact PolicyEngine CMK. `kms:Decrypt` omits the FAS-oriented condition block
because live `GenesisPolicyEngineCheck` calls proved it did not authorize the
runtime decrypt; metadata-only `kms:DescribeKey` retains `kms:ViaService`. The
CMK key policy retains service conditions on grant creation, cryptography, and
validation, source and encryption-context conditions on cryptography, and an
operation/context-constrained grant-creation boundary. AgentCore's two
service-created grants are independently operation- and context-constrained.
Deletion holds the requested mode while policies delete, then reverses through
target deletion → zero-target barrier → `LOG_ONLY` → detach → Gateway and engine
deletion. In `ENFORCE`, removing permits before targets is default-deny; in
`LOG_ONLY`, the retained Lambda wrapper remains the enforcement backstop.
`CUSTOM_JWT` group policies use the separately live-proven
quoted-element candidate when a discovery URL is supplied directly to the
Gateway stack; pipeline JWT-authorizer wiring remains a later gate.

Start in `LOG_ONLY` and do not switch to `ENFORCE` until live decisions match
the Lambda wrapper. The reference campaign passed parity, mode rollback,
production deployment, and zero-residual teardown on exact commit `f45a12c`.
The wrapper remains active as an intentional rollback and defense-in-depth
control, and `OFF` remains the supported native-PolicyEngine rollback.

The Workload synth project uses the named
`AgenticAI-WLP-<tenant>-<agent>-RegistrySynth` role and assumes only the two
Registry reader roles. Its future executions resolve SSM/Registry context
just-in-time; developers commit stable tool IDs, never environment-specific
RegistryRecord IDs. The legacy `allowedToolIds` path remains the rollback mode
until a live rollback deployment from the R2 revision passes and maintainers
explicitly retire it.

`DEPRECATED` Registry records are terminal and cannot be updated or approved
again. Recover one without replacing the Registry, its reader role, tools, or
the retained DynamoDB rollback path by setting
`agenticai/gaRegistryRecordGenerations` to an environment-scoped, monotonically
increasing generation such as `{"nonprod":{"tool-echo":2}}`. First remove only
the terminal record through the governed cleanup path, then update the Platform
pipeline root with the generation and let the Platform pipeline create the new
record plus update its SSM pointer. Explicitly approve the replacement `DRAFT`
record before resuming Workload deployments. Never decrease or reuse a
generation, and never apply a generation to production without its own review.

The reference R2 Gateway stays in the pipeline Region (`us-west-2`) so no
uncontrolled CDK cross-region artifact support stack appears. A region override
requires its own secure replication-bucket design and independent live proof.

### 6.4 Validation

```bash
python3 tests/smoke/smoke.py            # read-only sanity checks
pytest tests/integration/ -v            # full D-03 harness (needs live creds)
```

Fast checks: `npm test` green; `npm run synth` emits the credential-free Management stack and is cdk-nag-clean with only the documented `SEC-0NN` suppressions. To validate the full pipeline topology, populate every required `agenticai/*` context value and run `npx cdk synth --strict --context stage=pipeline`. In a deployed environment, confirm SCPs are attached, VPCEs are present, `bedrock:InvokeModel` without a `GuardrailIdentifier` returns `AccessDenied`, and a non-allow-listed model returns `AccessDenied`.

Repository hygiene gates, runnable locally and suitable for wiring into CI: `npm run scrub` (fails on any AWS account ID, internal reference, or hardcoded developer path in the tree) and `gitleaks detect --config .gitleaks.toml`.

### 6.5 Common issues

| Symptom                                                                                                                                                   | Cause                                                                                                                                                                                                                                                                                                                                                                             | Fix                                                                                                                                                                                                                                                                                                                                                                                          |
| --------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cdk bootstrap` `sts:AssumeRole` denied                                                                                                                   | Target not set up for cross-account trust                                                                                                                                                                                                                                                                                                                                         | Assume admin in the target first, re-run                                                                                                                                                                                                                                                                                                                                                     |
| Bedrock `AccessDenied`                                                                                                                                    | SCP-01/02 not matched                                                                                                                                                                                                                                                                                                                                                             | Confirm model on allow-list + `GuardrailIdentifier` supplied                                                                                                                                                                                                                                                                                                                                 |
| Cross-account KMS decrypt fails                                                                                                                           | Bootstrap `aws-cdk-lib` < 2.150                                                                                                                                                                                                                                                                                                                                                   | Re-bootstrap ≥ 2.150                                                                                                                                                                                                                                                                                                                                                                         |
| D-03 `CreateGateway` `not authorized`                                                                                                                     | Fresh CR role IAM not yet propagated to AgentCore                                                                                                                                                                                                                                                                                                                                 | Use the pipeline-created `RegistryRoles` stage; legacy standalone mode must rely on the built-in propagation gate                                                                                                                                                                                                                                                                            |
| D-03 `CreateGatewayTarget` "role lacks permission to invoke Lambda"                                                                                       | `GatewayPermissionReady` was approved before the Platform permission phase completed                                                                                                                                                                                                                                                                                              | Keep the Workload pipeline paused; run the Platform pipeline with `agenticai/enableGaGatewayInvokePermissions=true`, verify exact alias permissions, then approve the handoff                                                                                                                                                                                                                |
| RuntimeMemory `CreateOauth2CredentialProvider` denies `CreateTokenVault`                                                                                  | The provider creator lacks AgentCore's dependent permission to initialize the account's `default` token vault                                                                                                                                                                                                                                                                     | Grant `bedrock-agentcore:CreateTokenVault` only on the exact `token-vault/default` ARN; include `TagResource` when passing mandatory tags; keep lifecycle actions in the scoped custom-resource role                                                                                                                                                                                         |
| RuntimeMemory `CreateWorkloadIdentity` reports `already exists` as `ValidationException`                                                                  | A prior failed custom-resource create left the exact deterministic identity without ownership tags                                                                                                                                                                                                                                                                                | Verify exact name and ARN; permit only the known empty-tag migration, apply and re-read all five ownership tags, and reject every foreign or partial tag set                                                                                                                                                                                                                                 |
| RuntimeMemory `CreateWorkloadIdentity` denies `bedrock-agentcore:TagResource` on `.../workload-identity/*`, then on `workload-identity-directory/default` | Per the `bedrock-agentcore` service reference, `CreateWorkloadIdentity` authorizes on both `workload-identity` and its parent `workload-identity-directory` (likewise `CreateOauth2CredentialProvider` on `oauth2credentialprovider` + `token-vault`), and create-time tags are evaluated as `TagResource` on those same resources; exact-ARN or family-only scoping fails closed | Scope the lifecycle and create-time `TagResource` actions to the two default containers plus the two deterministic families; keep post-create `ListTagsForResource`/`TagResource` on the exact ARNs; never a bare `*`; prove it with `iam:SimulateCustomPolicy` against the exact denied pairs before redeploying (`scripts/live-agentcore-generated-agent-spike/simulate_idprov_policy.py`) |
| Platform R2 `Registry.Deploy` denies `iam:CreateRole` for a generated `ServiceRole-*` name                                                                | Tool Lambdas were relying on CDK auto-generated role names outside the scoped `AgenticAI*` deployment boundary                                                                                                                                                                                                                                                                    | Use the explicit `AgenticAI-Platform-<environment>-<tool>-exec` roles emitted by the R2 tools construct; do not widen the execution policy to arbitrary role names                                                                                                                                                                                                                           |
| Workload Synth denies `agent-registry:ListTagsForResource`                                                                                                | `RegistryReaderRole` can read records but cannot verify their five ownership tags                                                                                                                                                                                                                                                                                                 | Deploy the R2 reader policy that scopes `ListTagsForResource` to the exact Registry and its `/record/*` family before retrying the Workload pipeline                                                                                                                                                                                                                                         |
| Registry validator reports `getaddrinfo ENOTFOUND agent-registry-control.<region>.amazonaws.com`                                                          | GA Agent Registry resolves on its `api.aws` hostname from Lambda                                                                                                                                                                                                                                                                                                                  | Use `agent-registry-control.<region>.api.aws` while retaining SigV4 service name `agent-registry`                                                                                                                                                                                                                                                                                            |
| ToolGateway rollback cannot delete `GatewayResource` and CloudTrail shows a friendly `AgenticAI-D03-Gateway-*` identifier                                 | The custom resource retained a synthetic physical ID instead of the service-minted Gateway ID                                                                                                                                                                                                                                                                                     | Persist `CreateGateway.gatewayId` with `PhysicalResourceId.fromResponse("gatewayId")` and use `PhysicalResourceIdReference` for update/delete                                                                                                                                                                                                                                                |
| ToolGateway stack reaches `DELETE_COMPLETE` but the Gateway remains and CloudTrail says targets are still associated                                      | `DeleteGatewayTarget` is asynchronous even after its custom resource reports complete                                                                                                                                                                                                                                                                                             | Keep a dependency-ordered `TargetDeleteBarrier` between targets and Gateway; its waiter polls `ListGatewayTargets` to empty before `DeleteGateway` runs                                                                                                                                                                                                                                      |
| A GA Registry record is `DEPRECATED` and status/update calls report a terminal state                                                                      | `DEPRECATED` records cannot return to `DRAFT` or `APPROVED`                                                                                                                                                                                                                                                                                                                       | Remove only the terminal record through governed cleanup, increment its environment-specific `agenticai/gaRegistryRecordGenerations` value, and let the Platform pipeline recreate it and update the SSM pointer; explicitly approve the new record                                                                                                                                          |
| Pipeline M2M token endpoint cannot resolve                                                                                                                | Hosted-domain URL was built with the AWS API suffix                                                                                                                                                                                                                                                                                                                               | Derive it from `UserPoolDomain.baseUrl()`; Cognito managed domains use the `amazoncognito.com` suffix                                                                                                                                                                                                                                                                                        |
| `subnets in unsupported AZ`                                                                                                                               | AgentCore supports only `use1-az1/az2/az4` in `us-east-1`                                                                                                                                                                                                                                                                                                                         | Filter subnets by AZ ID (`AgentcoreCompatibleSubnetIdFirst` output)                                                                                                                                                                                                                                                                                                                          |

Rollback: `npx cdk destroy <stack>` per-stack, or `bash scripts/teardown.sh` for the full sweep.

---

## 7. Running the Guidance

Three Strands blueprints ship at v1 under `blueprints/` (plus LangGraph + CrewAI reference agents):

| Blueprint                 | Model mix                         | Pattern                                                   |
| ------------------------- | --------------------------------- | --------------------------------------------------------- |
| `agenticai-task-agent`    | Haiku 4.5                         | Deterministic single-shot; max-iteration guard; streaming |
| `agenticai-chatbot-agent` | Sonnet 4.5 / Haiku 4.5            | Multi-turn; HITL escalation; Customer-Facing guardrail    |
| `agenticai-multi-agent`   | Sonnet supervisor + Haiku workers | Supervisor + N-worker dispatch                            |

Under D-01 you invoke via the per-workload LiteLLM endpoint fronted by API Gateway. Under D-03 agents run on AgentCore Runtime and reach tools through the workstream MCP Gateway and Bedrock via cross-account AssumeRole. Next steps: add a workload app (§13), swap the guardrail profile (§11), tune eval thresholds (§11), or add a region (§11).

---

## 8. Cost

Rough estimates (no measured 24-hour baseline yet — see §15). `us-west-2` pricing, 2026.

| Traffic profile                          | Monthly (USD, approx) |
| ---------------------------------------- | --------------------- |
| Dev / low (10K invocations/day, 500 tok) | ~$280                 |
| Moderate (100K/day, 1K tok)              | ~$1,100               |
| High (1M/day, 1.5K tok)                  | ~$9,000               |

At moderate traffic the largest lines are Bedrock inference (~$600, Haiku ≈ 4× cheaper than Sonnet) and the 11 interface VPCEs across 3 AZs (~$240). Optimisation levers: route tolerant workloads to Haiku, Flex tier for dev/test, batch inference, Provisioned Throughput for sustained steady-state, and tuning CloudWatch retention. Per-app Budgets (filtered by `application-id`) alert at 80 % ACTUAL / 100 % FORECASTED; override via `agenticai/monthlyBudgetUsd` + `agenticai/notificationEmail`.

LiteLLM's virtual-key spend view is the budget-alert source of truth; CUR is the chargeback source of truth. They diverge for retries, cached responses, and guardrail short-circuits — reconcile monthly (target drift ≤ 1 %).

---

## 9. Operations

**SLOs.** First-token p99 ≤ 1500 ms; guardrail violations ≤ 1 %; tool-call success ≥ 98 %; session success ≥ 95 %; API Gateway 5xx ≤ 0.1 %. Each has a CloudWatch alarm feeding a CMK-encrypted SNS topic.

**Runbooks** (`scripts/` + dashboards). Key incident classes and first moves:

- **Guardrail-violation spike** — inspect the dashboard, pull offending prompts from `/agenticai/bedrock-invocations`, classify (content / prompt-attack / PII / denied-topic), mitigate (WAF rule, pause agent via ECS scale-to-0 + API GW throttle, upgrade guardrail profile), add a regression case to the blueprint's `eval/cases.jsonl`.
- **Prompt injection** (OWASP LLM01) — capture sessions, isolate source (Cognito block for direct; S3 version-revert for poisoned RAG), add adversarial regression cases.
- **Tool-call spiral** — pull the session trace, identify the loop, cancel in-flight runs; prefer better termination signals over raising the max-iteration ceiling.
- **Bedrock throttling (429)** — check `ThrottledCount` vs quota, shed load via WAF rate-limit / route to Haiku, then request a quota increase or move to Provisioned Throughput.
- **LiteLLM p99 regression** — check ECS CPU/memory + Bedrock-side latency + VPCE/ALB health; scale the service or cycle tasks.
- **MCP target outage** — identify the failing target in the Gateway logs, circuit-break its Cedar route, activate fallbacks.
- **Cross-account KMS / SelfMutate failures** — usually a bootstrap `aws-cdk-lib` gap or a direct `cdk deploy` against the pipeline stack; re-bootstrap ≥ 2.150 or always flow changes through the pipeline.

**Change management.** All prod changes flow through the pipeline (evaluation gate + manual approval); no out-of-band `cdk deploy` to prod. Reviews: Well-Architected quarterly, cost monthly, security-exception expiry monthly, SCP drift quarterly, dependency/SBOM monthly, red-team quarterly. Quarterly chaos experiments (Bedrock throttling, VPCE failure, ECS task kill, KMS pending-delete) each carry a hypothesis + stop criteria.

---

## 10. Security

### 10.1 Threat model

STRIDE + OWASP LLM Top 10 + MITRE ATLAS applied to blueprint-authored controls (customer deployments extend it). Highlights:

- **Spoofing** — Cognito JWT authorizer at API Gateway; RBPs with `aws:SourceArn`/`aws:SourceAccount`; LiteLLM forwards agent identity for CloudTrail attribution.
- **Tampering** — SCPs inherited from the OU (workload IAM cannot override); SCP-05 guardrail-mutation deny; immutable-tag ECR + scan-on-push; config rendered from SSOT at synth.
- **Information disclosure** — Guardrail PII BLOCK/ANONYMIZE + AU regex; per-tenant IAM + SG + memory namespace + `dynamodb:LeadingKeys`; public-access-block + CMK on every bucket; `kms:ViaService` + `kms:CallerAccount` on cross-account grants; `scripts/scrub-security-leakage.sh` + gitleaks on every change.
- **Denial of service** — WAF rate limit at API Gateway; LiteLLM virtual-key budgets; per-account Bedrock quotas; agent max-iteration guard; circuit breaker on MCP targets.
- **Elevation of privilege** — ExternalId + `PrincipalArn`/`RoleSessionName` conditions on cross-account roles; pipeline role scoped to bootstrap roles; AgentRuntime trust is service-principal-only (`allowLocalRootAssume` opt-in, hard-disabled in prod).

**OWASP LLM Top 10** — prompt injection (guardrail PROMPT_ATTACK + pinned system prompts), insecure output (guardrail output filters + eval quality score), model DoS (rate limits + quotas), supply chain (Dependabot + license-check + SBOM + pinned SDK + image scan), sensitive-info disclosure (PII filters + Memory actor-scoping), insecure plugin design (Registry-declared tools + scoped Gateway targets), excessive agency (max-iteration + HITL), overreliance (evaluation gate + human review).

### 10.2 Security exceptions

Every cdk-nag / cfn-nag suppression carries an inline `SEC-0NN` marker recording the requirement, the justification, and the compensating control — 24 in total (`SEC-001`..`SEC-016`, `SEC-022`..`SEC-029`), each visible on the suppression itself in the CDK source. Suppressions without a marker fail CI. The markers distinguish **service-limitation** exceptions (e.g. AgentCore's action-family evaluator rejecting narrow per-action lists; Bedrock guardrail admin APIs lacking resource-level ARNs — reviewed when the upstream service adds support) from **framework** exceptions (CDK custom-resource / Provider internals — reviewed on each `aws-cdk-lib` major bump).

### 10.3 Shared responsibility

- **AWS** — managed services under the Shared Responsibility Model.
- **Platform team** — blueprint code, platform-account stacks, SCPs, base guardrail, CI/CD, Registry, shared ECR.
- **Delivery team** — per-application workload code (agent container, tools, prompts, eval corpus, inference-profile tagging, guardrail-profile selection from the approved list) and privacy compliance for any personal data they store (see the note in `packages/developer-access/src/workstream-roster.ts`).

Report security issues privately via the [AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/) — **not** public GitHub issues.

---

## 11. Choice architecture

A customer should never have to fork the repo to make a supported variant. Every recognised override:

| Decision                           | Default                    | Override                                                                         |
| ---------------------------------- | -------------------------- | -------------------------------------------------------------------------------- |
| Identity provider                  | Cognito                    | `agenticai/customJwtIssuer` + `customJwtAudience` (corporate OIDC)               |
| Guardrail profile (per agent)      | Baseline                   | per-agent `blueprints/<name>/bedrock.config.yaml`                                |
| Model allow-list                   | Sonnet 4.5 + Haiku 4.5     | `PLATFORM_ALLOWED_MODELS` constant (forces platform review)                      |
| Region                             | `us-west-2`                | `packages/platform-baselines/src/approved-regions.ts` + SCP-06 sandbox-soak      |
| Eval thresholds                    | see §2.5                   | `agenticai/eval*` context keys                                                   |
| Gateway fronting                   | API Gateway (§08 Option A) | hard default                                                                     |
| Gateway PolicyEngine migration     | `OFF`                      | `agenticai/gatewayPolicyEngineMode` (`LOG_ONLY` before `ENFORCE`)                |
| Pipeline Runtime/Memory foundation | Off                        | `agenticai/enablePipelineRuntimeMemory=true` (BETA; `PUBLIC`, inert agent only)  |
| Browser egress / Lattice endpoints | Off                        | `agenticai/enableBrowserInternetEgress` / `enableLatticePrivateEndpoints` (BETA) |

An override that breaks a spec MUST (e.g. adding a non-Claude model) becomes a new deviation in §3.

---

## 12. Compliance

### 12.1 Well-Architected + GenAI Lens

The blueprint maps to all six pillars: **Operational Excellence** (dashboards + OAM + self-mutating pipeline + eval gate + runbooks), **Security** (Identity/SCPs/RBPs/VPCE/WAF/Cedar, PrivateLink-only, CMK everywhere, CloudTrail + GuardDuty + Security Hub), **Reliability** (quota requests, 3-AZ, per-account isolation, versioning), **Performance** (Sonnet/Haiku per-agent, `ConverseStream`, eval p99 gate), **Cost** (per-app Budgets + CUR attribution + allow-list caps), **Sustainability** (Graviton, on-demand, Haiku for low-stakes, TTLs). The GenAI Lens considerations (model governance, RAG, evaluation, responsible-AI guardrails, observability, spiral detection, HITL, red-team) each map to a named construct.

### 12.2 NIST 800-53 Rev 5

**First-pass derivation** — the blueprint maintainers' interpretation, **not** an authoritative attestation. Customers under formal regimes (FedRAMP, IRAP, ISM, HIPAA, PCI-DSS) must validate against their own catalogue and run their ATO process. Heavy coverage in AC (SCPs, per-app IAM, tenant RBPs, VPCE, Cognito, Cedar) and SC (PrivateLink-only, VPCEs, CMK, TLS 1.2+, region allow-list); AU (CloudTrail + Model Invocation Logging), SI (guardrails + eval gate), SR (pinned deps + SBOM), PT (PII filters + Memory scoping). PE/PS inherited. For each control the blueprint emits the CFN template, cdk-nag `NagReport.csv`, CloudTrail events, and a conformance-test assertion.

### 12.3 EU AI Act

`ConformityAssessmentConstruct` (`@agenticai/eu-ai-act-compliance`) wires Article-by-Article controls. Default risk classes (Article 6): chatbot=`limited`, task=`limited`, multi-agent=`high`. It auto-generates `technical-documentation.md` / `risk-assessment.md` / `human-oversight-protocol.md` at deploy into an Object-Lock COMPLIANCE 7-year record-keeping bucket. Article 9 (risk management) → online-evaluation watchdog; Article 10 (data governance) → eval-corpus GOVERNANCE bucket + manifest SHA envelope; Article 14 (human oversight) → `HumanInTheLoopConstruct`; Article 15 (accuracy/robustness) → evaluation gate + kill-switch + circuit breaker. High-risk Articles 9–17 take effect August 2026.

---

## 13. Multi-account topology

**Adding a workload application** (per workstream): provision two accounts via Account Factory (`agenticai-<ws>-nonprod` / `-prod` under `AgenticAI-Workloads`), bootstrap both with trust to platform-nonprod, deploy `D03PlatformCoreStack` (which also emits the workstream's Identity Center permission sets + `RegistryConsumerGrant` + roster row), then a `WorkloadPipelineStack` instance. The developer then works entirely from the workstream account via the `agenticai` CLI (§1.1).

**Account closure.** `bash scripts/teardown.sh` destroys stacks in reverse dependency order (`RETAIN` resources remain by design). Manual remaining steps: empty + delete retained S3 buckets, cancel KMS keys pending deletion, `aws organizations close-account`. Accounts enter `SUSPENDED` for ≥ 90 days before Organizations deletes them.

**Edge cases.** No-Control-Tower fallback (advanced; replace Account Factory with `organizations:CreateAccount` + manual baseline). Existing Log Archive/Audit via `agenticai/adoptExistingLogArchive`. Shared-services TGW via `agenticai/transitGatewayId`. CIDR conflicts via `agenticai/vpcCidr`.

---

## 14. Architecture Decision Records

MADR-lite records for the non-obvious decisions (full text in Git history / earlier tags):

- **ADR-0001** — LiteLLM in the inference path (D-01); triple-gate guardrail preserved.
- **ADR-0002** — CDK + CloudFormation, not Terraform (D-02); AFT explicitly rejected.
- **ADR-0003** — API Gateway fronts AgentCore Gateway as the primary auth boundary (§08 Option A).
- **ADR-0004** — Memory namespaces static at synth (only `{actorId}`/`{memoryStrategyId}`/`{sessionId}` vary).
- **ADR-0005** — Model allow-list as a single TypeScript constant flowing into four enforcement surfaces.
- **ADR-0006** — EU AI Act posture: Object-Lock COMPLIANCE 7-year bucket + auto-generated conformity docs.
- **ADR-0007** — Evaluation gates as platform infrastructure; 7-category scoring SSOT shared by the offline gate + online watchdog.
- **ADR-0008** — Agent lifecycle: versioned manifests (SHA-256 envelope) + canary + auto-rollback.
- **ADR-0009** — Protocol-native MCP + A2A; `MCP_PROTOCOL_VERSION = 2025-06-18` locked; qualified tool names.
- **ADR-0010** — Kill-switch + circuit breaker as real runtime constructs (four live revoke branches; retry+fallback chain).
- **ADR-0011** — HITL reference construct: Step Functions `WaitForTaskToken` + Cedar approver scoping.
- **ADR-0012** — Multi-framework support (Strands / LangGraph / CrewAI) via lazy-imported adapters.
- **ADR-0013** — AgentCore Registry as tool SSOT (replaces the TypeScript catalogue truth claim).
- **ADR-0014** — Identity Center permission sets per workstream (`AgenticAI-WS-Dev-/Ro-/Apv-`; 16-char workstream-id cap).
- **ADR-0015** — Developer CLI as pure-function helpers sharing the eval-scoring SSOT.

---

## 15. Known limitations and honest disclaimers

Know what **has** been live-verified and what **has not** before adopting.

**Live-verified on real AWS.**

- **Central AgentCore inference Gateway U-1** (`us-west-2`, 2026-09-18): Bedrock Mantle target reached `READY`; model discovery returned 49 models; IAM and Cognito M2M inbound authentication worked; Strands `LiteLLMModel` 1.44.0 passed streaming and non-streaming; the same positive-twin model returned exact HTTP 429 under a zero-rate `qualifiedModelId` rule; teardown and independent inventory found zero Gateway/IAM/Cognito residue. See [`evidence/live/2026-09-18-agentcore-gateway-spike.md`](evidence/live/2026-09-18-agentcore-gateway-spike.md).
- **Platform pipeline deployment and invocation** (`us-west-2`, 2026-09-19): exact commits `f037b4e`, `2ca8272`, and `0ef7f50` completed reviewed Platform pipeline executions through production. The Management/Governance Log Archive is live; the production Guardrail, AgentCore Gateway, and inference target are `READY`; the native rate limit is `ACTIVE`; Cognito M2M and 49-model discovery passed; and Strands `LiteLLMModel` passed streaming and non-streaming against the pipeline-owned endpoint. See [`evidence/live/2026-09-19-platform-pipeline-deployment.md`](evidence/live/2026-09-19-platform-pipeline-deployment.md).
- **Gateway PolicyEngine compatibility** (`us-west-2`, 2026-09-19): exact commit `46c3a62` passed eight `FAIL_ON_ANY_FINDINGS` Cedar policies, 20 subject/group decisions, four filtered tool lists, direct-call denial, exact 401/403 JWT negatives, expired-token denial, `ENFORCE → LOG_ONLY → ENFORCE`, terminal evidence preservation, and direct independent zero-residual inventory. This is the isolated API-contract proof that preceded the Workload pipeline migration. See [`evidence/live/2026-09-19-policyengine-compatibility-spike.md`](evidence/live/2026-09-19-policyengine-compatibility-spike.md).
- **Pipeline-owned Gateway PolicyEngine** (`us-west-2`, 2026-09-21): exact commit `f45a12c` passed nonproduction and production deployment, IAM-principal positive and direct-policy-denial twins, `LOG_ONLY → ENFORCE → LOG_ONLY → ENFORCE`, in-place semantic-search removal with zero metadata disclosure, exact principal restoration, fail-closed policy → target → zero-target → `LOG_ONLY` → detach teardown, service-grant retirement, exact log cleanup, and independent zero-unintended-residue inventory. The Lambda Cedar wrapper remains intentionally retained. See [`evidence/live/2026-09-21-pipeline-agentcore-policyengine.md`](evidence/live/2026-09-21-pipeline-agentcore-policyengine.md).
- **AgentCore Runtime and Memory compatibility** (`us-west-2`, 2026-09-21): exact commit `88d5381` passed a zero-finding, digest-pinned `linux/arm64` image build, Memory `ACTIVE`, Runtime `READY`, exact `InvokeAgentRuntime` handshake, exact short-term `CreateEvent`/`GetEvent` round trip, Runtime-before-Memory cleanup, service-grant retirement, exact service-log cleanup, and independent zero-active-residue inventory. This is an isolated nonproduction Platform-account API-contract proof, not Workload-pipeline integration. See [`evidence/live/2026-09-21-agentcore-runtime-memory-compatibility-spike.md`](evidence/live/2026-09-21-agentcore-runtime-memory-compatibility-spike.md).
- **Pipeline-owned AgentCore Runtime and Memory foundation** (`us-west-2`, 2026-09-22): exact commit `442de00` passed exact role/permission handoff, digest-bound zero-finding image admission and content-address drift rejection, nonproduction and production Gateway/Runtime/Memory deployment, Runtime and Memory round trips, MCP and bypass twins, live content-address rejection, complete no-op redeployment, access retirement, dependency-ordered teardown, grant retirement, exact log/image cleanup, and independent zero-unintended-residue inventory. The deployed Runtime intentionally remained an inert compatibility handler; generated-agent integration is not claimed. See [`evidence/live/2026-09-22-pipeline-agentcore-runtime-memory.md`](evidence/live/2026-09-22-pipeline-agentcore-runtime-memory.md).
- **GA Agent Registry compatibility** (`us-west-2`, 2026-09-19): exact commit `8e66dc3` passed native `AWS::AgentRegistry` resource creation, custom governance-document round trip, observed `DRAFT`, explicit submission to `APPROVED`, data-plane discovery, deterministic `UPDATE_ROLLBACK_COMPLETE`, original-record restoration, normal cleanup, and direct independent zero-residual inventory. This is an isolated API-contract proof, not the pipeline-owned migration. See [`evidence/live/2026-09-19-agent-registry-compatibility-spike.md`](evidence/live/2026-09-19-agent-registry-compatibility-spike.md).
- **Pipeline-owned GA Agent Registry R1** (`us-west-2`, 2026-09-20): exact producer commit `f3ec7d6` deployed through both Platform environments; exact utility commit `39a13ab` bound all four live records to their processed templates, observed `DRAFT`, submitted each explicitly, and independently verified `APPROVED` status, exact descriptor digests, and exact discovery sets. The legacy DynamoDB path remained active; wrong-principal and wrong-account trust twins were denied. See [`evidence/live/2026-09-20-pipeline-ga-agent-registry-r1.md`](evidence/live/2026-09-20-pipeline-ga-agent-registry-r1.md).
- **Pipeline-owned GA Agent Registry R2 consumer and Tool Gateway** (`us-west-2`, 2026-09-21): exact deployed commit `3870e0e` passed stable-ID cross-account resolution, exact Workstream role and Lambda-permission handoff, `APPROVED` + descriptor-digest + target-ARN validation, nonproduction and production Gateway deployment, MCP `tools/list` and `tools/call`, denial twins, no-op redeployment, fail-closed status drift, terminal-record generation recovery, and target → barrier → Gateway teardown. Teardown-hardening commit `7774299` recovered exact resources from deleted-stack history and removed 39 empty service-created log groups; independent inventory found zero unintended residue. See [`evidence/live/2026-09-21-pipeline-ga-agent-registry-r2.md`](evidence/live/2026-09-21-pipeline-ga-agent-registry-r2.md).
- **D-03 v3 full end-to-end tool-call round-trip** (2026-05-05, re-verified 2026-07-02): IAM user → AssumeRole → runtime role → MCP over the CUSTOM_JWT gateway → Gateway service role → cross-account `lambda:InvokeFunction` → tool Lambda → MCP `tools/call` reply, for both demo tools; unauthenticated gateway calls return `401`.
- **Per-developer Cedar entitlement** (2026-07-02): non-member JWT denied (`CedarDeniedError` before user code), member JWT allowed.
- **Gap-closure surface** (2026-05-15, re-verified 2026-07-02): eval-gates GOVERNANCE bucket, EU AI Act COMPLIANCE 7-year bucket + 3 conformity docs, agent-version GSIs + rollback Step Function, MCP probe, kill-switch Step Function (4 revoke branches), chargeback bucket, HITL Step Function, online-eval watchdog.
- **D-03 guardrail triple-gate, `dynamodb:LeadingKeys` tenant isolation, `kms:ViaService` cross-account scoping**, and **clean teardown to zero residuals**.

**Not live-verified or deliberately deferred.**

- **Lambda-wrapper retirement** — the pipeline-owned native PolicyEngine path passed parity, production deployment, rollback, and teardown, but `@agenticai/tool-cedar-wrapper` remains intentionally active as a rollback and defense-in-depth control. Retirement requires a separate maintainer decision and behavior-changing review cycle.
- **Exact 429 twin against the pipeline-owned production rate limit** — the rate limit is `ACTIVE` and positive discovery/inference passed, but its configuration was not mutated for a destructive-limit test. Exact HTTP 429 behavior is live-proven by the isolated compatibility spike.
- **Gateway OTEL rate-limit span correlation in `us-west-2`** — reproduced blocker: real HTTP 200/429 twins produced no `aws/spans` records after CloudWatch Logs trace destination was `ACTIVE`, Transaction Search indexing was 100%, delivery propagation was allowed, and six minutes of polling elapsed. X-Ray and indexing were restored and all run-owned resources were removed.
- **SCPs 01–12 org soak** — unit + regression tests only (`scp-bypass-regression.test.ts`, 13 cases); the primary IAM identity policies these SCPs defend-in-depth were verified least-privilege on the live roles.
- **Generated-agent integration through pipeline-owned Runtime/Memory** — the isolated and pipeline-owned Runtime/Memory foundations are live-proven, but the deployed handler remained intentionally inert. A generated agent has not yet proven secure AgentCore Identity M2M token acquisition, `LiteLLMModel`, `MCPClient`, and application-level Memory behavior through that pipeline. A safe pipeline-level induced RuntimeMemory rollback and live High-finding image rejection also remain open.
- **No measured 24-hour cost baseline.** **Control Tower landing zone** — documented prerequisite, tested on standalone accounts.
- **Regions** — `us-east-1` remains live-verified for the earlier D-03 tool-Gateway path. `us-west-2` is live-verified for the central inference Gateway, Platform pipeline, isolated and pipeline-owned PolicyEngine campaigns, isolated and pipeline-owned Runtime/Memory campaigns, and pipeline-owned R2 Registry/Tool Gateway slice, but remains blocked for OTEL span correlation. No EMEA PolicyEngine, Runtime/Memory, or R2 pipeline region has yet passed the full matrix. APAC remains outside `PLATFORM_APPROVED_REGIONS`.
- **VPC Lattice** private endpoints (AWS BETA, opt-in); **Entra Agent Identity** deferred to v2.

**Operational findings baked into the blueprint** (full detail in `CHANGELOG.md`): AgentCore supports only specific AZ IDs (`use1-az1/az2/az4` in `us-east-1`; AZ-ID filter output provided); the `MCP-Protocol-Version: 2025-06-18` header is required after `initialize`; the Gateway service role must exist before its ARN is added to tool Lambda resource policies (R2 creates it in `RegistryRoles`, pauses, then lets the Platform pipeline grant it); fresh custom-resource IAM roles take minutes to propagate to the AgentCore control plane (propagation gate + stable role stage provided); Lambda cross-account resource policies for Gateway targets must name the exact service-role ARN; `CreateRegistry`/`CreateRegistryRecord` return only ARNs (ids derived from them) and require descriptor `inlineContent` valid against the MCP/A2A schema.

The 35 packages under `packages/` are enumerated in [`CHANGELOG.md`](CHANGELOG.md) under the development phase that introduced each one (19 in the initial build-out, 8 gap-closure + 4 self-audit, 3 developer-experience, 1 entitlement).

---

## 16. Cleanup

```bash
export AGENTICAI_INFERENCE_MODEL_RATE_LIMITS='[{"qualifiedModelId":"openai.gpt-oss-120b","requestsPerMinute":10,"tokensPerMinute":10000}]'
bash scripts/teardown.sh     # reverse-dependency stack sweep
pytest tests/teardown/       # verify zero residuals
```

The teardown refuses to synthesize a present Platform Gateway or pipeline stack without the model-rate allocation and its account/role context. For an R2 Workload deployment, also provide both resolved GA context files, stable tool IDs, Workstream account/AZ tuples, and the Gateway Region. If pipeline Runtime/Memory is enabled, export `AGENTICAI_ENABLE_PIPELINE_RUNTIME_MEMORY=true` so destroy synthesizes the native Runtime/Memory stacks and captures their exact service-created Runtime logs. If PolicyEngine is enabled, export `AGENTICAI_GATEWAY_POLICY_ENGINE_MODE` plus both environment-specific IAM-role JSON arrays so destroy synthesizes the deployed graph rather than the default `OFF` graph. The script destroys production/nonproduction ToolGateway stacks before their RegistryRoles stacks and then removes the Workload pipeline root. A PolicyEngine CMK enters its configured seven-day pending-deletion window after the service retires both grants; this is intentional. Before each stack deletion, the script snapshots the exact physical IDs of its CodeBuild projects and Lambda functions; after a successful `cdk destroy`, it verifies those resources are absent and deletes only their exact service-created default CloudWatch log groups. A rerun against an already-absent stack recovers exact IDs from CloudFormation's deleted-stack event history, so failed cleanup and prior deployment generations remain recoverable without prefix-wide deletion. Missing groups are idempotent, while discovery, resource-absence, or deletion errors fail the teardown. A direct `cdk destroy` bypasses this cleanup and can leave empty `/aws/codebuild/*` or `/aws/lambda/*` groups. `cdk destroy` runs dependency-ordered. The EU AI Act Object-Lock COMPLIANCE 7-year bucket cannot be deleted before its retention expires — this is intentional and documented.

---

## 17. Contributors and License

Maintained by the AI Platform team. Issues and pull requests are welcome — see the [contribution guidelines](https://github.com/aws-samples/sample-ai-agent-factory/blob/main/CONTRIBUTING.md) and [code of conduct](https://github.com/aws-samples/sample-ai-agent-factory/blob/main/CODE_OF_CONDUCT.md) in the parent repository. Distributed under **MIT-0** — see [`LICENSE`](LICENSE). Report security issues privately via the [AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/), not a public issue.

---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.  
SPDX-License-Identifier: MIT-0
