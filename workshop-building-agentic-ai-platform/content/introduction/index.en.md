---
title: "Introduction"
weight: 10
---

In this workshop you will build an **Agentic AI Platform** — an enterprise-style landing zone pattern for AI agents using Amazon Bedrock, Amazon Bedrock AgentCore, and open-source projects (MCP, A2A, Strands Agents).

The workshop supports multiple tracks so you can choose based on your role and available time:

| Track | Best For | Path | Time |
|-------|----------|------|------|
| **Track 1: Build an Agent** | AI/ML Engineers, Developers | Module 1 → Module 4 | ~1.5–2 hours |
| **Track 2: Build the Platform** | Platform Engineers, Infra Teams | Module 1 → Module 2 → Module 3 | ~2–3 hours |
| **Track 3: Full Journey** | Solutions Architects, Tech Leads | Module 1 → Module 2 → Module 3 → Module 4 | ~3–4 hours |

Within Modules 3 and 4, you choose between the **OSS path** (MCP Gateway & Registry) or the **AWS-native path** (AgentCore Registry & Gateway) — same tools, different infrastructure.

You will choose your track at the end of Module 1 after understanding the platform vision.

::alert[Module 1 is required for all tracks. It takes about 15–20 minutes and sets the context for everything that follows.]{type="info"}

## Learning outcomes

By the end of this workshop you will be able to:

- **Deploy a governed LLM gateway** that fronts Amazon Bedrock with virtual API keys, per-team model allow-lists, budgets, and spend tracking — so application teams consume models through one audited entry point instead of calling Bedrock directly.
- **Stand up a tool registry and gateway** and register MCP servers and agents in it, using either the open-source MCP Gateway & Registry or the AWS-native AgentCore Registry & Gateway, and explain the trade-off between the two.
- **Apply guardrails and tool-level access policies** at the gateway, so a request for a tool an agent is not entitled to is blocked before it reaches the tool, and sensitive tool output is filtered on the way back.
- **Wire an agent to the platform end to end** — a Strands agent that resolves its tools through the gateway and its model through the LLM gateway, with no endpoint or credential hardcoded in the agent.
- **Observe and troubleshoot agent behaviour** across both gateways using CloudWatch logs, metrics, and the platform dashboard, and trace a single agent invocation through every hop it makes.

Which of these you complete depends on the track you pick — Track 1 covers the agent and gateway consumption path, Track 2 covers the platform build, and Track 3 covers all five.

## Prerequisites

You do **not** need prior experience with AI agents, MCP, or Amazon Bedrock AgentCore — Module 1 introduces every concept the later modules use. What you do need:

| | Requirement |
|---|---|
| **Experience level** | 200 (intermediate). Comfortable in a Linux terminal, and able to read Python well enough to follow a ~40-line script or notebook cell. You will run prepared commands and cells rather than write code from scratch. |
| **AWS knowledge** | Working familiarity with IAM roles and policies, AWS CloudFormation stacks, and Amazon CloudWatch Logs. You should know how to read a stack output and find a log group. |
| **Amazon Bedrock** | No prior use required, but you should know what a foundation model invocation is. Model access is granted for you at an event, and Module 4 walks through requesting it in your own account. |
| **Tooling** | Nothing to install. Every command runs in the browser-based Workshop IDE (VS Code Server) that the environment provisions, which already has the AWS CLI, Python 3.13, Node.js 22, and the Jupyter kernel. |
| **Your own account (self-paced only)** | Administrator access to a **disposable, non-production** AWS account, plus the AWS CLI configured locally to run the one deploy script. See [Self-paced setup](getting-started/self-service/). |

::alert[If any of the AWS knowledge above is new to you, the workshop still works — every command is given in full and every expected output is shown. You will simply spend more time reading and less time exploring.]{type="info"}

## Supported regions

**At an AWS-run event, use `us-west-2` (Oregon).** `contentspec.yaml` lists `us-west-2` as the workshop's only deployable region, so the Workshop Studio guardrail blocks API calls to anywhere else — switching regions mid-workshop will fail rather than fall back.

**Deploying into your own account?** `us-west-2` is the primary validated region and the one every command assumes. `us-east-1` (N. Virginia) also works, including the optional Cedar Policy Engine section in Module 3b — the AgentCore Policy Engine API is available in both regions. Amazon Bedrock AgentCore is not available in every region, so if you pick a third region, check the [AgentCore endpoints and quotas](https://docs.aws.amazon.com/general/latest/gr/bedrock-agentcore.html) first — sections that depend on a capability your region lacks are marked Optional.

## Cost

::alert[**This workshop creates billable AWS resources.** At an AWS-run event the account is provided and the cost is covered for you. If you deploy into **your own account**, you are responsible for the charges — expect roughly **$15-30 for a one-day run** in `us-west-2`, driven mainly by ECS Fargate tasks, an Amazon DocumentDB cluster, NAT gateways, two Application Load Balancers, AWS Lambda invocations, Amazon CloudFront, and Amazon Bedrock model invocations. Cost accrues per hour whether or not you are actively using the environment, so **run the [Cleanup](../cleanup/) module as soon as you finish**. Pricing details: [Amazon Bedrock](https://aws.amazon.com/bedrock/pricing/), [AgentCore](https://aws.amazon.com/bedrock/agentcore/pricing/), [ECS Fargate](https://aws.amazon.com/fargate/pricing/), [DocumentDB](https://aws.amazon.com/documentdb/pricing/), [VPC/NAT](https://aws.amazon.com/vpc/pricing/), [ELB](https://aws.amazon.com/elasticloadbalancing/pricing/), [CloudFront](https://aws.amazon.com/cloudfront/pricing/), [Lambda](https://aws.amazon.com/lambda/pricing/).]{type="warning"}
