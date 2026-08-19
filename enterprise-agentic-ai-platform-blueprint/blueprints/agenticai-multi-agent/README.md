# Strands Blueprint — agenticai-multi-agent

Supervisor / worker multi-agent pattern per spec §4.1. Ships in v1 scope.

## Topology

```
  ┌──────────────┐        ┌────────────────┐
  │ Supervisor   │───────▶│ Worker agent 1 │
  │ agent        │        └────────────────┘
  │              │        ┌────────────────┐
  │              │───────▶│ Worker agent 2 │
  │              │        └────────────────┘
  │              │                ...
  └──────────────┘
```

Each agent (supervisor + workers) has its own:

- AgenticApp L3 instance → own runtime role, memory namespace, inference
  profile, cost-centre tag
- Baseline guardrail at minimum; workers may override to Internal Tool
  profile with platform approval

## Communication

- Supervisor → Worker via in-account `bedrock-agentcore:InvokeAgentRuntime`
  — scoped by per-agent RBP (spec §3.1.3 / R-RT-010).
- No cross-account calls (spec §2.1.5 / R-ARCH-033 requires RBP + VPCE source
  condition — out of scope for v1 multi-agent).

## Shape

```
blueprints/agenticai-multi-agent/
├── README.md
├── supervisor.py
├── worker.py
├── prompts/
└── bedrock.config.yaml
```
