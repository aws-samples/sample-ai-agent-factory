# Live evidence — server-side guardrail enforcement on the inference Gateway

- **Dates:** 2026-09-24 (nonproduction) and 2026-09-25 (production)
- **Status:** PASS — the open defect recorded in
  [`2026-09-24-chaos-dependency-failure.md`](2026-09-24-chaos-dependency-failure.md)
  (the Gateway inference path returned model content for a bogus or absent
  `guardrail_identifier`) is closed in both environments by a Gateway
  REQUEST interceptor that runs `bedrock:ApplyGuardrail` before the model is
  called
- **Revisions under test:** Platform `e72c19c` (interceptor, third revision),
  Workload `2d1340f` (generated-agent parser tolerance); both deployed through
  their reviewed pipelines to nonproduction and, after explicit approval, to
  production
- **Probes:** `chaos_dependency_probe.py --mode inference-guardrail`
  (Platform account, Cognito M2M through the inference Gateway) and
  `live_invoke_probe.py --mode positive|unsubscribed-tool` (Workstream
  account, `InvokeAgentRuntime`), both standalone venv files with fail-closed
  account guards; evidence holds codes, error classes, latencies and
  fingerprints only
- **Region:** `us-west-2`

This is a sanitized summary. It contains no AWS account IDs, credentials,
tokens, Gateway/Runtime/Guardrail IDs or other account-scoped physical
identifiers.

## What was built

