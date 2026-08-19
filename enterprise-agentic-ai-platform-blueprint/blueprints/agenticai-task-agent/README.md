# Strands Blueprint — agenticai-task-agent

Task-agent pattern per spec §4.1. Structured task execution with deterministic
tool use and optional human-in-the-loop escalation. Ships in v1.

## What it is

A Strands agent scaffolded with:

- Orchestration model defaults (deterministic tool calls, max-iteration guard)
- Baseline Bedrock Guardrail attached (spec §2.4.4)
- Per-app inference profile (cost-allocation tags)
- Memory namespace scoped to tenant/agent/env (spec §3.4.4 static segments)
- Streaming-first invocations via LiteLLM (D-01)

## Shape

```
blueprints/agenticai-task-agent/
├── README.md                      # this file
├── agent.py                       # Strands agent definition
├── tools/                         # per-agent tool definitions
├── prompts/                       # versioned system prompt
├── eval/                          # regression suite for the evaluation gate
└── bedrock.config.yaml            # LiteLLM model mapping + guardrail id
```

## Deployment

The agent is deployed via the workload-account CDK pipeline once the
`packages/agentcore-runtime` L1 wrapper ships in a Phase 5 follow-on. The
agent container image lives in the per-app ECR repo owned by the
`AgenticApp` L3 construct in `packages/agentic-app`.

## Customization

Delivery teams override:

- `prompts/system.md` — system prompt content
- `tools/*.py` — per-agent tools
- `eval/cases.jsonl` — regression cases the evaluation gate scores against
- `bedrock.config.yaml` — model selection from `PLATFORM_ALLOWED_MODELS`
- `agent.py` max-iteration cap, streaming mode, HITL escalation rules
