---
title: "Module 5: Observe & evaluate with CloudWatch Omni"
weight: 80
---

**All tracks** (Part A for Tracks 2 and 3, Part B for Tracks 1 and 3)

::alert[**Where to run commands.** All `aws` / `bash` / `python` commands in this module should be run in the **Workshop IDE's terminal**, not your laptop terminal. **When you open a notebook**, select the **`Python 3 (workshop)`** kernel when VS Code prompts in the top-right; `ModuleNotFoundError` usually means the wrong kernel is selected.]{type="info"}

By the end of this module you will have:

- Verified the Amazon CloudWatch Omni domain and space Workshop Studio pre-provisioned for the account, and signed in to the space as both of your IAM identities
- Queried the platform's telemetry (LLM Gateway, interceptor audit logs, AgentCore Gateway spans) across logs and traces in one place
- Read a complete agent trace: model calls through the LLM Gateway, tool calls through the AgentCore Gateway, Memory and Code Interpreter spans
- Scored agent sessions with built-in evaluators, on demand and continuously, and read the scores back as logs and metrics
- Reproduced a quality regression that error rates cannot show and caught it with an evaluator

Modules 2 to 4 gave every layer of the platform its own observability surface: LiteLLM spend logs, interceptor audit logs, the GenAI Observability dashboard, Memory events. Each one answers a question about one component. None of them answers the two questions a platform team gets asked first: "what did this agent actually do, end to end, for this user?" and "is it getting better or worse?"

CloudWatch Omni is the layer that answers both. It reads the telemetry the platform already emits (no re-instrumentation), correlates logs, metrics and traces in one CloudWatch Dataset, and scores agent runs with evaluators served by Amazon Bedrock AgentCore Evaluations. In this module you take on the role of the **platform operator**: you stand up the space, confirm the platform's signals arrive, then hand the agent team a quality score they can alarm on.

## How Omni is organized

Three concepts decide where telemetry lives and who can see it:

| Concept | What it is | In this workshop |
|---------|-----------|------------------|
| Domain | The identity and sign-in boundary. One per account, reachable at `https://<domain-name>.cloudwatch-omni.global.app.aws` (the name you set, e.g. `ws-platform-<account-id>`) | Pre-provisioned by Workshop Studio (in `workshop-agentcore-stack`): one account-level domain, IAM sign-in only |
| Space | Where you work. One per account and Region, inside the domain | One space in the workshop Region |
| CloudWatch Dataset | Makes logs, metrics and traces queryable and correlated. Created when the space is created | Receives every log group in the account plus `aws/spans` |

The workshop account is a single account, so you use the single-account setup. In an enterprise with many accounts, the organization management account creates one domain, each member account creates its own space, and CloudWatch centralization rules copy telemetry into a hub account. The [enterprise agentic AI platform blueprint](https://github.com/aws-samples/sample-ai-agent-factory/tree/main/enterprise-agentic-ai-platform-blueprint) implements that hub model.

## What is already in place

Workshop Studio provisioned three things for this module when it deployed `workshop-agentcore-stack`, so you do not wait for them:

| Resource | Why it matters |
|----------|----------------|
| CloudWatch Transaction Search, 100 % indexing | The one-time prerequisite for any AgentCore span (Runtime, Gateway, Memory) to reach CloudWatch. It takes about 10 minutes to take effect after it is enabled, so it was enabled during provisioning |
| `CloudWatchOmniSpaceAccessRole-workshop` and `CloudWatchOmniDatasetIntegrationRole-workshop` | The roles Omni and CloudWatch Logs assume for your space and its Dataset. You pass them in; you never create roles in this module |
| `AgentCoreEvaluationRole-workshop` | The execution role AgentCore Evaluations uses to read spans and write scores |

## Prerequisites

Part A (steps 1 to 3) needs Module 3b completed, so the AgentCore Gateway has served tool calls. Part B (steps 4 to 6) needs Module 4 completed with the agent deployed and the five tests on the [Run the agent](/module-4/run-the-agent) page executed at least once, so there are agent traces to look at.

Confirm the IDE tooling is new enough before you start. The AWS CLI gained the `cloudwatchomni` command in 2.39 and the notebook kernel needs boto3 1.43.100 or later:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws --version
aws cloudwatchomni list-domains --region $(aws configure get region) --query 'items[].name' --output text
python3 -c "import boto3; boto3.client('cloudwatchomni'); print('boto3', boto3.__version__, 'OK')"
:::

The first line should report `aws-cli/2.39` or newer, the second prints the pre-provisioned `ws-platform-<account-id>` domain (in a self-paced account that has not created one yet it prints nothing, without an error), and the third prints the boto3 version. If the second command says `Invalid choice: 'cloudwatchomni'`, the IDE's CLI is older than this module expects; re-run the IDE provisioning or ask a facilitator.

::alert[Track 1 (fast path): the platform modules were deployed for you, so start at step 1, then skip step 3 and continue with step 4. Track 2 (platform): do steps 1 to 3, then step 7. Track 3: everything.]{type="info"}

## Steps

1. [Verify your domain and space](step-1/): Confirm the pre-provisioned domain, space, Dataset integration and access grants (in your own account the self-service deploy script's stacks created them)
2. [Sign in to your space](step-2/): Open the space from the CloudWatch console, or with a one-time deep link from the terminal
3. [Platform signals](step-3/): LLM Gateway logs, interceptor audit logs and Gateway spans in one query (Tracks 2 and 3)
4. [Agent traces and sessions](step-4/): Read a FAST agent run end to end (Tracks 1 and 3)
5. [Evaluate quality](step-5/): Score sessions on demand, then continuously with an online evaluation (Tracks 1 and 3)
6. [Catch a regression](step-6/): An off-topic answer that returns HTTP 200, caught by an evaluator (optional, Track 3)
7. [Cleanup](step-7/): Delete the evaluation configuration, the space and the domain

## Source code

:::code{showCopyAction=false showLineNumbers=false language=text}
source/module-5-omni/
└── notebooks/
    ├── 01-architecture.ipynb         # Concepts and prerequisite checks
    ├── 02-create-space.ipynb         # Domain, space, Dataset integration
    ├── 03-sign-in.ipynb              # One-time deep link
    ├── 04-platform-signals.ipynb     # Telemetry SQL across platform log groups
    ├── 05-agent-traces.ipynb         # Agent traces, sessions, token usage
    ├── 06-evaluate.ipynb             # On-demand Evaluate + online evaluation config
    ├── 07-regression.ipynb           # Off-topic regression scored with Builtin.Refusal, system-prompt fix
    └── 08-cleanup.ipynb              # Cleanup

static/cfn/agentcore/
└── workshop-agentcore-stack.yaml     # Transaction Search + the three roles (pre-deployed)
:::
