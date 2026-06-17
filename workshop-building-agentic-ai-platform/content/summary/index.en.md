---
title: "Summary"
weight: 90
---

Congratulations on completing the workshop!

## What You Built

Depending on the track you chose, you deployed and explored different layers of the Agentic AI Platform:

| Track | What You Built |
|-------|---------------|
| **Track 1 (Fast Path)** | Built and deployed a Travel Planning Agent using FAST, connected to the pre-provisioned registries (OSS MCP and/or AgentCore) plus the Tools Gateway and LLM Gateway |
| **Track 2 (Platform)** | Deployed and configured the foundational infrastructure — LLM Gateway with virtual keys and guardrails, OSS MCP Registry with access control, Tools Gateway with travel tools, and/or the AgentCore Registry & Gateway |
| **Track 3 (Full Journey)** | Built the complete platform end-to-end — foundation components plus a Travel Agent (workshop sample) consuming them via FAST, across either the MCP path, the AgentCore path, or both |

## Architecture Recap

The platform you built follows the architecture from Module 1:

| Layer | What You Implemented |
|-------|---------------------|
| **Security** | Cognito identity (user + M2M), group-based access control, Bedrock Guardrails on model and tool outputs |
| **Gateway** | LLM Gateway (governed model access), AgentCore Gateway and/or Tools Gateway (governed tool access), MCP Registry and/or AgentCore Registry (tool and agent discovery) |
| **Runtime** | AgentCore Runtime running Strands Agents in managed containers via FAST |
| **Observability** | CloudWatch logs, GenAI Observability traces, gateway audit trail, spend tracking via LiteLLM, AgentCore Memory |

## Key Takeaways

- Enterprise agentic AI requires a **platform approach** — not just individual agents
- The **gateway pattern** (LLM Gateway, Tool Gateway, Agent Gateway) is the control plane for all AI interactions — it enforces governance without requiring changes to agent code
- **Amazon Bedrock AgentCore** provides the managed foundation for deploying and operating agents at scale — runtime, memory, gateway, identity, and observability
- **Open standards** (MCP, A2A) and open-source frameworks (Strands Agents) make the platform framework-agnostic — swap agent frameworks without re-architecting the platform
- **Two paths to the same outcome** — whether you use the open-source MCP Registry or the AWS-native AgentCore Registry, the platform architecture and agent code remain similar
- **Separation of concerns** between platform teams (who govern) and AI/ML teams (who build) is what makes the platform scalable across an organization

## Taking This to Production

The workshop deployed a functional platform in a single account. Moving to production involves hardening across several dimensions:

| Area | Workshop | Production |
|------|----------|------------|
| **Networking** | Single VPC, public endpoints | Private endpoints, VPC peering, PrivateLink |
| **Identity** | Single Cognito pool, static tokens | Federated identity (SAML/OIDC), short-lived M2M tokens via OAuth2 Client Credentials |
| **Cost controls** | Manual virtual key budgets | Automated budget alerts, chargeback integration with finance systems |
| **Observability** | CloudWatch logs | CloudWatch dashboards, X-Ray tracing, custom metrics, alerting |
| **Deployment** | Manual CDK/CLI | CI/CD pipelines, blue/green deployments, canary releases |
| **Multi-account** | Single account | Landing zone with separate accounts for platform, workloads, and shared services |

## Next Steps

- Explore the [Amazon Bedrock AgentCore documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html)
- Review the [Strands Agents documentation](https://strandsagents.com/)
- Learn more about [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
- Explore the [FAST repository](https://github.com/awslabs/fullstack-solution-template-for-agentcore) for additional agent patterns
- Talk to your AWS Solutions Architect about deploying the Agentic AI Platform in your organization

## Cleanup

Don't forget to clean up your resources to avoid unexpected charges. See the [Cleanup](../cleanup) section for instructions.
