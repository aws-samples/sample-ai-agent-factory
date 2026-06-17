---
title: "Module 3b: AgentCore Registry & Gateway"
weight: 60
---

**Track 2 and Track 3**

::alert[**Where to run commands.** All `aws` / `bash` / `python` commands in this module should be run in the **Workshop IDE's terminal** — not your laptop terminal. **When you open a notebook**, select the **`Python 3 (workshop)`** kernel when VS Code prompts in the top-right; `ModuleNotFoundError` usually means the wrong kernel is selected.]{type="info"}

By the end of this module you will have:

- Created an AgentCore Registry and registered 3 MCP tools with metadata
- Exercised the Publisher/Admin approval workflow — separation of duties in action
- Searched the catalog as a Consumer persona and discovered tools by capability
- Invoked tools through the AgentCore Gateway with governed access
- Added Bedrock Guardrails and group-based access control via Cedar policies

In this module you take on the role of a **platform engineer** onboarding a set of tools for an AI/ML team. You will deploy the infrastructure, stand up a governed tool catalog, register tools, and test the full flow from discovery to invocation — all using [AWS-native AgentCore services](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry.html) with no self-managed infrastructure.

::alert[This module is part of Track 2 (Build the Platform) and Track 3 (The Full Journey). If you are on Track 1 (fast path), these steps are completed automatically by the workshop bootstrap — skip to Module 4.]{type="info"}

## The Scenario

Your AI/ML team is building an intelligent travel and commerce assistant. The assistant needs to:

1. **Search flights** — find available flights between cities
2. **Search hotels** — find hotel availability by destination and dates
3. **Look up products** — retrieve product information from the catalog

As the platform engineer, your job is to deploy the tool infrastructure, register each tool in a governed catalog, set up identity and access control, and verify that agent developers can discover and invoke tools safely.

## Three Pillars, Three Personas

The AgentCore Services architecture is built on three pillars — **Discovery** (Registry), **Governance** (Gateway), and **Identity** (Cognito + WorkloadIdentity) — and you will work through them as three personas with distinct IAM boundaries: **Admin** (builds the catalog and approves tools), **Publisher** (registers tools but cannot approve their own), and **Consumer** (agent developer, searches and invokes). This separation of duties mirrors real enterprise governance: the person who builds a tool is not the person who approves it for production use.

The full tables — which service backs each pillar, which IAM role backs each persona, and what each role can and cannot do — live on the [Architecture Overview](step-1/) page. Skim this page, then open step-1 to see the detail.

## What You Will Learn

- How the AgentCore Registry provides a searchable, governed tool catalog
- How the AgentCore Gateway routes and governs tool invocations via Lambda targets
- How Cognito and WorkloadIdentity provide human and machine identity
- How request and response interceptors enforce audit trails and content safety
- How IAM persona roles enforce separation of duties across the platform

## Prerequisites

- Completed Module 1 (The Vision)
- Completed Module 2 (LLM Gateway) — or have the LLM Gateway endpoint available
- Access to the workshop AWS account

## Steps

1. [Architecture Overview](step-1/) — Understand the three pillars, personas, and tool lifecycle
2. [Verify Infrastructure](step-2/) — Verify the pre-deployed CloudFormation stack and capture outputs
3. [Create the Registry](step-3/) — Create an AgentCore Registry with Cognito JWT auth
4. [Register Tools](step-4/) — Register 3 MCP tools, exercise Publisher/Admin approval workflow, EventBridge automation, Registry→Gateway sync
5. [Discover & Search](step-5/) — Search the catalog as Consumer, test authorization boundaries
6. [Test the Gateway](step-6/) — Invoke tools through the Gateway with governed access
7. [Add Guardrails](step-7/) — Wire Bedrock Guardrails and group-based access control
8. [Cleanup](step-8/) — Tear down all AWS resources

## Source Code

The CloudFormation stack (`static/cfn/agentcore/workshop-agentcore-stack.yaml`) is pre-deployed by Workshop Studio — you do not run it by hand. Your work in this module happens inside the notebooks below.

:::code{showCopyAction=false showLineNumbers=false language=text}
source/module-3b-agentcore/
└── notebooks/
    ├── 01-architecture.ipynb       # Architecture overview
    ├── 02-deploy.ipynb             # Verify the pre-deployed stack
    ├── 03-create-registry.ipynb    # Create the AgentCore Registry
    ├── 04-register-tools.ipynb     # Register tools, Publisher/Admin approval, auto-sync
    ├── 05-discover-search.ipynb    # Search catalog as Consumer, authz boundaries
    ├── 06-test-gateway.ipynb       # Invoke tools through the Gateway
    ├── 07-guardrails.ipynb         # Bedrock Guardrails + group-based access control
    └── 08-cleanup.ipynb            # Cleanup resources

static/cfn/agentcore/
└── workshop-agentcore-stack.yaml   # CloudFormation: Cognito, Lambda, Gateway, IAM, Identity (pre-deployed)
:::
