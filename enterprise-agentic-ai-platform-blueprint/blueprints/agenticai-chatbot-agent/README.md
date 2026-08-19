# Strands Blueprint — agenticai-chatbot-agent

Chatbot pattern per spec §4.1, shipped in v1 scope (DECISIONS.md Q-BLUEPRINTS-SCOPE).

Differentiators vs `agenticai-task-agent`:

- **Human-in-the-loop escalation path** (DECISIONS.md Q-HITL-PATTERNS). The agent routes unresolvable queries to a human queue instead of looping.

  **G-12 note: HITL convention divergence (intentional).** The chatbot uses an **in-process callable** (`hitl_hand_off`) — fast, synchronous, suitable for chat UX where the agent waits on a queue+websocket loop. The `agenticai-task-agent` and `agenticai-multi-agent` blueprints use an **out-of-process Step Functions** state machine (`hitl_state_machine_arn`) — durable, cross-system, suitable for long-running task / supervisor workflows where the agent process may not be alive when the approver responds. Both feed the same downstream approver SQS queue + DDB pause-token table when wired against the platform `HumanInTheLoopConstruct`. Choose the convention that matches your runtime: chat = in-process, task/supervisor = SF.
- **Streaming-first** — chat UX demands first-token latency < 1.5s (Phase 7 eval-gate threshold).
- **Conversation memory** — uses longer short-term TTL than the task-agent default (90d vs 30d).
- **Customer-Facing guardrail profile** is the default (vs Baseline for task-agent), per spec §2.4.4.

## Shape

```
blueprints/agenticai-chatbot-agent/
├── README.md
├── agent.py                       # conversation state + HITL
├── escalation.py                  # HITL hand-off helper
├── prompts/
│   └── system.md
├── eval/
└── bedrock.config.yaml
```
