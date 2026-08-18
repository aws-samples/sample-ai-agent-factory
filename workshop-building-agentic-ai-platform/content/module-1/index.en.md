---
title: "Module 1: The Vision"
weight: 20
---

Estimated time: 15-20 minutes

In this module, you will understand why enterprises need a platform approach to agentic AI, what the Agentic AI Platform is, and how to choose the right workshop track for your goals.

## Who this workshop is for

Platform engineers, cloud architects, and ML/AI engineers who are responsible for making agentic AI usable by *many* teams rather than shipping a single agent. You will act as the platform team: standing up the model, tool, and agent governance layers that application teams then build on.

You do not need prior AgentCore experience. You do need to be comfortable reading Python and running AWS CLI commands from a terminal.

## Prerequisites

- Working knowledge of the AWS CLI and basic AWS concepts (IAM roles, CloudFormation, VPCs)
- Ability to read Python — you will run and edit small scripts and notebooks, not write services from scratch
- Familiarity with REST APIs and JSON
- An AWS account with the workshop infrastructure deployed, plus the Workshop IDE (both are provided for you at an AWS event; see [Getting Started](../introduction/getting-started/) if you are running this yourself)

Helpful but not required: prior exposure to the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), Amazon Bedrock, or LiteLLM.

## Supported regions

**At an AWS-run event, use `us-west-2` (Oregon).** It is the only region the event account is provisioned in, and the Workshop Studio guardrail blocks API calls to other regions — switching regions mid-workshop will fail rather than fall back.

**Deploying into your own account?** `us-west-2` is the primary validated region and the one every command in this workshop assumes. `us-east-1` (N. Virginia) also works, including the optional Cedar Policy Engine section in Module 3b — the AgentCore Policy Engine API is available in both regions. AgentCore is not available in every region, so if you pick a third region, check the [AgentCore endpoints and quotas](https://docs.aws.amazon.com/general/latest/gr/bedrock-agentcore.html) first — sections that depend on a capability your region lacks are marked Optional.

## Cost

::alert[**This workshop creates billable AWS resources.** At an AWS-run event the account is provided and the cost is covered for you. If you deploy into **your own account**, you are responsible for the charges — expect roughly **$15-30 for a one-day run** in `us-west-2`, driven mainly by ECS Fargate tasks, an Amazon DocumentDB cluster, NAT gateways, two Application Load Balancers, and Amazon Bedrock model invocations. Cost accrues per hour whether or not you are actively using the environment, so **run the [Cleanup](../cleanup/) module as soon as you finish**. Pricing details: [Amazon Bedrock](https://aws.amazon.com/bedrock/pricing/), [AgentCore](https://aws.amazon.com/bedrock/agentcore/pricing/), [ECS](https://aws.amazon.com/fargate/pricing/), [DocumentDB](https://aws.amazon.com/documentdb/pricing/), [VPC/NAT](https://aws.amazon.com/vpc/pricing/), [ELB](https://aws.amazon.com/elasticloadbalancing/pricing/).]{type="warning"}

## What you will learn

- The enterprise challenges that drive the need for an Agentic AI Platform
- How AI applications evolve from simple chatbots to complex multi-agent systems at scale
- The six core pillars of the platform and the AWS services and open-source projects that power them
- The gateway pattern and why it is central to governing agents, models, and tools
- How to choose your workshop track based on your role and available time

::alert[This is a shared module. All participants should complete this before choosing their track.]{type="info"}
