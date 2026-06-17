---
title: "Module 3a: OSS MCP Registry + Tools Gateway"
weight: 40
---

**Track 2 and Track 3**

::alert[**Where to run commands.** All `aws` / `bash` / `python` commands in this module should be run in the **Workshop IDE's terminal** — not your laptop terminal. **When you open a notebook**, select the **`Python 3 (workshop)`** kernel when VS Code prompts in the top-right; `ModuleNotFoundError` usually means the wrong kernel is selected.]{type="info"}

Use the pre-deployed [MCP Gateway & Registry](https://github.com/agentic-community/mcp-gateway-registry) to register tools and agents, configure identity-aware access control, and verify discoverability — building the governance layer that agents in Module 4 will consume.

## The Problem

Module 2 solved model access — every agent talks to one LLM Gateway instead of managing its own Bedrock credentials. But models are only half the story. Agents also need **tools** (MCP servers) and **other agents** (A2A peers). Without governance, the same sprawl pattern emerges:

![MCP Sprawl vs Governed Access](/static/img/module-3/mcp-gateway-registry-infographic.png)

- Teams deploy MCP servers independently — no inventory of what exists, no way to discover new tools
- Agents are hardcoded to specific tool endpoints — add a new tool and every agent needs a code change
- No unified identity — each MCP server has its own auth scheme, and revoking access means touching every server
- No audit trail connecting a specific agent to a specific tool invocation
- Agent-to-agent collaboration requires hardcoded knowledge of endpoints and capabilities

This is the tool and agent sprawl problem. The MCP Gateway & Registry solves it.

## Two Layers of Governance

Both layers are taught in this module. You cover the discovery and access-control layer first (steps 1–6), then layer the Tools Gateway on top (the Tools Gateway section later in this module).

| Layer | Component | What It Does |
|-------|-----------|-------------|
| **Discovery & Access Control** | MCP Gateway & Registry | Central catalog of tools and agents. Semantic search for discovery. Group-based access control for who can use what. |
| **Runtime Enforcement** | AgentCore Tools Gateway | Sits in the request path. Adds Bedrock Guardrails content screening, request/response interceptors, and audit logging to every tool call. |

The Registry answers *"what tools exist and who is allowed to use them?"* The Tools Gateway answers *"what happens when a tool is actually called?"* Together they form the complete governance stack.

::alert[You can think of the Registry as the control plane (policy and discovery) and the Tools Gateway as the data plane (runtime enforcement). This mirrors how AWS services like API Gateway separate configuration from request handling.]{type="info"}

## What's Already Deployed

The workshop bootstrap deployed the MCP Gateway & Registry on Amazon ECS Fargate with:

- **Registry UI and API** — Gradio dashboard and FastAPI backend for registration and discovery
- **Auth Server** — OAuth2/OIDC proxy bridging Amazon Cognito with the Registry
- **MCP Gateway** — aggregates tools from registered MCP servers behind a single endpoint
- **Demo MCP servers** — CurrentTime and RealServerFakeTools for testing
- **Demo A2A agents** — Order Processing agent stub for testing A2A registration
- **Amazon Cognito** — user pool with group-based access control (shared with the Tools Gateway)
- **Amazon DocumentDB** — stores registrations, agent cards, and access control scopes
- **Observability** — Grafana dashboards backed by Amazon Managed Service for Prometheus

## The Scenario

![Travel Agent Architecture — Flights MCP, Hotels MCP, and Travel Agent connected through the Registry and AgentCore Gateway](/static/img/module-3/travel-agent-architecture.png)

You are a **platform engineer** who has received a request from an AI/ML team building a travel planning assistant. The team needs two tools and wants to register the agent itself:

1. A **Flights MCP server** — searches flights by route, date, and budget
2. A **Hotels MCP server** — searches hotels by city, dates, and price
3. A **Travel Agent card** — an A2A agent card describing the assistant's capabilities so other agents can discover and delegate to it

Your job is to register the MCP servers and the agent card in the platform registry, configure access control, create a machine-to-machine service account, and verify everything is discoverable. The output of this module feeds directly into the Tools Gateway section later in this module (where these tools are synced to the AgentCore Gateway) and Module 4 (where the developer team builds the actual Travel Agent).

## What You Will Do

| Step | What | Why |
|------|------|-----|
| **Workshop Architecture** | Understand the deployed infrastructure and how components connect | Know what you're working with |
| **Registry Overview** | Log in, explore pre-registered content, get an API token | Orient before making changes |
| **Register MCP Servers** | Register the Flights and Hotels MCP servers | Make the tools discoverable to agents |
| **Register an A2A Agent** | Register a Travel Agent card with skills metadata | Enable agent-to-agent discovery |
| **Create a Service Account** | Create an M2M identity with scoped access for the AgentCore agent | Give the agent a governed identity |
| **Verify and Hand Off** | Run end-to-end checks and share credentials with the developer team | Confirm readiness for the Tools Gateway and Module 4 |

::alert[This module is part of Track 2 (Build the Platform) and Track 3 (The Full Journey).]{type="info"}

## Prerequisites

- Completed Module 1 (The Vision)
- Access to the workshop AWS account
