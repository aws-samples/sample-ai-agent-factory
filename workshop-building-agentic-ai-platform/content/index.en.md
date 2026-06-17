---
title: "Building an Enterprise Agentic AI Platform on Amazon Bedrock AgentCore"
weight: 0
---

Welcome to the Building an Enterprise Agentic AI Platform Workshop!

Enterprises are moving beyond simple chatbots and proof-of-concept AI agents, but the jump to production requires more than better models — it requires a **foundation**. A solution that provides governed model access, tool and agent registries, security controls, and observability across the entire agent fleet.

Amazon Bedrock AgentCore provides the managed runtime, memory, gateway, identity, and observability you would otherwise have to build yourself to operate agents at scale. But AgentCore alone isn't a platform. Combined with an **LLM Gateway** for governed, cost-attributed model access, an **MCP Gateway & Registry** for cataloging and discovering the tools, agents, and skills available to your fleet, and **Strands Agents** for writing the agents themselves, these components compose into a formidable foundation for enterprise agentic AI.

This workshop teaches you how to assemble these building blocks, govern them, and deploy real agents on top.

## What will I build?

This workshop follows two complementary personas — the **platform engineer** who provides governed LLM and tool infrastructure (Modules 2, 3a, 3b), and the **AI/ML engineer** who builds agents on top of it (Module 4). You pick the **track** that matches your role and available time:

- **Module 1: The Vision** — why enterprises need a platform approach to agentic AI, not just individual agents (all tracks)
- **Module 2: LLM Gateway** — deploy LiteLLM Proxy on ECS Fargate for governed, cost-attributed access to Amazon Bedrock models
- **Module 3a: MCP Registry + Tools Gateway** — register tools in the MCP Gateway & Registry, then layer an AgentCore Tools Gateway on top for JWT auth, audit, and guardrails
- **Module 3b: AgentCore Registry & Gateway** — AWS-native tool governance using Amazon Bedrock AgentCore with Cedar-based authorization and EventBridge-driven approval workflows
- **Module 4: Build Your Agent** — deploy a full-stack travel agent using FAST (Fullstack AgentCore Solution Template) on Amazon Bedrock AgentCore, wired to the platform via either the MCP path or the AgentCore path

## Choose Your Track

| Track | Best For | You Do | Duration |
|-------|----------|--------|----------|
| **Track 1: Fast Path** | AI/ML Engineers who want to build an agent | Jump straight to Module 4 — platform is pre-deployed | ~1.5–2 hrs |
| **Track 2: Build the Platform** | Platform Engineers | Modules 1 → 2 → 3a → 3b (stops before agent) | ~2–3 hrs |
| **Track 3: Full Journey** | Solutions Architects, Tech Leads | Modules 1 → 2 → 3a → 3b → 4 end-to-end | ~3–4 hrs |

All tracks share **Module 1** (the vision). Module 1 concludes with a track selector so you can pick the right path for your goals.

## Target audience

This 300 level workshop is designed for Platform Engineers, AI/ML Engineers, and Solutions Architects who want hands-on experience building the foundational infrastructure for enterprise agentic AI workloads. It is ideal for those looking to move AI agents from proof-of-concept to production with proper governance, security, and observability.

**Estimated Duration:** 1.5-4 hours depending on track chosen

### Prerequisites

This workshop is not an introduction to AWS or AI/ML concepts. Participants should have a foundational understanding of the following:

1. Basic familiarity with core AWS services (IAM, Lambda, CloudFormation, ECS Fargate, API Gateway, Cognito, CloudWatch)
2. Comfort with command-line interfaces and the AWS CLI
3. Familiarity with Amazon Bedrock and how LLMs use tools (function/tool calling)

Each module's opening page includes a short verification block to confirm the tools it expects are installed and working before you begin. For Module 4 specifically, see the [Module 4 prerequisites check](/module-4#prerequisites).

### AWS Account Requirements and Costs

**At an AWS Event:** An AWS account will be provided for you at no cost. You will not incur any charges.

**Self-Hosted:** If you are running this workshop in your own AWS account, you will incur charges for the AWS services used. Key cost-bearing resources include [Amazon ECS Fargate](https://aws.amazon.com/fargate/pricing/), [Amazon DocumentDB](https://aws.amazon.com/documentdb/pricing/), [Amazon Aurora PostgreSQL](https://aws.amazon.com/rds/aurora/pricing/), [NAT Gateways](https://aws.amazon.com/vpc/pricing/), [Application Load Balancers](https://aws.amazon.com/elasticloadbalancing/pricing/), [Amazon CloudFront](https://aws.amazon.com/cloudfront/pricing/), [AWS Lambda invocations](https://aws.amazon.com/lambda/pricing/), and [Amazon Bedrock model invocations](https://aws.amazon.com/bedrock/pricing/). Be sure to follow the [cleanup instructions](/cleanup) at the end of the workshop to minimize costs.
