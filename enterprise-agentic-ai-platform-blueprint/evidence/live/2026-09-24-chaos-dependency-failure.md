# Live evidence — chaos and dependency failure on the redeployed revision

- **Date:** 2026-09-24
- **Status:** PASS WITH DEFECT — authentication and Runtime input-handling fail closed; the "guardrail on every call" claim does **not** hold on the Gateway inference path and is recorded as an open defect with the documented server-side remedy
- **Revision under test:** `d2186b9`, deployed through the pipeline in both environments earlier the same day
- **Probe:** `scripts/live-agentcore-generated-agent-spike/chaos_dependency_probe.py` (standalone, venv, fail-closed account guard; evidence holds codes, error classes, latencies and fingerprints only)
- **Region:** `us-west-2`
- **Scope:** Platform account — the pipeline-owned nonproduction inference Gateway; Workstream test account — the nonproduction Runtime

This is a sanitized summary. It contains no AWS account IDs, credentials,
tokens, Gateway/Runtime IDs or other account-scoped physical identifiers.

## Why these experiments and not fault injection

The pipeline-owned resources expose no fault-injection hook, and mutating them
out of band (deleting a Gateway target, detaching a role) is exactly the
pipeline-bypass the architecture forbids. The campaign therefore induces
dependency failure at the boundaries the generated agent depends on —
authentication, the guardrail dependency, malformed input — and observes
whether each fails closed, then re-proves the positive path afterwards.

## Inference Gateway authentication — PASS (fails closed)

| Request                                                                     | Result                     |
| --------------------------------------------------------------------------- | -------------------------- |
| no bearer                                                                   | HTTP 401, no model content |
| malformed bearer (`not-a-token`)                                            | HTTP 401, no model content |
| syntactically valid JWT with a forged signature and a plausible scope claim | HTTP 401, no model content |

## Inference Gateway guardrail dependency — DEFECT (does not fail closed)

The generated agent (and every blueprint agent) refuses to start an inference
call without a `guardrail_identifier`, and sends it as an extra body parameter
on the OpenAI-compatible request. Live:

| Request                                              | Result                  |
| ---------------------------------------------------- | ----------------------- |
| correct guardrail id                                 | HTTP 200, model content |
| non-existent guardrail id (`gr-does-not-exist-0000`) | HTTP 200, model content |
| no guardrail parameter at all                        | HTTP 200, model content |

The Gateway's inference connector forwards the request to Bedrock Mantle under
the **Gateway's own service role**, whose policy is `bedrock-mantle:CreateInference`
and `ListModels` on `*` with no condition. The `guardrail_identifier` parameter is
not part of the OpenAI-compatible contract and is ignored end to end. The three
D-01 enforcement layers that guarantee guardrail-on-every-call for direct Bedrock
callers (SCP-02 on the workload principal, the task-role deny, the VPC endpoint
policy) do not evaluate on this path: the caller is the Platform Gateway role,
and the `bedrock-mantle` service defines no guardrail condition key (its
`CreateInference` keys are `Model`, `ServiceTier` and tags). The live Gateway has
no PolicyEngine attached and no interceptors configured.

Consequence: on the D-03 inference path, guardrails are a **client-side
promise** made by agent code, not a platform control. A caller holding a valid
M2M token can obtain guardrail-free inference. The architecture's "Bedrock
Guardrail: mandatory on every call" line and the agent-code comments claiming
SCP/IAM/VPCE backing on this path are therefore overstated for the Gateway
route and were amended in this commit.

Documented remedy (AWS Bedrock AgentCore developer guide, "guardrails in
policies"): attach an AgentCore **PolicyEngine** to the inference Gateway in
`ENFORCE` mode with guardrail policies (`contentFilter`, `promptAttack`,
`sensitiveInformation`; effects `forbid` / `suppressOutput`) plus a permissive
policy for benign traffic; the Gateway then blocks matching requests with
HTTP 403 before they reach the model. That is a behaviour-changing Platform
revision (new PolicyEngine + policies + Gateway association) and is recorded as
the next release blocker, to be proven with a tripping prompt and a benign
control in both environments. A secondary hardening surfaced by the same
inspection: the Gateway role's `CreateInference` grant has no
`bedrock-mantle:Model` condition, so the model allow-list rests only on the
fail-open zero-rate rate-limit entry; pinning the condition to the allocation's
models makes the allow-list fail closed.

## Runtime malformed-input fuzz — PASS (fails closed, no hangs)

Eight raw `InvokeAgentRuntime` calls against the nonproduction Runtime:

| Payload                                                  | Result                                                                                                            |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| empty body                                               | HTTP 400 `ValidationException` (service-side, 0.3 s)                                                              |
| not JSON                                                 | HTTP 424 `RuntimeClientError` (container rejected, 1.6 s)                                                         |
| ~900 KB prompt                                           | HTTP 424 `RuntimeClientError` (8.0 s, no hang)                                                                    |
| session id shorter than the service minimum              | rejected client-side by parameter validation, never sent                                                          |
| `{}`                                                     | HTTP 200: the entrypoint substitutes its fixed default prompt and default actor, then runs the full governed loop |
| wrong field types (`prompt` a list, `actor_id` a number) | HTTP 200: non-string fields are ignored, defaults substituted, full governed loop                                 |
| control characters in the prompt                         | HTTP 200, governed loop                                                                                           |
| `text/plain` content type with a JSON body               | HTTP 200, governed loop                                                                                           |

No request exceeded 8 s; the Runtime stayed `READY`; the positive probe passed
immediately afterwards. The HTTP 200 rows are by design in the entrypoint
(`payload_map.get(PROMPT_FIELD)` falls back to a constant prompt and
`default-actor`), so a malformed payload can never smuggle an unexpected prompt
or actor scope — it degenerates to the fixed default run. Whether an
unrecognised payload should instead be rejected is a product decision; it is
not a security defect because the substituted values are constants.

## Honest residuals

- No managed-service outage (rate limiter, Identity, Memory) can be induced
  without a hook; those remain documented fail-open/fail-closed statements
  backed by the service documentation, not by live injection.
- The guardrail defect above is open until the PolicyEngine-backed guardrail
  policies are deployed through the Platform pipeline and proven live.
