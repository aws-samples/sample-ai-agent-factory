---
title: "Architecture Overview"
weight: 31
---

Before deploying, understand what you're building and the AWS resources involved.

## Why an LLM Gateway?

In an enterprise agentic AI platform, multiple agents and applications need access to foundation models. Without a gateway:

- **Credential sprawl** — Every team manages their own API keys for each provider
- **No cost visibility** — Finance can't attribute model costs to teams or projects
- **No guardrails** — No central point to enforce content safety policies
- **Provider lock-in** — Switching models requires changes in every application

The LLM Gateway solves these by providing a **single, governed endpoint** that all agents and applications call.

![LLM Gateway Architecture — LiteLLM Proxy on ECS Fargate with API Gateway, virtual keys, and Bedrock model access](/static/img/module-2/llm-gateway-architecture.png)

::alert[This module implements the [Guidance for Multi-Provider Generative AI Gateway on AWS](https://aws.amazon.com/solutions/guidance/multi-provider-generative-ai-gateway-on-aws/) — an AWS Solutions pattern for unified LLM access with cost tracking and governance.]{type="info"}

## LiteLLM Proxy + Strands Agents

A key reason we chose LiteLLM is its native integration with [Strands Agents](https://strandsagents.com/) — an open-source SDK for building AI agents on AWS — via `LiteLLMModel`:

:::code{showCopyAction=true showLineNumbers=true language=python}
from strands import Agent
from strands.models.litellm import LiteLLMModel

# Agent routes all LLM calls through the platform gateway
model = LiteLLMModel(
    model_id="claude-sonnet",
    api_base="https://<api-gateway-id>.execute-api.<region>.amazonaws.com",
    api_key="<virtual-key>",
)

agent = Agent(model=model, tools=[...])
result = agent("Analyse this data and create a report.")
:::

This means:
- **Tool calling works** — LiteLLM speaks the Bedrock Converse API natively, so Strands tool calls flow through without translation
- **Cost is tracked** — Every LLM call from every agent is attributed to its virtual key
- **Guardrails are enforced** — Bedrock Guardrails apply centrally, invisible to agent code
- **Models are swappable** — Change `model_id` to route to any of 65+ Bedrock models

## AWS Resources

The CloudFormation template creates:

| Resource | Service | Purpose |
|----------|---------|---------|
| VPC + 4 subnets | EC2 | Isolated network (2 public, 2 private) |
| NAT Gateway | EC2 | Outbound internet for private subnets |
| Internet Gateway | EC2 | Public subnet internet access |
| HTTP API + VPC Link | API Gateway | HTTPS front door, routes to internal ALB |
| Internal ALB | ELB | Routes traffic to LiteLLM on port 4000 (private) |
| ECS Cluster + Service | ECS Fargate | Runs LiteLLM + PostgreSQL sidecar |
| EFS File System | EFS | Persistent storage for PostgreSQL data |
| Task Role | IAM | `bedrock:InvokeModel` + Guardrails permissions |
| Execution Role | IAM | Pull images, write logs, read secrets |
| Admin Key Secret | Secrets Manager | Auto-generated LiteLLM admin key |
| PostgreSQL Password | Secrets Manager | Auto-generated database password |
| Log Group | CloudWatch | Container logs (7-day retention) |

## Two-Container Architecture

The ECS Fargate task runs **two containers** in the same task definition:

| Container | Image | Port | Purpose |
|-----------|-------|------|---------|
| `litellm` | `litellm-database:v1.83.3-stable` | 4000 | LLM proxy, virtual keys, spend tracking, Admin UI |
| `postgres` | `postgres:16.6` (Debian) | 5432 | Stores virtual keys, teams, spend logs |

PostgreSQL data is persisted on EFS, so it survives task restarts. The LiteLLM container waits for PostgreSQL to be healthy before starting.

## Network Flow

Requests flow through: **Client → API Gateway (HTTPS) → VPC Link → Internal ALB → ECS Fargate (LiteLLM + PostgreSQL) → Amazon Bedrock**. All internal traffic stays within the VPC. The API Gateway provides the public HTTPS endpoint.

## Available Models

The gateway is pre-configured with Bedrock models from multiple providers including Anthropic Claude, Amazon Nova, Meta Llama, Mistral AI, Cohere, and others. You will explore the full model list in the next step.

---

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-2-llm-gateway/notebooks/` and open the corresponding notebook.

Open **`step-1-architecture.ipynb`**. This notebook provides an interactive overview of the LLM Gateway architecture, including diagrams and explanations of the AWS resources involved.


