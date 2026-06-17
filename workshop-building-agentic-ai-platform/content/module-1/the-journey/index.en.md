---
title: "Your Workshop Journey"
weight: 24
---

Now that you understand the platform vision, it's time to choose your path.

## Choose Your Track

::::tabs
:::tab{label="Track 1: Build an Agent"}

### Best for: AI/ML Engineers, Developers

You want to build and deploy an agent — not set up infrastructure. The platform foundations are pre-deployed for you.

**What you will do:**

1. Jump to Module 4 — build a Travel Agent using [FAST](https://github.com/awslabs/fullstack-solution-template-for-agentcore) (Fullstack AgentCore Solution Template) — an open-source starter kit for full-stack agent apps on AgentCore
2. Connect it to the pre-deployed LLM Gateway and Tools Gateway
3. Plan trips through the React frontend

**Start at:** [Module 4 (Build Your Agent)](/module-4)

**Time:** ~1.5–2 hours

:::
:::tab{label="Track 2: Build the Platform"}

### Best for: Platform Engineers, Infrastructure Teams

You want to understand and deploy the foundational platform components that AI/ML teams depend on.

**What you will do:**

1. Deploy the LLM Gateway for governed model access (Module 2)
2. Set up the tool registry and gateway (Module 3 — choose OSS or AWS-native path)
3. Configure guardrails and access control

**Start at:** Module 2 (LLM Gateway)


**Time:** ~2–3 hours

:::
:::tab{label="Track 3: Full Journey"}

### Best for: Solutions Architects, Tech Leads

Build the platform foundations and then build agents on top. End-to-end understanding of both sides.

**What you will do:**

1. Deploy all platform foundation components (Modules 2 and 3)
2. Build and deploy a Travel Agent that leverages the platform (Module 4)
3. See the full lifecycle from infrastructure to agent in production

**Start at:** Module 2 (LLM Gateway)

**Time:** ~3–4 hours

:::
::::

::alert[**Track 1 (Fast Path):** The platform infrastructure is pre-deployed for you. Skip Modules 2 and 3 and [jump directly to Module 4](/module-4) to build and deploy your agent.]{header="Quick Start" type="info"}

## Module Roadmap

| Module | What It Covers | Required For |
|--------|---------------|-------------|
| **Module 1: The Vision** (this module) | Platform vision, architecture, core components | All tracks |
| **Module 2: LLM Gateway** | Governed model access, guardrails, cost attribution | Track 2, Track 3 |
| **Module 3a: OSS MCP Registry + Tools Gateway** | Open-source tool registries, governed tool access | Track 2, Track 3 (OSS path) |
| **Module 3b: AgentCore Registry & Gateway** | AWS-native registry, gateway, identity | Track 2, Track 3 (AWS-native path) |
| **Module 4: Build Your Agent** | Agent development using FAST, connected to the platform | Track 1, Track 3 |

::alert[Within Modules 3 and 4, you choose between the **OSS path** (MCP Gateway & Registry) and the **AWS-native path** (AgentCore Registry & Gateway). Both provide the same travel tools — the difference is the infrastructure you manage.]{type="info"}

## Ready?

Pick your track and let's go. If you are unsure, we recommend Track 3 for the full experience.
