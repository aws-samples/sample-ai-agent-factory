# Live evidence — load, rate-limit, concurrency and soak on the redeployed revision

- **Date:** 2026-09-24
- **Status:** PASS WITH FINDING — zero-rate control exact; concurrency and soak green; per-model RPM/TPM limits measured as approximate traffic shaping, and the repository's claims downgraded accordingly
- **Revision under test:** `d2186b9` deployed through the pipeline in both environments earlier the same day (`2026-09-24-redeploy-grant-retirement.md`)
- **Probe:** `scripts/live-agentcore-generated-agent-spike/load_rate_limit_probe.py` (standalone, venv, fail-closed account guard, bearer re-minted in memory, evidence holds codes/counts/latencies/fingerprints only)
- **Region:** `us-west-2`
- **Scope:** Platform account — the pipeline-owned nonproduction inference Gateway and its native rate limit; Workstream test account — the nonproduction Runtime/Memory/Tool Gateway

This is a sanitized summary. It contains no AWS account IDs, credentials,
tokens, Gateway/Runtime IDs or other account-scoped physical identifiers.

## Live configuration under test

The nonproduction Gateway rate limit was read back live: status `ACTIVE`, one
dimension key (`qualifiedModelId`), two entries — the allow-listed model at
10 requests/minute and 10,000 tokens/minute, and the `*` wildcard at 0
requests/second — unchanged since the Platform pipeline created it on
2026-09-18. Every probe request carried the mandatory `guardrail_identifier`
and used the same OpenAI-compatible `/inference/v1/chat/completions` contract
as the generated agent's `LiteLLMModel`.

## Zero-rate wildcard (exact control) — PASS

One completion for a model the Gateway exposes but the allocation does not
name (`claude-haiku-4-5`) returned exactly HTTP 429 with a rate-limit error
body. (A first attempt with a model id containing `:` returned HTTP 400
"Model ID contains invalid characters" — a request-shape rejection, not a
rate-limit result, and recorded here so it is not mistaken for one.)

## Per-model requests-per-minute — approximate, not a ceiling

| Run   | Shape                             | Admitted (200) | Throttled (429) | Notes                                                                                                             |
| ----- | --------------------------------- | -------------- | --------------- | ----------------------------------------------------------------------------------------------------------------- |
| burst | 14 requests back-to-back (~8 s)   | 14             | 0               | no throttle at all inside one window                                                                              |
| paced | 60 requests, 3 s apart (~3.4 min) | 38             | 5               | admitted 15, 16 and 7 in the first three wall-clock minutes; 429s scattered, not clustered after the 10th request |
| fast  | 25 requests, 0.5 s apart (~23 s)  | 19             | 6               | first 429 at request 3; 19 admitted within 23 s                                                                   |

Bedrock Mantle's own `Inferences` metric for the allow-listed model
corroborates from the model side: 14, 23, 14, 15 and 19 inferences were
recorded in single minutes during these runs. The limiter therefore shapes
traffic probabilistically around the configured rate rather than enforcing a
hard 10-per-minute ceiling; the recovery request after a quiet minute was
admitted or throttled depending on the run.

In the paced run, requests after ~150 s returned HTTP 403: the probe's own
Cognito client-credentials bearer had passed its five-minute validity. That is
token expiry, not rate limiting; the probe now re-mints before three minutes
and the later runs show no 403. It is recorded because a load tool that does
not refresh its bearer would misreport this as a platform fault.

## Per-model tokens-per-minute — budget-based, catches up late

| Run                     | Requests | Admitted input tokens (Mantle `TotalInputTokens`, one minute) | 429 |
| ----------------------- | -------- | ------------------------------------------------------------- | --- |
| 6 × ~3.6k-token prompts | 6        | 22,044                                                        | 0   |
| 5 × ~8k-token prompts   | 5        | 40,870                                                        | 0   |

Against a 10,000 tokens/minute allocation, four times the budget was admitted
in a single minute with no throttle. This matches the service documentation's
statement that token limits are budget-based and "might temporarily exceed the
configured rate before enforcement catches up"; at this scale the catch-up did
not occur inside the window.

## Consequence for the repository's claims

The Gateway rate limit is documented by AWS as fail-open and, as measured, is
approximate for both request and token rates. It is therefore recorded as
traffic shaping only. README threat-model wording ("model DoS (rate limits +
quotas)", the denial-of-service row) and the architecture's U-2 verification
row were changed in this commit so that per-account Bedrock quotas and SCP/IAM
scoping — not the Gateway rate limit — are named as the quota and abuse
controls. No code change is warranted: the allocation is configured exactly as
designed; the control's semantics are what they are.

## Runtime concurrency — PASS

Concurrent full sessions against the nonproduction Runtime (each session a
real `InvokeAgentRuntime` performing SigV4 MCP `tools/list`, the governed
`tools/call`, M2M inference and a Memory round trip):

| Concurrency | Sessions passed | Invoke latency (s) | Runtime status after |
| ----------- | --------------- | ------------------ | -------------------- |
| 4           | 4 / 4           | 7.5 – 10.2         | `READY`              |
| 8           | 8 / 8           | 6.8 – 13.2         | `READY`              |

The account's AgentCore quota for new Runtime session creation is 25 per
second (adjustable); these runs stayed well inside it and were not meant to
find the ceiling.

## Runtime soak — PASS

One full positive session every 40 seconds for 30 minutes against the
nonproduction Runtime: **48 invocations, 48 passed, 0 failed**; invoke latency
p50 7.95 s, p95 10.03 s, max 10.72 s, mean 7.95 s; Runtime `READY` at the end.
Every invocation exercised the complete governed loop (SigV4 MCP discovery,
governed tool call, M2M inference through the Platform Gateway, Memory event
round trip), so the soak also stands as a 30-minute steady-state check of the
Identity token path and the Memory service.

A first soak attempt ended after eight green invocations when the probe's
session credentials rotated underneath it; it was restarted as a fresh run and
the eight earlier results are not counted above.

## Honest residuals

- Concurrency was proven to 8 simultaneous sessions, not to the service
  ceiling; a ceiling-finding run would spend real inference budget for no
  release value and is out of scope.
- Fail-open injection (rate-limit service unavailable) and missing-telemetry
  alarming remain unproven: the service exposes no outage hook, and the
  Gateway publishes no per-Gateway `Throttles` metric in this account's
  `AWS/BedrockAgentCore` namespace (only `AWS/Bedrock-AgentCore`
  `InboundAuthorization*` series exist), so throttle counts were corroborated
  from the model side instead.
- Production was not load-tested; its Gateway carries the identical
  pipeline-rendered limit and the same service semantics apply.
