---
title: "Self-Paced Setup"
weight: 13
---

::alert[**Scripted self-service is available now.** Clone this repository and run one deploy script (below) to provision the full platform — including the browser IDE — into your own AWS account. The one-click CloudFormation console buttons and the published Workshop Studio asset bundle are still being prepared and will be added once the workshop is public; until then, use the scripted flow on this page.]{header="Self-paced setup" type="info"}

::alert[Provisioning this workshop environment in your own AWS account will create resources and there will be costs associated with them. For per-service pricing, see [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/), [Amazon ECS pricing](https://aws.amazon.com/ecs/pricing/), [Amazon DocumentDB pricing](https://aws.amazon.com/documentdb/pricing/), [Amazon Aurora pricing](https://aws.amazon.com/rds/aurora/pricing/), [Amazon CloudFront pricing](https://aws.amazon.com/cloudfront/pricing/), [AWS Lambda pricing](https://aws.amazon.com/lambda/pricing/), and [NAT Gateway pricing](https://aws.amazon.com/vpc/pricing/). The cleanup section provides a guide to remove all provisioned resources to prevent further charges.]{header="Warning" type="error"}

Follow these instructions if you are running this workshop independently in your own AWS account. The deploy script provisions the **same environment** that AWS-run events provision automatically — including the browser-based IDE — so every module works exactly as written, with no path changes.

## Prerequisites

### Local tooling

The deploy script runs from your laptop and needs only a few tools on your `PATH`:

| Tool | Why it's needed | Install |
|---|---|---|
| **AWS CLI v2** | Authenticates to your account and drives every CloudFormation, S3, and EC2 call in the deploy. | [Install AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| **`yq`** | The deploy script reads `contentspec.yaml` (stack list, parameters) with `yq`. | [`yq` (mikefarah)](https://github.com/mikefarah/yq) — `brew install yq`, or see the releases page |
| **Git** | To clone this repository. | [Install Git](https://git-scm.com/downloads) |
| **A modern browser** | Chrome, Firefox, or Edge — to open the workshop IDE. | — |

The deploy wrapper checks for `aws` and `yq` up front and exits with an install hint if either is missing.

::alert[You do **not** need to install Python, JupyterLab, Node.js, or the AWS CDK on your laptop. The workshop IDE that the deploy script provisions comes with all of these — plus the workshop source code — pre-installed. Every module command is meant to run in that IDE's terminal.]{type="info"}

### Region

Deploy into any of the **validated regions** below. The workshop has been built and tested against the Amazon Bedrock model and Amazon Bedrock AgentCore availability in these regions. Other regions are not supported — the Amazon Bedrock AgentCore Registry control plane is not yet generally available everywhere (it returns an internal error in regions such as `eu-central-1` and `ap-southeast-1`), which breaks Modules 3b and 4.

| Region | Location |
|---|---|
| `us-west-2` | US West (Oregon) — default |
| `us-east-1` | US East (N. Virginia) |
| `eu-west-1` | Europe (Ireland) |

The deploy script reads your region from your AWS CLI configuration and adapts automatically (model IDs and CloudFormation resources are region-agnostic). A preflight check confirms the required Bedrock and AgentCore capabilities are available in your region before any stack is created. Set your region with:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws configure set region <region>   # one of: us-west-2, us-east-1, eu-west-1
:::

::alert[Your account must have access to the required Bedrock models **in your chosen region** — model access is granted per region. If it does not, request it first in the **Bedrock console → Model access** page (confirm the console region selector matches your deploy region). Module 2 includes the full model-access steps.]{type="info"}

### AWS account and permissions

We recommend running this in a **dedicated AWS account** you can tear down afterwards, rather than a shared or production account — the deploy creates IAM roles, networking, and compute across many services.

The deploying principal needs permission to create that infrastructure (CloudFormation, EC2/VPC, ECS, Lambda, IAM roles, Secrets Manager, DocumentDB/RDS, Cognito, Bedrock, AgentCore, CloudFront, and more). Attach the **scoped deploy policies** included in this repository:

- [`static/cfn/self-service-deploy-policy-1.json`](https://github.com/awslabs/agentic-ai-platform/blob/main/workshop-agentic-ai-platform-agentcore/static/cfn/self-service-deploy-policy-1.json)
- [`static/cfn/self-service-deploy-policy-2.json`](https://github.com/awslabs/agentic-ai-platform/blob/main/workshop-agentic-ai-platform-agentcore/static/cfn/self-service-deploy-policy-2.json)
- [`static/cfn/self-service-deploy-policy-3.json`](https://github.com/awslabs/agentic-ai-platform/blob/main/workshop-agentic-ai-platform-agentcore/static/cfn/self-service-deploy-policy-3.json)
- [`static/cfn/self-service-deploy-policy-4.json`](https://github.com/awslabs/agentic-ai-platform/blob/main/workshop-agentic-ai-platform-agentcore/static/cfn/self-service-deploy-policy-4.json)

They grant only the **explicit actions the workshop stacks actually use** (no `service:*` wildcards), with every regional statement restricted to the validated regions via an `aws:RequestedRegion` condition. The action list is split across four files because it exceeds a single IAM managed policy's 6,144-character limit. Create each as a customer-managed policy and attach all four to the user or role that runs the deploy:

:::code{showCopyAction=true showLineNumbers=false language=bash}
for n in 1 2 3 4; do
  aws iam create-policy \
    --policy-name "AgenticPlatformWorkshopDeploy${n}" \
    --policy-document "file://static/cfn/self-service-deploy-policy-${n}.json"
done
# then attach each returned policy ARN to your deploy user/role, e.g.:
# aws iam attach-user-policy --user-name <you> --policy-arn <arn-from-above>
:::

::alert[Prefer not to manage four policies? `AdministratorAccess` on the deploying principal also works for a dedicated workshop account — the scoped policies are the least-privilege option for accounts where that matters.]{type="info"}

::alert[If your account is part of an AWS Organization and you want an extra guardrail, you can apply [`static/cfn/self-service-scp.json`](https://github.com/awslabs/agentic-ai-platform/blob/main/workshop-agentic-ai-platform-agentcore/static/cfn/self-service-scp.json) as a Service Control Policy — it restricts the account to the validated regions and blocks account/billing changes. This is optional hardening, not required to run the workshop.]{type="info"}

::alert[The deploy provisions a Code Editor IDE on an EC2 instance with a scoped instance role (it mirrors the workshop's permission set plus the CDK actions Module 4 needs). This is the environment you run module commands from — another reason to use a dedicated, disposable account and tear it down when finished.]{type="warning"}

::alert[**Do not run this in an account with automated resource-governance or "janitor" tooling.** The MCP Gateway & Registry stack stands up an Amazon DocumentDB cluster (plus Aurora PostgreSQL) that the registry service connects to during stack creation. Some corporate or sandbox accounts run schedulers that automatically delete or stop RDS/DocumentDB instances shortly after they are created — if one deletes the registry's DocumentDB instance, the registry ECS service can never connect and the `workshop-registry-stack` deploy fails (you will see repeated DNS resolution errors for the `*.docdb.amazonaws.com` endpoint in the registry container logs). Use an account where you can keep the databases running for the duration of the workshop.]{type="warning"}

## Clone the Workshop Repository

:::code{showCopyAction=true showLineNumbers=false language=bash}
git clone https://github.com/awslabs/agentic-ai-platform.git
cd agentic-ai-platform/workshop-agentic-ai-platform-agentcore
:::

::alert[The public `awslabs` repository URL above (`https://github.com/awslabs/agentic-ai-platform`, directory `workshop-agentic-ai-platform-agentcore`) is a placeholder until the workshop is published. If you received this content as a repository or archive, use that copy: `cd` into its root (the directory containing `deploy-cfn.sh` and `contentspec.yaml`) and continue.]{type="info"}

## Deploy the Platform

From the repository root, run the self-paced deployer:

:::code{showCopyAction=true showLineNumbers=false language=bash}
./scripts/self-service-deploy.sh
:::

This script:

1. Confirms your AWS credentials, required tools (`aws`, `yq`), and resolves your configured region (warning you if it is outside the supported-region allow-list).
2. Deploys all workshop CloudFormation stacks in dependency order — LLM Gateway, MCP Gateway & Registry, Tools Gateway, AgentCore, and the Code Editor IDE.
3. Prints the IDE **URL** and **IdePassword** when it finishes.

::alert[The full deployment takes approximately **30–45 minutes** (the Registry stack alone is 20–30 minutes). Keep the terminal open — the script runs synchronously and reports each stack as it completes. If a stack fails, the script prints the failure reason; run `./deploy-cfn.sh cleanup` to remove broken stacks before retrying.]{type="info"}

When the script finishes it prints a table like this — keep these values handy:

```
-------------------------------------------------------------
|                      DescribeStacks                       |
+--------------+--------------------------------------------+
|  URL         |  https://dxxxxxxxxxxxxx.cloudfront.net     |
|  IdePassword |  <randomly-generated-password>             |
+--------------+--------------------------------------------+
```

::alert[If you ever lose these values, retrieve them again with:
`aws cloudformation describe-stacks --stack-name code-editor --region "$(aws configure get region)" --query "Stacks[0].Outputs[?OutputKey=='URL' || OutputKey=='IdePassword']" --output table`]{type="info"}

## Open the Workshop IDE

Your workshop environment includes a browser-based VS Code IDE with all required tools pre-installed (AWS CLI, Python, Git, Docker, Node.js, the AWS CDK, and Strands Agents). **All workshop commands should be run in this IDE's terminal.**

Open the **`URL`** value printed by the deploy script (the `code-editor` stack's `URL` output) in a new browser tab.

::alert[Once the IDE is open in a new tab, return to this page to continue — leave the IDE tab open so you can switch to it when a module asks you to run commands.]{type="info"}

### Sign in to the IDE

The first time you open the IDE URL you will see a password prompt from code-server. This gates the IDE so only you — not anyone who happens to stumble on the CloudFront URL — can reach it.

Paste the **`IdePassword`** value (the `code-editor` stack's `IdePassword` output) into the login form:

| Output key | What to do with it |
|---|---|
| `URL` | Click to open the IDE (you did this already) |
| `IdePassword` | Copy and paste into the code-server "Welcome to code-server" password field |

::alert[The password is **randomly generated** on every fresh deploy and is not shared across deployments. code-server remembers your session via a cookie, so you won't be prompted again unless you close the browser.]{type="info"}

You should see the VS Code IDE with the `/workshop/` folder pre-populated with the workshop source code:

![VS Code IDE with workshop source pre-staged at /workshop/](/static/img/getting-started/code-editor-landing.png)

::alert[The IDE runs on an EC2 instance in your account with pre-configured AWS credentials. You do not need to paste CLI credentials into the IDE terminal — they are available automatically via the instance role. The default AWS region is also pre-set to the region you deployed into.]{type="info"}

## Verify the Environment

Before starting the modules, confirm everything is healthy. From the repository root (in your local terminal, where you ran the deploy), run the self-test:

:::code{showCopyAction=true showLineNumbers=false language=bash}
./scripts/self-test.sh -r "$(aws configure get region)"
:::

This checks that all five stacks are `*_COMPLETE` and that the LLM Gateway, MCP Registry, AgentCore Gateway, and Bedrock are reachable. It should report `0 failed`.

::alert[A transient failure immediately after a stack reaches `CREATE_COMPLETE` is possible — public endpoints (CloudFront, load balancers) can take a few minutes to start serving traffic. If a health check fails, wait 2–3 minutes and re-run the self-test.]{type="info"}

## Where to Run CLI Commands

::alert[**Every `aws` / `bash` / `python` command in every module is intended for the Workshop IDE's terminal.** Open a terminal in the IDE with **Terminal → New Terminal** (`` Ctrl+` ``). The terminal already runs in the `/workshop/` folder with pre-configured AWS credentials and the correct default region (the region you deployed into). The only commands you run from your **local** terminal are the deploy (`./scripts/self-service-deploy.sh`), the self-test (`./scripts/self-test.sh`), and cleanup (`./deploy-cfn.sh destroy`).]{type="warning"}

## Cleanup

When you are finished, tear everything down to stop charges. From the repository root in your local terminal:

:::code{showCopyAction=true showLineNumbers=false language=bash}
./deploy-cfn.sh destroy
:::

This removes all five stacks in reverse dependency order (including the GuardDuty VPC-endpoint pre-cleanup that otherwise blocks VPC deletion). If you completed **Module 4**, tear down its FAST CDK resources **first** — see the [Cleanup](../../cleanup/) page for the complete teardown order and a verification checklist.

## What's Next

Proceed to **Module 1: The Vision** to understand the platform architecture and choose your track.
