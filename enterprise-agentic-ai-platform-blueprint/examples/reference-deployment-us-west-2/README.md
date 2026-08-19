# Reference Deployment — us-west-2 (7 accounts)

A runnable, end-to-end deployment of the Enterprise Agentic AI Platform Blueprint. See [`README.md` §13](../../README.md#13-multi-account-topology) for the multi-account topology this mirrors.

## Topology

| Role | OU | Account purpose |
|---|---|---|
| Management | Root | AWS Organization admin, SCPs, account provisioning |
| Log Archive | Security | Centralised CloudTrail + CUR + CWL destination |
| Audit | Security | CloudWatch OAM sink, Security Hub / GuardDuty master |
| Platform non-prod | AgenticAI-Platform | Guardrail Admin, Registry, CDK Pipelines |
| Platform prod | AgenticAI-Platform | Pinned guardrail, Registry, CI/CD production target |
| Workload non-prod (app1) | AgenticAI-Workloads | First agent app — nonprod deploy target |
| Workload prod (app1) | AgenticAI-Workloads | First agent app — prod deploy target |
| SCP sandbox | AgenticAI-Sandbox | Soaks new SCPs before promotion |

Total: **7 accounts** (optional Shared Services account for TGW/PHZ can be added later).

Region: **`us-west-2`**.

## Prerequisites

1. AWS Control Tower landing zone in your Organization management account.
2. AWS CLI ≥ 2.15, Node.js ≥ 20 LTS, Python ≥ 3.12, CDK ≥ 2.150.0.
3. A GitHub repository containing this blueprint with a [CodeStar Connection](https://docs.aws.amazon.com/dtconsole/latest/userguide/connections-create-github.html) authorising `aws-samples/sample-ai-agent-factory`.
4. Bedrock model access approved in the target region for Claude Sonnet 4.5 + Claude Haiku 4.5.
5. IAM Identity Center (SSO) user with AdministratorAccess on the management account.

## Configuration

Populate [`cdk.context.json`](./cdk.context.json) with:

- `agenticai/organizationId` — `o-xxxxxxxxxx`.
- Six account IDs (management, log archive, audit, sandbox, platform-nonprod, platform-prod, workload-nonprod, workload-prod).
- `agenticai/pipelineRoleArn` — the ARN of the pipeline execution role in platform-nonprod (created automatically by CDK Pipelines bootstrap).
- `agenticai/githubRepo` + `agenticai/githubConnectionArn` + `agenticai/notificationEmail`.

Example baseline is checked in; overwrite with real values before deploy.

## Deploy walkthrough

Full sequence, Phase 1 → Phase 8:

```bash
# 1. Install + build.
npm ci
npm run build
npm test

# 2. Bootstrap the management account.
CDK_DEFAULT_ACCOUNT=<MGMT_ACCT> npx cdk bootstrap aws://<MGMT_ACCT>/us-west-2 --qualifier hnb659fds

# 3. Deploy Org + SCPs (sandbox-first).
npx cdk deploy --context stage=management \
  --context agenticai/guardrailAdminRoleArn=arn:aws:iam::<PLATFORM_NP>:role/AgenticAI-GuardrailAdmin \
  AgenticAI-Management-OrgStack

# 4. Soak SCPs in the sandbox OU (run the four canonical denial tests below).
bash ../../scripts/scp-sandbox-soak.sh

# 5. Promote SCPs to AgenticAI-Workloads OU.
npx cdk deploy --context stage=management \
  --context agenticai/attachScpsToWorkloadsOu=true \
  AgenticAI-Management-OrgStack

# 6. Provision accounts via Control Tower Account Factory for:
#    - Log Archive, Audit, Sandbox, Platform-nonprod, Platform-prod,
#      Workload-app1-nonprod, Workload-app1-prod.
#    (Control Tower creates + enrolls; run from the management account.)

# 7. Bootstrap every new account with trust to platform-nonprod.
bash ../../pipelines/bootstrap/bootstrap-cross-account.sh

# 8. Deploy the platform + workload pipelines from platform-nonprod.
npx cdk deploy --context stage=pipeline \
  --context agenticai/githubRepo=... \
  --context agenticai/githubConnectionArn=... \
  AgenticAI-PlatformPipelineStack AgenticAI-WorkloadPipelineStack

# 9. Pipelines self-mutate and deploy all stacks end-to-end. Smoke tests
#    run automatically at prod stage exit.
```

## Validation checklist

After Phase 8 completes, verify:

- [ ] `aws organizations list-policies-for-target --target-id <AgenticAI-Workloads-OU-id>` returns 8 SCPs.
- [ ] `aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values=<workload-vpc>` returns 11 interface + 1 gateway endpoint.
- [ ] `aws bedrock invoke-model` in the workload account **without** `--guardrail-identifier` returns AccessDenied (SCP-02 + IAM + VPCE policy triple-gate).
- [ ] `aws bedrock invoke-model` with a non-allow-listed model ID returns AccessDenied (SCP-01 + VPCE policy).
- [ ] `aws oam list-attached-links --identifier <audit-sink-arn>` shows source links from the workload + platform accounts.
- [ ] `aws s3api get-object` against the RAG bucket from outside the workload VPC returns AccessDenied.
- [ ] Calling the API Gateway URL without a valid Cognito JWT returns 401.
- [ ] `aws budgets describe-budgets --account-id <workload>` returns the per-app budget.
- [ ] CloudWatch dashboard `agenticai-nonprod-app1-primary` renders with the Bedrock, LiteLLM, and guardrail widgets.

## Teardown

```bash
bash ../../scripts/teardown.sh
```

The script destroys stacks in reverse dependency order, empties retained buckets, and warns about 90-day Control Tower account-close windows. See [`README.md` §16](../../README.md#16-cleanup) for the full cleanup procedure.
