---
title: "Module 4: Build Your Agent"
weight: 70
---

**Tracks 1 and 3**

::alert[**Where to run commands.** All `aws` / `bash` / `python` commands in this module should be run in the **Workshop IDE's terminal** — not your laptop terminal. **When you open a notebook**, select the **`Python 3 (workshop)`** kernel when VS Code prompts in the top-right; `ModuleNotFoundError` usually means the wrong kernel is selected.]{type="info"}

In this module you take on the role of an **AI/ML engineer** who has been given access to the foundational services built in Modules 2 and 3. Your job is to build and deploy a travel planning agent that consumes the governed model access, registered tools, and gateway infrastructure the infrastructure team set up.

You will use [FAST](https://github.com/awslabs/fullstack-solution-template-for-agentcore) (Fullstack AgentCore Solution Template) — an open-source starter template that handles the undifferentiated heavy lifting of infrastructure setup — so you can focus on agent behavior rather than plumbing.

::alert[**Track 1 (fast path):** Foundational services are pre-deployed — start here directly. **Track 3 (full journey):** You arrive here after completing Modules 2 and 3.]{type="info"}

## The Scenario

Your team needs to build a **Travel Planning Agent** that can search flights, search hotels, and recommend itineraries — all through the governed services. You will deploy it as a full-stack application with a React frontend, AgentCore Runtime backend, and conversation memory.

The infrastructure team has deployed:

- An **LLM Gateway** (Module 2) with virtual keys, budgets, and guardrails
- A **Tool Registry** (Module 3a or 3b) with Flights MCP and Hotels MCP servers registered
- A **Tools Gateway** (Module 3a or 3b) with travel tools and Cognito JWT auth

![FAST Architecture — AgentCore Runtime with Amplify frontend, Cognito auth, Gateway tools, and Memory](/static/img/module-4/fast-architecture.png)

## What You Will Do

| Step | What | Why |
|------|------|-----|
| **Architecture** | Understand FAST and verify prerequisites | Know what you're deploying |
| **Deploy** | Clone FAST, create a travel agent pattern, deploy with CDK | Get a working baseline |
| **Connect to LLM Gateway** | Route model calls through Module 2 | Governed model access with cost tracking |
| **Connect to Tools Gateway** | Wire travel tools from your chosen path (3a or 3b) | Flights + Hotels via AgentCore Gateway |
| **Run the Agent** | Plan trips through the React frontend | End-to-end validation |
| **Observe** | Inspect traces, logs, and memory | Service observability |
| **Register Agent (Optional)** | Register the agent in the AgentCore Registry | Agent discoverability |
| **Cleanup** | Tear down Module 4 resources | Clean up |

::alert[**Which tool path do I connect to?** Module 4 always deploys the **same** FAST agent — the only fork is the "Connect to Tools Gateway" step, where you wire in the tools you registered earlier. Choose the path that matches the module you did: **Module 3a (open-source MCP Registry + Tools Gateway)** → follow *Connect to Tools Gateway (MCP path)*; **Module 3b (AWS-native AgentCore Registry & Gateway)** → follow *Connect to Tools Gateway (AgentCore path)*. On **Track 1 (fast path)** both are pre-deployed, so pick either — the AgentCore path is the recommended default. You can also do both to compare.]{header="MCP path (3a) vs AgentCore path (3b)" type="info"}

## Prerequisites

- Completed Module 1 (The Vision)
- **Track 1 (fast path):** Foundational services are pre-deployed — no prior modules required
- **Track 3 (full journey — MCP path):** Completed Modules 2 and 3a
- **Track 3 (full journey — AgentCore path):** Completed Modules 2 and 3b

Before starting, confirm the Workshop IDE has the expected tooling installed. Run these in the IDE terminal:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws sts get-caller-identity --query Arn --output text
node --version
npx cdk --version
docker --version || finch --version
python3 --version
:::

You should see your participant role ARN, Node `v20.x.x`, CDK `2.x.y`, Docker or Finch `25.x.x+`, and Python `3.13.x`. If any command errors out, re-open the Workshop IDE terminal or confirm the code-editor stack shows `CREATE_COMPLETE`.
