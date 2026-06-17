---
title: "Module 2: LLM Gateway"
weight: 30
---

::alert[**Where to run commands.** All `aws` / `bash` / `python` commands in this module should be run in the **Workshop IDE's terminal** — not your laptop terminal. **When you open a notebook**, select the **`Python 3 (workshop)`** kernel when VS Code prompts in the top-right; `ModuleNotFoundError` usually means the wrong kernel is selected.]{type="info"}

Deploy an enterprise LLM Gateway using **LiteLLM Proxy** that provides governed, unified access to Amazon Bedrock foundation models — with virtual keys, cost tracking, guardrails, and native integration with Strands Agents.

## The Problem

- Teams across the enterprise call foundation models directly, each managing their own credentials
- No centralized visibility into model usage, costs, or which teams are consuming what
- No guardrails or rate limiting — any team can run up costs or send harmful prompts
- Switching between models or providers requires code changes in every application
- Agents need a single, portable endpoint they can call regardless of the underlying model

## The Solution: LiteLLM Proxy

| Capability | Description |
|-----------|-------------|
| **Unified API** | OpenAI-compatible endpoint — existing code and Strands Agents work by changing the base URL |
| **Virtual Keys** | Per-team/per-key budgets, rate limiting, and spend tracking — no provider keys exposed |
| **65 Bedrock Models** | Route to Claude, Nova, Llama, Mistral, Cohere, Titan, DeepSeek, Qwen, and more through one gateway |
| **Bedrock Guardrails** | Native integration with Amazon Bedrock Guardrails for content safety and PII detection |
| **Cost Tracking** | Per-key, per-team, per-model spend attribution with `/spend/logs` API |
| **Caching** | Identical requests return cached responses — reduces cost and latency |
| **Strands Agents** | Native `LiteLLMModel` provider — tool calling works through the Bedrock Converse API |

## What You'll Deploy

[LiteLLM Proxy](https://docs.litellm.ai/) is an open-source LLM gateway deployed on **ECS Fargate** with a **PostgreSQL sidecar** (for virtual keys and spend tracking), persisted on **EFS**. An **API Gateway HTTP API** provides an HTTPS front door via a VPC Link to an internal ALB. The task role authenticates to **Amazon Bedrock** via IAM — no API keys needed.

```
Strands Agents / Applications
        │
        ▼
┌─────────────────────────────┐
│   API Gateway (HTTPS)       │
│   /* → VPC Link             │
└─────────┬───────────────────┘
          │
┌─────────▼───────────────────┐
│   Internal ALB (private)    │
│   /* → Port 4000            │
└─────────┬───────────────────┘
          │
┌─────────▼───────────────────┐
│   ECS Fargate Task          │
│                             │
│   ┌───────────┐ ┌────────┐ │
│   │ LiteLLM   │ │Postgres│ │
│   │ Proxy     │ │Sidecar │ │
│   │ Port 4000 │ │Port5432│ │
│   └─────┬─────┘ └───┬────┘ │
│         │            │      │
│         │    EFS Volume     │
│         │   (persistence)   │
└─────────┼───────────────────┘
          │ IAM Task Role
          ▼
┌─────────────────────────────┐
│   Amazon Bedrock            │
│   + Bedrock Guardrails      │
│                             │
│   Claude · Nova · Llama     │
│   Mistral · Cohere · Titan  │
│   DeepSeek · Qwen · Gemma   │
│   Nemotron · Writer · more  │
└─────────────────────────────┘
```

## Why LiteLLM?

LiteLLM was chosen over other gateways because:

1. **Native Strands Agents support** — `LiteLLMModel` is a first-class provider in the Strands Agents SDK. Tool calling works through the Bedrock Converse API natively.
2. **Bedrock Guardrails integration** — LiteLLM can apply Amazon Bedrock Guardrails on every request via config, without agent code changes.
3. **IAM auth to Bedrock** — Uses the ECS task role directly, no API keys or translation layers.
4. **Virtual keys + spend tracking** — Multi-tenant cost attribution that maps to the solution's multi-tenant model.

::alert[The LLM Gateway infrastructure is pre-provisioned by Workshop Studio. At AWS events, costs are managed by the event platform and resources are cleaned up automatically.]{type="info"}

## Steps

1. [Architecture Overview](step-1/) — Understand what you're deploying and why
2. [Explore the LLM Gateway](step-2/) — Verify the pre-deployed stack and capture outputs
3. [Virtual Keys & Teams](step-3/) — Set up teams, budgets, and virtual keys
4. [Test Model Access](step-4/) — Chat completions, multi-model routing, and Strands Agent demo
5. [Enterprise Guardrails](step-5/) — Create a Bedrock Guardrail and wire it into the gateway
6. [Spend Tracking & Admin](step-6/) — Review costs, explore the LiteLLM Admin UI
7. [Cleanup](step-7/) — Tear down all AWS resources

## Source Code

:::code{showCopyAction=false showLineNumbers=false language=text}
source/module-2-llm-gateway/
├── cfn/
│   └── litellm-config.yaml     # LiteLLM config: models, guardrails, settings
├── scripts/                     # Deploy, setup keys, test scripts
├── llm_gateway_client/          # Python client library
├── tests/                       # Unit tests
├── walkthrough.ipynb            # Jupyter notebook walkthrough
└── requirements.txt             # Python dependencies
:::
