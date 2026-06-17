---
title: "Architecture and Prerequisites"
weight: 71
---

Before deploying, understand what FAST builds and verify your environment is ready.

## FAST Architecture

FAST (Fullstack AgentCore Solution Template) deploys a complete agent application stack using AWS CDK:

| Component | Service | Purpose |
|-----------|---------|---------|
| Frontend | AWS Amplify Hosting | React chat UI with Cognito login |
| Authentication | Amazon Cognito | User pool for frontend login + M2M client for gateway auth |
| Agent Runtime | AgentCore Runtime | Runs the Strands agent in a managed container |
| Model Access | Amazon Bedrock | Foundation model invocation (routed through the LLM Gateway) |
| Tool Execution | AgentCore Gateway | MCP-based tool dispatch to Lambda targets |
| Conversation Memory | AgentCore Memory | Short-term conversation history across turns |
| Code Interpreter | AgentCore Code Interpreter | Secure Python sandbox for calculations |

## Authentication Flows

1. **User → Frontend** — Cognito Authorization Code grant (browser login)
2. **Frontend → Runtime** — User's JWT passed in the Authorization header
3. **Runtime → Gateway** — OAuth2 Client Credentials (M2M) via AgentCore Identity Token Vault

## Patterns

FAST ships with multiple agent patterns (Strands, LangGraph, Claude Agent SDK). You will create a `strands-travel-agent` pattern — a travel planning agent that uses the platform's LLM Gateway and Tools Gateway.

## Prime Anthropic Model Access

::alert[**Track 1 participants — this step is required.** If you skipped Modules 2 and 3, the Anthropic Marketplace subscription has not been activated yet. The FAST agent uses Claude Sonnet and will fail with `AccessDeniedException` until you complete this one-time step. If you completed Module 2, you have already done this and can skip ahead.]{type="warning"}

Before the agent can invoke any Claude model, Anthropic requires a one-time use case submission for this AWS account. Open the Bedrock console and prime **both** Claude Sonnet 4.5 and 4.6 — the baseline agent uses 4.5, and the workshop-modified agent uses 4.6 via the LLM Gateway.

::alert[Confirm the Bedrock console **region selector** (top-right) matches the region you deployed into — model access is granted per region.]{type="info"}

:button[Open Bedrock Console]{target="_blank" href="https://console.aws.amazon.com/bedrock/home" variant="primary" iconName="external" iconAlign="right"}

::::expand{header="First-time setup — step by step"}
1. In the Bedrock console left navigation, open **Model catalog**.
2. Find **Claude Sonnet 4.5** (use the search/filter if needed) and click the model card.
3. Click **Open in playground** in the top-right of the model page.
4. Type `hi` in the prompt box and click **Run**.
5. If the **Submit use case details for Anthropic** dialog appears, fill the form (use `Workshop testing` for the use case) and click **Submit**.
6. Wait for the model to return a response.
7. Go back to **Model catalog** and repeat steps 2–6 for **Claude Sonnet 4.6**.
8. You can now close the playground — the account is primed for both models.
::::

## Verify Prerequisites

Confirm your region is set (the Workshop IDE pre-sets it to your deploy region) and that the tools are available:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws configure get region   # should print your deploy region
node --version && cdk --version && python3 --version && docker --version
:::

::alert[If deploying from your local machine instead of the Workshop IDE, verify you have Node.js 18+, Python 3.12+, CDK v2, and Docker (or Finch with `export CDK_DOCKER=finch`).]{type="info"}

## Verify Platform Foundations

::alert[If using the Workshop IDE, credentials are pre-configured via the instance role.]{type="warning"}

Your platform team deployed the tool registry and gateway in Module 3a (MCP path) or Module 3b (AgentCore path). There are two paths — choose the tab that matches your track:

- **MCP Path (3a):** Uses the open-source MCP Gateway & Registry — tools are registered in a self-managed catalog
- **AgentCore Path (3b):** Uses the AWS-native AgentCore Registry & Gateway — fully managed, no infrastructure to maintain

Both paths provide the same travel tools (flights, hotels, knowledge base) through an AgentCore Gateway. Your agent code is identical regardless of which path was used.

::::tabs
:::tab{label="MCP Path (from Module 3a)"}

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

echo "=== LLM Gateway ==="
aws cloudformation describe-stacks --stack-name workshop-llm-gateway-stack \
  --query "Stacks[0].StackStatus" --output text --region $REGION

echo "=== Registry ==="
aws cloudformation describe-stacks --stack-name workshop-registry-stack \
  --query "Stacks[0].StackStatus" --output text --region $REGION

echo "=== Tools Gateway ==="
aws cloudformation describe-stacks --stack-name workshop-tools-gateway-stack \
  --query "Stacks[0].StackStatus" --output text --region $REGION
:::

All three should show `CREATE_COMPLETE`.

:::
:::tab{label="AgentCore Path (from Module 3b)"}

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

echo "=== LLM Gateway ==="
aws cloudformation describe-stacks --stack-name workshop-llm-gateway-stack \
  --query "Stacks[0].StackStatus" --output text --region $REGION

echo "=== AgentCore ==="
aws cloudformation describe-stacks --stack-name workshop-agentcore-stack \
  --query "Stacks[0].StackStatus" --output text --region $REGION
:::

Both should show `CREATE_COMPLETE`.

:::
::::

Verify the gateway has travel tools ready:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
  --query "items[0].gatewayId" \
  --output text --region $REGION)

aws bedrock-agentcore-control list-gateway-targets \
  --gateway-identifier $GATEWAY_ID \
  --query "items[].name" \
  --output text --region $REGION
:::

You should see targets including flights, hotels, and knowledge base tools.

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4b-fast/notebooks/` and open **`01-architecture-prereqs.ipynb`**.
