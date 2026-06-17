# Sample AI Agent Factory

A curated collection of end-to-end samples for building, governing, and operating
**Agentic AI on AWS** — anchored on Amazon Bedrock and Amazon Bedrock AgentCore.

## Our vision

Enterprises are moving past one-off chatbots and proof-of-concept agents. The hard part is
no longer picking a model — it's everything *around* the model: governed access, reusable
tools, security and authorization, observability, and a repeatable way to ship agents to
production. We call that capability an **AI Agent Factory**: the people, patterns, and
platform that let an organization turn ideas into production agents *reliably and at scale*.

This repository gathers complementary samples that, together, show two halves of that story:

1. **A platform foundation** — the governed, enterprise-grade landing zone an organization
   stands up *once* so every team can build on shared, secured infrastructure.
2. **A builder experience** — the low-code / no-code surface that lets engineers (and
   non-engineers) design, deploy, and operate agents *on top of* that foundation without
   hand-writing infrastructure.

The goal is to make the path from "I have an idea for an agent" to "it's running, governed,
and observable in production" as short and as safe as possible.

## What's included

| Folder | What it is | Best for |
|--------|------------|----------|
| [`workshop-building-agentic-ai-platform/`](workshop-building-agentic-ai-platform/) | A hands-on AWS **workshop** for building an enterprise landing-zone pattern for agentic AI on Amazon Bedrock AgentCore — governed model access (LLM Gateway), a tool/agent registry (MCP Gateway & Registry), security controls, and observability. | Platform & ML engineers and solutions architects who want to **understand and build the foundation**. |
| [`Agentic-ai-self-service/`](Agentic-ai-self-service/) | The **AgentCore Visual Workflow Platform** — an n8n-style drag-and-drop builder to design, configure, and deploy AgentCore agents from a canvas, with templates, CloudFormation/Python export, and an enterprise feature set (versioning, Cedar policy, evaluation, observability, registry). | Engineers who want to **build and ship agents fast** on top of AgentCore. |

Each folder is self-contained and keeps **its own `README.md`** with the full architecture,
prerequisites, and deploy instructions for that project. Start there once you've picked a track.

### `workshop-building-agentic-ai-platform/` — build the foundation

A 300-level, multi-module workshop that composes Amazon Bedrock AgentCore with an LLM Gateway
(LiteLLM for governed, cost-attributed model access), an MCP Gateway & Registry (tool and agent
discovery), and Strands Agents — then deploys a real agent on top using FAST. It runs either at
an AWS event (Workshop Studio) or self-paced in your own account via a single deploy script.
See [`workshop-building-agentic-ai-platform/README.md`](workshop-building-agentic-ai-platform/README.md).

### `Agentic-ai-self-service/` — build agents on the canvas

A visual workflow builder for AWS Bedrock AgentCore deployed with API Gateway, Lambda, Step
Functions, DynamoDB, and CloudFront. Drag-and-drop AgentCore components onto a canvas, pick from
13 model providers, deploy through a Step Functions orchestration, and test agents in-canvas —
plus enterprise capabilities like agent versioning & rollback, Cedar policy enforcement,
evaluation, cost analytics, a two-persona agent registry, and CloudFormation / Python export.
See [`Agentic-ai-self-service/README.md`](Agentic-ai-self-service/README.md).


## Getting started

1. Pick a folder above based on whether you want to **build the platform** or **build agents**.
2. Open that folder's `README.md` and follow its prerequisites and deploy steps.
3. Both deploy real, billable AWS resources — read each project's cost notice and tear resources
   down when finished.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information. Each
subproject also documents its own security posture in its `README.md`.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file. Bundled
subprojects retain their own licenses and notices — see the `LICENSE`/`NOTICE` files inside each
folder.
