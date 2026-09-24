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

- **Live-verified through the pipeline.** `live_invoke_probe.py` proved the
  real `InvokeAgentRuntime` round-trip (`LiteLLMModel` inference + `MCPClient`
  `tools/list`/`tools/call` + a Memory event round-trip) in both environments on
  2026-09-23, again after the 2026-09-24 teardown and redeploy, plus the
  wrong-account and unsubscribed-tool twins; see
  `../../evidence/live/2026-09-23-pipeline-generated-agent.md` and
  `../../evidence/live/2026-09-24-redeploy-grant-retirement.md`.

- **RegistryReader trust twins.** `registry_reader_trust_twins.py` drives the
  deployed Workstream validator Lambda — the only principal the reader trust
  admits — through a positive invoke, a wrong-ExternalId twin and a
  wrong-session-name twin (each must fail with exactly STS `403 AccessDenied`),
  restores the function environment byte-for-byte in `finally` and re-proves the
  positive afterwards. Evidence carries fingerprints and codes only; the
  ExternalId value is never printed. Offline contract tests:
  `python -m pytest test_registry_reader_trust_twins.py -q`.

- **Load, rate-limit, concurrency and soak.** `load_rate_limit_probe.py` runs
  `rate-limit-rpm` / `rate-limit-tpm` / `zero-rate` against the Platform
  inference Gateway with the agent's exact OpenAI-compatible request shape
  (bearer re-minted in memory before Cognito's five-minute validity lapses),
  and `concurrency` / `soak` against the Runtime by fanning out the positive
  live probe. Live 2026-09-24: zero-rate exact 429; RPM/TPM approximate
  (traffic shaping, not a ceiling); 8-way concurrency and a 30-minute soak
  green. See `../../evidence/live/2026-09-24-load-rate-limit.md`.

- **Chaos and dependency failure.** `chaos_dependency_probe.py` runs
  `inference-auth` (no / malformed / forged bearer -> 401), `runtime-fuzz`
  (malformed, empty, oversized and wrong-shape payloads -> controlled 4xx or
  the fixed default run; Runtime stays `READY`; positive re-proof) and
  `inference-guardrail`, which found the defect that the Gateway path
  ignores the request guardrail parameter (fixed by the Gateway REQUEST
  interceptor in `packages/platform-inference-gateway/lambda/guardrail-interceptor/`;
  the probe's pass gate now expects HTTP 403 for a tripping prompt and 200
  for the benign control). See
  `../../evidence/live/2026-09-24-chaos-dependency-failure.md`. Offline tests:
  `python -m pytest test_load_rate_limit_probe.py test_chaos_dependency_probe.py -q`.

The bearer token is acquired by the Runtime via AgentCore Identity M2M (workload
identity → OAuth2 credential provider → `GetResourceOauth2Token`), the recipe
live-proven by `../live-agentcore-identity-m2m-spike` and recorded in
`../../evidence/live/2026-09-22-agentcore-identity-m2m-compatibility-spike.md`.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
