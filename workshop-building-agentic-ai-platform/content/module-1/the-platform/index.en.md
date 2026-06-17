---
title: "The Agentic AI Platform"
weight: 22
---

## What Is It?

![Platform architecture overview: a multi-account AWS landing zone. An AWS Organization holds the governance accounts (Management with Organizations, Control Tower, and IAM Identity Center; Log Archive with org CloudTrail, WORM S3, and Bedrock invocation logs; Audit with Security Hub, GuardDuty, Config, Inspector, and CloudWatch OAM). A central Platform account hosts the Auth Boundary (API Gateway, WAF, Cognito), Bedrock AgentCore (Gateway, Registry, Cedar), the Inference Gateway (LiteLLM on ECS Fargate), and Security and Cost controls (Guardrails, CUR, KMS). Per-application Workload accounts run AgentCore services, agent blueprints, RAG and knowledge stores, and PrivateLink egress. A CI/CD pipeline on the right runs from source through build and test, non-prod deploy, an evaluation gate, a canary stage, production, and a teardown test.](/static/img/module-1/agentic-ai-platform-architecture.png)

An **enterprise-style landing zone pattern** for agentic AI workloads — like an [AWS Landing Zone](https://docs.aws.amazon.com/prescriptive-guidance/latest/migration-aws-environment/building-landing-zones.html) for cloud adoption, but purpose-built for AI agents.

It complements your organization's [Cloud Center of Excellence (CCoE)](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-center-of-excellence/introduction.html) — the team that leads cloud adoption, migration, and operation — by extending their governance framework to cover agentic AI workloads with the same rigour applied to traditional cloud infrastructure.

It is not a single product. It is a **well-architected foundation** composed of three layers:

| Layer | Purpose |
|-------|---------|
| **Shared Services** | Centralized capabilities — model access, tool registries, agent registries, observability, security |
| **Blueprints** | Standardized, pre-approved patterns for rapid agent deployment |
| **Governance** | Controls that enable both compliance and agility — guardrails, cost attribution, access policies, audit trails |

As the diagram above shows, these layers are realized as a **multi-account AWS landing zone**: dedicated *governance accounts* (Management, Log Archive, Audit) enforce org-wide controls, a central *Platform account* hosts the shared services (auth boundary, Bedrock AgentCore, inference gateway), and per-application *Workload accounts* run agents from standardized blueprints — all promoted through a single governed CI/CD pipeline.

## Why a Platform Approach?

| Without Platform | With Platform |
|-----------------|---------------|
| Each team builds their own model access layer | Shared LLM Gateway with cost attribution and guardrails |
| Tools are hardcoded into individual agents | Centralized Tool Registry — via open-source MCP Registry or AWS-native AgentCore Registry |
| No visibility into agent behavior across teams | Unified observability with tracing and evaluation |
| Security is an afterthought | Identity, access control, and audit built into the foundation |

## Built on AWS and Open Source

| Component | Technology |
|-----------|-----------|
| **Model Access & Guardrails** | Amazon Bedrock |
| **Agent Runtime, Memory, Gateway** | Amazon Bedrock AgentCore |
| **Agent Framework** | Strands Agents (open source) |
| **Tool Connectivity** | Model Context Protocol — MCP (open standard) |
| **Agent-to-Agent Communication** | A2A Protocol (open standard) |
| **LLM Gateway** | Open-source LLM Gateway pattern |

::alert[The platform is framework-agnostic. While this workshop uses Strands Agents, the same patterns work with CrewAI, LangGraph, LlamaIndex, OpenAI Agents SDK, and other frameworks supported by AgentCore.]{type="info"}
