# Live evidence — AgentCore Gateway inference and rate limits

- **Date:** 2026-09-18
- **Status:** PASS for the assertions listed below
- **Environment:** non-production Platform account (account suffix `7240`), `us-west-2`
- **Git HEAD:** `e6cf0182e4c0484de719fc259e41c40db409805c`
- **Source bundle SHA-256:** `2cab635708f0ff84e92940c3b559b4ef4984d62d7d0a4917d7840e6490fc4f4c`
- **SDK:** Boto3 `1.43.97`, Botocore `1.43.97`
- **Strands/LiteLLM:** `strands-agents==1.44.0`, `litellm==1.89.1`
- **Model:** `bedrock-mantle/openai.gpt-oss-120b`

This file is a sanitized summary. It contains no access keys, client secrets,
JWTs, authorization headers, or model response text.

## Assertions live-verified

### Phase A — AWS IAM inbound authorization

1. Created an AgentCore Gateway with AWS IAM inbound authorization.
2. Created a `bedrock-mantle` inference connector using
   `GATEWAY_IAM_ROLE` outbound authorization.
3. Gateway model discovery returned HTTP 200 with 49 models.
4. OpenAI-compatible non-streaming inference returned HTTP 200.
5. OpenAI-compatible SSE streaming inference returned HTTP 200.
6. Created a `qualifiedModelId` rate limit with an explicit zero-rate entry for
   the same known-good model.
7. The same model then returned exact HTTP 429; the earlier HTTP 200 call was
   its positive twin.
8. Deleted the rate limit, target, Gateway, inline IAM policy, and IAM role.
9. Repeated cleanup successfully and verified zero residue.

Key AWS request IDs:

| Operation | Request ID |
|---|---|
| Gateway creation | `69c25b5c-754c-4e0b-b822-3f9515a9bcde` |
| Inference target creation | `f7d1fd44-9324-4c18-8a7d-7457b4e5d626` |
| Model discovery | `b1a86f64-c67f-4fea-86f2-b758c8b68fd3` |
| Non-streaming inference | `93609083-1418-4516-92a3-954b02a85fc1` |
| Streaming inference | `68275f85-18af-473d-b2da-e71013301be7` |
| Rate-limit creation | `9d1b3083-0a7b-43e8-831f-c92c49125ae4` |
| Expected 429 | `f55c014e-477e-4310-a06a-87ec3a3fd959` |
| Gateway deletion | `53593f07-586b-47ec-970d-fb8d5bc7f807` |

### Phase B — Cognito M2M and Strands `LiteLLMModel`

1. Created an ephemeral Cognito User Pool, resource server, confidential M2M
   client, and hosted domain.
2. Created a `CUSTOM_JWT` Gateway restricted to that client ID.
3. Obtained client-credentials access tokens in process memory only.
4. Gateway model discovery returned HTTP 200 with 49 models.
5. Strands `LiteLLMModel` completed a non-streaming invocation.
6. Strands `LiteLLMModel` completed a streaming invocation.
7. An explicit zero-rate entry returned exact HTTP 429 and referenced
   `strands_litellm_model_non_streaming_passed` as its positive twin.
8. Deleted the rate limit, target, Gateway, IAM resources, Cognito domain, and
   User Pool.
9. Independent list/get checks found no remaining Gateway, User Pool, or IAM
   role.

Key AWS request IDs:

| Operation | Request ID |
|---|---|
| Cognito User Pool creation | `a95e3e53-7e83-4628-9a04-a6364d80833b` |
| Cognito client creation | `ba510126-0844-4401-86ba-376c475b5f20` |
| JWT Gateway creation | `af0f4657-e2fd-4387-902a-a2e32150c63e` |
| Inference target creation | `5d96d89c-9400-4c46-8cce-05cb92169ebb` |
| Model discovery | `98b0712a-8218-4a56-a593-ce68df9804e8` |
| Rate-limit creation | `6aa62bbf-f32d-4543-858f-6d7097c24141` |
| Expected 429 | `6ea09f49-512d-4681-8d97-16d269ecb0f0` |
| Gateway deletion | `2f88e1a9-d46f-40b1-ab00-11d50449c601` |
| Cognito User Pool deletion | `9c8f0e87-21be-4eb6-ada7-0f047c96b955` |

## Defects found by real deployment

1. `ListGateways` omits `gatewayArn`; cleanup must resolve `GetGateway` before
   checking tags.
2. AgentCore idempotency tokens remain reserved after resource deletion; tokens
   must be unique per clean run and persisted for retries within that run.
3. The invocation routing ID is `bedrock-mantle/openai.gpt-oss-120b`, while the
   rate-limit `qualifiedModelId` is `openai.gpt-oss-120b`.
4. Cleanup-only success must not overwrite a failed verification result.
5. Top-level cleanup must run for framework-specific exceptions such as
   `MaxTokensReachedException`.
6. A 16-token cap was insufficient for the selected reasoning model; a bounded
   256-token cap passed.
7. Negative evidence must reference a positive event that exists in the same
   evidence bundle.

Every failed attempt above was followed by successful cleanup before the next
attempt.

## Local contract verification

- Python and TypeScript manifest implementations produced byte-identical
  manifests for ASCII, Unicode, quoting, escaping, input-order, and tool-order
  fixtures: 9/9 focused Jest assertions passed.
- TypeScript build passed.
- ESLint passed with zero findings.
- Security leakage scrub passed.
- `git diff --check` passed.

## Not yet proven

This spike does **not** prove:

- Gateway OTEL rate-limit attributes or alarm delivery.
- AgentCore Policy Engine or Bedrock Guardrail enforcement.
- Tool Gateway authorization.
- Runtime, Memory, Registry, pipeline-only Workstream deployment, canary
  endpoint repointing, or cross-tenant controls.
- Full RPM/TPM/CPS fairness behavior.
- Final agent-manifest-to-deployment parity. The source bundle is hashed above;
  the final pipeline evidence must additionally record the canonical agent
  manifest SHA.

These remain blocking release gates.