`packages/platform-inference-gateway/` now requires an `inputGuardrail` (the
same stage's baseline Guardrail id, version and ARN) and wires a REQUEST
interceptor Lambda (`lambda/guardrail-interceptor/index.py`) into the
inference Gateway. For every `POST /v1/chat/completions` body it:

1. extracts the untrusted turns — `user`, `tool`/`function` and role-less
   messages plus bare `input`/`prompt` strings — and leaves the pipeline-owned
   `system`/`developer` prompt, top-level `system`/`instructions` and prior
   `assistant` output unguarded (Bedrock's guarded-content convention);
2. scores **each turn on its own** `ApplyGuardrail` call (turns evaluated
   concurrently; a turn longer than the API limit is chunked but never mixed
   with another turn);
3. fails closed: HTTP 403 with body `{"error":{"code":"guardrail_intervened",
"tripped":[...]}}` on any `BLOCKED` action, 503 when the guardrail is
   unavailable or unconfigured, 413 above the 200 000-character evaluation
   budget, 400 for a non-object body. The Gateway strips custom headers, so
   the marker travels in the JSON body. Request headers are never passed to
   the interceptor and request text is never logged or echoed (the decision
   log carries `decision`, `turns`, `path`, request id and tripped types
   only).

The Gateway role additionally pins `bedrock-mantle:Model` to the allocated
models, so an unallocated model is denied by IAM instead of by the fail-open
native rate limit. Stack outputs `GuardrailInterceptorFunctionArn`,
`EnforcedGuardrailIdentifier` and `EnforcedGuardrailVersion` expose the
enforced control.

### Why not AgentCore Policy (Cedar)

A reversible schema discovery against the live nonproduction Gateway (a
temporary, unattached PolicyEngine, deleted afterwards) showed that the
inference action is `<target>___POST:/v1/chat/completions`,
`context.input.messages` is `Set<record>`, the `BedrockGuardrails::*`
providers accept only `string` arguments, the Dogwood parser has no set
traversal, and the authoring service reports the guardrail request as
"cannot be expressed in Dogwood". The control therefore lives in a REQUEST
interceptor, which the L1 `CfnGateway` supports declaratively.

## Four revisions, three rejected at the gates — what the live gates caught

| Revision                  | Change                                                                                                                                                                               | Live result                                                                                                                                                                                                                                | Decision                     |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------- |
| 1                         | guard every text segment, including the `system` prompt                                                                                                                              | the baseline Guardrail scores the reference agent's own protocol prompt `PROMPT_ATTACK` HIGH, so every governed session returned 424                                                                                                       | rejected at `SecurityReview` |
| 2                         | guard untrusted roles only; plain-language probe prompts                                                                                                                             | first turn allowed (model emits its `TOOL` line), but the follow-up turn — the benign user request **and** the benign tool result sent together — scored `PROMPT_ATTACK` LOW when evaluated as one string, although each alone scored NONE | rejected at `SecurityReview` |
| 3 (`e72c19c`)             | one `ApplyGuardrail` call per untrusted turn, concurrent; the agent's Strands adapter keeps turns separate (`_split_history`)                                                        | tripping prompts 403, benign and two-turn protocol exchange 200 (below)                                                                                                                                                                    | promoted                     |
| 4 (`2d1340f`, agent only) | the `TOOL <name> {…}` parser tolerates the trailing `<done/>` marker the model appends in roughly two of three runs (revision 3's nonproduction positive session returned 424 on it) | positive session 8/8 (below)                                                                                                                                                                                                               | promoted                     |

Every rejection was a pipeline-stage rejection on a real gate; nothing was
promoted around it. Both promotions were explicit approvals after the
nonproduction proofs below passed.

## Nonproduction — PASS (2026-09-24, 19:43Z probe; sessions 22:15Z)

`inference-guardrail` mode, each tripping prompt sent once **with** and once
**without** the client `guardrail_identifier` parameter:

| Request                               | Status                | Body marker            | Tripped                                                                             |
| ------------------------------------- | --------------------- | ---------------------- | ----------------------------------------------------------------------------------- |
| positive (benign, correct parameter)  | 200, model content    | —                      | —                                                                                   |
| benign, parameter absent              | 200, model content    | —                      | —                                                                                   |
| benign, bogus parameter               | 200, model content    | —                      | —                                                                                   |
| prompt attack ×2                      | 403, no model content | `guardrail_intervened` | `contentPolicy.PROMPT_ATTACK`                                                       |
| denied topic (credential exposure) ×2 | 403, no model content | `guardrail_intervened` | `topicPolicy.CredentialExposure`                                                    |
| blocked PII (card + SSN) ×2           | 403, no model content | `guardrail_intervened` | `sensitiveInformationPolicy.CREDIT_DEBIT_CARD_NUMBER`, `…US_SOCIAL_SECURITY_NUMBER` |

Block latency 0.48–1.33 s; `guardrailEnforcedAtGateway: true`;
`parameterIgnoredByConnector: true` (the benign requests prove the client
parameter is still ignored by the connector — enforcement is server-side, not
a client promise). A two-turn protocol exchange through the Gateway (user
request → `TOOL` line → tool result → `<done/>`) returned 200 on both turns.

Generated agent on the same interceptor (Workstream account,
`InvokeAgentRuntime`): positive session HTTP 200 with all eight checks true
(exact marker, 3 tools discovered via SigV4 MCP, governed echo call, two
inference content blocks, Memory configured and round-tripped, only
subscribed tools called; 11.4 s); unsubscribed-tool twin refused at the model
allow-list with no tool call recorded.

## Production — PASS (2026-09-25, 08:17Z probe; sessions 08:19Z)

Before probing: the production interceptor's `CodeSha256` equals the proven
nonproduction function's; the production Gateway is `READY` with exactly one
`REQUEST` interceptor and `passRequestHeaders: false`; the production Runtime
runs the same image digest as the proven nonproduction Runtime.

| Request                               | Status                      | Body marker            | Tripped                                                                             |
| ------------------------------------- | --------------------------- | ---------------------- | ----------------------------------------------------------------------------------- |
| positive (benign, correct parameter)  | 200, model content (2.87 s) | —                      | —                                                                                   |
| benign, parameter absent              | 200, model content          | —                      | —                                                                                   |
| benign, bogus parameter               | 200, model content          | —                      | —                                                                                   |
| prompt attack ×2                      | 403, no model content       | `guardrail_intervened` | `contentPolicy.PROMPT_ATTACK`                                                       |
| denied topic (credential exposure) ×2 | 403, no model content       | `guardrail_intervened` | `topicPolicy.CredentialExposure`                                                    |
| blocked PII (card + SSN) ×2           | 403, no model content       | `guardrail_intervened` | `sensitiveInformationPolicy.CREDIT_DEBIT_CARD_NUMBER`, `…US_SOCIAL_SECURITY_NUMBER` |

Block latency 0.49–0.54 s; `passed: true`. Generated agent: positive session
HTTP 200, 8/8 checks (16.9 s); unsubscribed-tool twin refused at the model
allow-list, no tool call recorded.

### Independent corroboration — the interceptor's own decision log

The production interceptor's CloudWatch log for the probe window
(08:15–08:20Z) contains exactly:

| decision  | tripped                                 | count |
| --------- | --------------------------------------- | ----- |
| `blocked` | `contentPolicy.PROMPT_ATTACK`           | 2     |
| `blocked` | `topicPolicy.CredentialExposure`        | 2     |
| `blocked` | both `sensitiveInformationPolicy` types | 2     |
| `allowed` | —                                       | 6     |

The six allowed decisions are the three benign probe requests (`turns: 1`)
and the three inference calls of the two governed sessions. The positive
session's follow-up call at 08:19:16Z was logged as `turns: 2` and allowed —
the user request and the tool result scored separately, which is the exact
behaviour revision 2 lacked. No log line contains request text.

## Mutation twin

The same probe mode, run against the pre-interceptor revision `d2186b9` on
2026-09-24, returned HTTP 200 with model content for the bogus and absent
parameter cases and is recorded as the failing run in
[`2026-09-24-chaos-dependency-failure.md`](2026-09-24-chaos-dependency-failure.md);
its pass gate now requires every tripping prompt to be refused with the
interceptor marker, and the offline test
`test_guardrail_mode_pass_gate_requires_every_tripping_prompt_blocked` pins
that a 200 on any tripping prompt fails the gate. The interceptor's 18 handler
tests cover role scoping, per-turn scoring, chunking, the 403/503/413/400
paths, the streaming flag, anonymize-only interventions (not a block) and MCP
pass-through.

## Residual risks and open items

- **Guardrail quality is the baseline Guardrail's.** The interceptor enforces
  whatever the stage's baseline Guardrail decides; the three tripping classes
  above are the ones the baseline is configured for. Output filtering
  (RESPONSE interception) is not configured; the OWASP "insecure output"
  row still relies on the generated agent's output handling and the
  evaluation gate.
- **Trusted roles are unguarded by design.** `system`/`developer`/`assistant`
  content is pipeline-owned or model-produced and is not evaluated. A tenant
  that accepts untrusted text into its system prompt loses this protection;
  the agent module documents the contract.
- **Per-turn scoring is a policy choice.** Scoring turns separately matches
  how a Converse loop would have scored each input as it arrived, and avoids
  the false positive revision 2 hit; a multi-turn attack that is only
  detectable across turns is not caught by the interceptor and must be caught
  by the agent's own bounded tool loop and the evaluation gate.
- **Latency.** Each request pays one `ApplyGuardrail` round trip per
  untrusted turn (≈0.5 s per blocked request in both environments;
  concurrent for multi-turn bodies).
- **Region coverage.** Proven in `us-west-2` only; AgentCore Gateway
  interceptors and Bedrock Guardrails availability are part of the EMEA
  region matrix still to be run.
