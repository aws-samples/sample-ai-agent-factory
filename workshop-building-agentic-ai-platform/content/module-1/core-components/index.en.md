---
title: "Core Components"
weight: 23
---

Now let's look at the key components you will build in this workshop.

## Amazon Bedrock AgentCore

[Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/) is the managed foundation this platform builds on:

| Service | What It Does |
|---------|-------------|
| **AgentCore Runtime** | Sandboxed, low-latency serverless environments with session isolation |
| **AgentCore Gateway** | Secure connectivity to tools via MCP, Lambda functions, and API endpoints |
| **AgentCore Memory** | Short-term and long-term memory for stateful agent interactions |
| **AgentCore Identity** | Authentication and authorization for agent-to-tool and agent-to-agent interactions |
| **AgentCore Observability** | Monitoring, tracing, latency tracking, and cost attribution |

## The Gateway Pattern

Rather than letting every agent directly access models, tools, and other agents, all interactions flow through governed gateways:

| Gateway | What It Governs | Workshop Module |
|---------|----------------|-----------------|
| **LLM Gateway** | Model access — unified API, guardrails, cost attribution, rate limiting | Module 2 |
| **Tool Gateway (Open-Source)** | Tool access via MCP Registry — discovery, access control, audit logging, Bedrock Guardrails | Module 3a |
| **Tool Gateway (AWS-Native)** | Tool access via AgentCore Registry & Gateway — AgentCore Policy Engine (Cedar-based), Lambda targets | Module 3b |
| **Agent Gateway** | Agent-to-agent — A2A discovery, secure routing, entitlements | Modules 3a/3b (registry) |

## Registry vs. Gateway: Discovery and Invocation

The Registry and the Gateway answer two different questions, and it helps to keep them straight:

- **Agent Registry** (or **AgentCore Registry**) is a discovery and metadata catalog. It answers *"what tools exist and who is allowed to use them?"* — names, descriptions, schemas, ownership, and access policies.
- **AgentCore Gateway** is the execution and routing service. It answers *"what happens when a tool is actually called?"* — authenticating the caller, throttling requests, recording audit logs, and routing the call to the right target.

The recommended workflow combines both:

1. An agent queries the **Registry** to discover which tools exist and which it is entitled to use.
2. The agent then invokes those tools through the **Gateway**, which enforces authentication, throttling, audit logging, and routing.

::alert[The Registry and the Gateway are independent services. Discovery (Registry) and invocation (Gateway) are decoupled, so you can evolve your tool catalog and your runtime enforcement separately.]{header="Two independent services" type="info"}

## How a Request Flows

1. A **client** (app, IDE, playground) sends a request
2. The **Gateway** authenticates the caller and applies governance policies
3. The gateway routes to the **Agent** running in AgentCore Runtime
4. The agent reasons and decides which **tools** to use
5. Tool calls flow through the **Tool Gateway**, which enforces access control
6. **Observability** captures traces, latency, cost, and quality metrics at every step

This is the architecture you will build in the hands-on modules.
