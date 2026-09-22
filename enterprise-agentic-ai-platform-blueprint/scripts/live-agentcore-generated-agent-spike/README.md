# Generated-agent reference — real Strands `LiteLLMModel` + `MCPClient`

This package closes the roadmap's Round 2.B item **"Build one real Strands
reference agent using only `LiteLLMModel` and `MCPClient`."** It replaces the
intentionally-inert compatibility handler
(`../live-agentcore-runtime-memory-spike/agent/agent.py`) with a real agent that
exercises the three platform integrations the blueprint mandates.

## What it is

- `agent/agent.py` — the container entrypoint. A **pure orchestration core**
  (`ReferenceAgentCore`) that depends only on three small Protocols
  (`LlmClient`, `ToolClient`, `MemoryClient`), plus production adapters that wire
  the real clients:
  - **LLM inference** → Strands `LiteLLMModel` against the Gateway's
    OpenAI-compatible `/inference/v1` endpoint (a **sibling** of `/mcp`, never
    nested under it — the HTTP-400 lesson from the Identity M2M spike is baked
    into `_inference_base_url`). Never a direct Bedrock call.
  - **Tools** → Strands `MCPClient` over streamable-HTTP with the
    `MCP-Protocol-Version: 2025-06-18` header, using `tools/list` and
    `tools/call`. Never a direct Lambda invoke.
  - **Short-term memory** → AgentCore Memory (`create_event` / `list_events`),
    actor/session scoped, storing only fingerprints.
- `agent/Dockerfile`, `agent/requirements.txt` — the ARM64, non-root container,
  pinned to the same zero-finding AL2023 base digest the Runtime/Memory spike
  scanned, with the live-proven Strands `1.44.0` / LiteLLM `1.89.1` pins.
- `test_agent_core.py` — offline tests exercising the pure core with fakes.

## Contract invariants (enforced offline)

- Inference goes through the injected `LlmClient` (a `LiteLLMModel` adapter in
  production); tools through the injected `ToolClient` (an `MCPClient` adapter).
  Static source assertions forbid a direct `bedrock`/`bedrock-runtime` client or
  a `lambda` invoke in this module.
- Every LLM call carries a non-empty `guardrail_identifier`.
- Memory is scoped by `actor_id`/`session_id` only; no real end-user identity in
  session tags (spec §3.4.6).
- A tool the model requests that is not in the subscribed set is refused
  **before** any Gateway call — defense in depth alongside the Gateway's own
  Cedar scoping.
- The bounded tool-use loop honours a max-iteration guard.

## Status

- **Offline-verified.** The pure core is fully unit-tested with fakes (no
  Strands, no litellm, no boto3, no live AWS). Run:

  ```bash
  python -m pytest test_agent_core.py -q
  ```

- **NOT yet live-verified.** A real `InvokeAgentRuntime` round-trip that proves
  `LiteLLMModel` inference + `MCPClient` `tools/list`/`tools/call` + a Memory
  event round-trip through the pipeline-owned Runtime/Memory path — plus MCP and
  bypass twins and zero-residue teardown — remains the open live gate. The
  container must be rebuilt from the exact product revision and re-scanned to
  zero Critical/High findings before any Runtime consumes it by digest.

The bearer token is acquired by the Runtime via AgentCore Identity M2M (workload
identity → OAuth2 credential provider → `GetResourceOauth2Token`), the recipe
live-proven by `../live-agentcore-identity-m2m-spike` and recorded in
`../../evidence/live/2026-09-22-agentcore-identity-m2m-compatibility-spike.md`.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
