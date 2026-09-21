# ADR-0016 — AgentCore Gateway inference targets supersede the self-managed LiteLLM proxy

- **Status:** Accepted target-state decision; basic inference endpoint/client and allow/throttle contracts live-verified in `us-west-2`; extended verification remains open
- **Date:** 2026-09-18
- **Supersedes:** ADR-0001 — LiteLLM in the inference path (deviation D-01)
- **Related:** [RFC-0001](../architecture/target-state-architecture.md),
  [ADR-0002](./ADR-0002-cdk-not-terraform.md)
- **Decision drivers:** one supported architecture; remove per-account operational burden from the
  inference path; make rate limiting, guardrail attachment, and model allow-listing non-bypassable
  managed controls; keep generated agent code unchanged.

---

## Context

ADR-0001 put a self-managed LiteLLM deployment in the inference path, one per workload account
(deviation D-01 in [`README.md`](../../README.md) §3.1). It bought virtual-key per-team budgets with
throttling on exceed, per-team cost attribution, unified observability, and reuse of mature existing
code. Deviation D-03 kept the same component but moved it to the platform account.

Three problems have accumulated:

1. **Operational burden on the hot path.** A self-managed proxy in front of every inference call is a
   container fleet, a patching surface, a scaling concern, a secret to rotate, and an availability
   risk — per account under D-01, and a single point of failure under D-03. The blueprint asks a
   customer to operate this before they have run a single agent.
2. **Controls implemented twice.** Guardrail attachment, model allow-listing, and throttling are
   configured in the proxy *and* enforced again by service control policy, IAM, and VPC endpoint
   policy. Four enforcement surfaces for the model allow-list already require a drift test. Adding a
   proxy configuration to the set adds a fifth place to drift.
3. **The proxy is not a governance boundary.** It is a component inside the trust boundary that a
   compromised agent role can, in principle, be configured around. A managed Gateway with its own
   identity, policy, and rate-limiting layers is a boundary the workload cannot reconfigure.

Amazon Bedrock AgentCore Gateway is the managed infrastructure boundary this blueprint already uses
for tool traffic. Using it for inference traffic as well means one boundary technology, one identity
model, and one place where policy, guardrails, and rate limits are evaluated.

The distinction this ADR turns on: **`LiteLLMModel` the client library** and **a self-managed LiteLLM
proxy the deployed service** are two different things. The first is a useful OpenAI-compatible model
adapter in generated agent code. The second is infrastructure. The decision drops the second and
keeps the first.

Official documentation supports both halves of the endpoint-plus-client contract
([Sources](#sources)). The AgentCore Gateway documentation confirms an OpenAI-compatible
`/inference/v1` `chat/completions` endpoint, a JWT presented as the `api_key`, model discovery, and
connectors/providers. The Strands API confirms `LiteLLMModel` extends `OpenAIModel`, forwards
`client_args` to `litellm.acompletion`, and streams by default. The contracts line up and the basic
live path now passes Cognito M2M, 49-model discovery, and `LiteLLMModel` streaming and non-streaming
completion in `us-west-2`. Model-mediated tool calls, JWT refresh under expiry, and usage-token
reconciliation remain open portions of U-1.

---

## Decision

1. **The central AgentCore Inference Gateway, with inference targets bound to allow-listed Bedrock
   models, is the golden-path inference boundary.** It lives in the Platform account, one per
   environment. See [RFC-0001 §5](../architecture/target-state-architecture.md#5-central-inference-gateway-versus-per-workstream-tool-gateway).
2. **The self-managed LiteLLM proxy leaves the golden path.** No supported configuration deploys it
   in the inference path. Its fate in the repository — deleted, moved to experimental, or retained as
   a documented alternative — is open decision D-H in RFC-0001 §14.
3. **`LiteLLMModel` remains the model client in generated agent code**, pointed at the Gateway's
   OpenAI-compatible inference endpoint. `MCPClient` remains the only tool client. Direct
   `bedrock-runtime` and direct `lambda:Invoke` calls from agent code remain contract violations.
4. **Native Gateway rate limiting replaces LiteLLM virtual-key budgets** as the traffic-management
   control, with the dimensions, profiles, and fail-open compensating controls in
   [RFC-0001 §7](../architecture/target-state-architecture.md#7-rate-limiting-dimensions-profiles-and-fail-open-compensating-controls).
5. **Rate limiting is explicitly not authorization.** It evaluates before the policy engine and
   fails open — both confirmed by AWS's 2026-08-06 rate-limiting article ([Sources](#sources)).
   Authorization stays with the policy engine, which fails closed.
6. **Per-tenant Application Inference Profiles remain the cost-attribution mechanism**, unchanged
   from D-03. They are outside the Gateway's failure domain and so also serve as compensating control
   C-7.
7. **This ADR settles the design; live evidence bounds each operational claim.** The basic endpoint,
   client, and ordinary allow/throttle contracts are live-verified in `us-west-2`. The extended U-1
   and U-2 obligations below remain required before broader compatibility, enforcement,
   observability, or fail-open claims are made.

### What replaces each property ADR-0001 bought

| ADR-0001 property | Replacement in the target state | Status |
|---|---|---|
| Virtual-key per-team budgets, throttle on exceed | Native rate-limit profiles keyed on trusted JWT claims (`tenant_id`, `azp`, `sub`, `tier`) or IAM principal/source identity, combined with `targetName`, `qualifiedModelId`, and `toolName`; wildcard entries cover unmatched values | **Partially verified (U-2):** authorized HTTP 200 and exact zero-rate HTTP 429 passed; precedence, fail-open, and telemetry obligations remain |
| Per-team cost attribution | Per-tenant Application Inference Profiles plus the five-tag contract | **Verified** in the current design; carried forward unchanged |
| Unified observability | Gateway metrics and logs, invocation-log archive in Management/Governance, OAM sink | **Planned** |
| Guardrail `default_on` in the proxy | Guardrail mandatory at the inference target, plus the existing SCP and IAM deny-on-null enforcement | **Planned**, built on a **Verified** triple-gate |
| Model allow-list in the router config | Inference targets are bound only to allow-listed models; the allow-list source of truth and its drift test are unchanged | **Planned** |
| Reuse of mature existing code | `LiteLLMModel` as the client adapter — the reuse that mattered is in the agent, not in the infrastructure | **Partially verified (U-1):** streaming and non-streaming completion passed live; tool-call, refresh, and reconciliation checks remain |

---

## Consequences

### Positive

- One boundary technology for inference and tools; one identity model; one policy evaluation point.
- No proxy fleet to operate, patch, scale, or secure on the inference hot path.
- Rate limiting, guardrail attachment, and model binding become managed controls the workload cannot
  reconfigure.
- Generated agent code is expected to be unchanged apart from a base URL and a credential source.
- One less surface in the model-allow-list drift test.

### Negative, and accepted

- **A central Gateway is a single point of failure for all inference in an environment.** Accepted,
  mitigated by multi-AZ, per-environment isolation, and a pipeline gate on Gateway changes. Stated
  plainly in RFC-0001 §5.
- **Fail-open rate limiting is weaker than fail-closed proxy throttling.** A self-managed proxy that
  cannot decide typically refuses. Native rate limiting permits — documentation-confirmed
  ([Sources](#sources)). This is a real regression in traffic-management strength and is why
  RFC-0001 §7.4 names ten compensating controls and why case A-11 injects rate-limiter failure.
- **A managed capability cannot be patched locally.** A missing feature becomes a service request,
  not a configuration change.
- **The golden path intentionally remains Bedrock-only even though Gateway supports additional providers.**
  AgentCore Gateway inference targets can front Bedrock, OpenAI, Anthropic, and other
  OpenAI-compatible providers. This blueprint binds only allow-listed Bedrock models so its SCP,
  Guardrail, inference-profile, and cost contracts remain coherent. Adding another provider requires
  reopening the allow-list, credential, Guardrail, attribution, and adversarial-test decisions; it
  does not require retaining a self-managed LiteLLM proxy.
- **The migration is not free.** Collapsing accounts and removing the proxy touches stateful
  resources. See RFC-0001 §14 D-C and §15.

### Neutral

- ADR-0001 is superseded, not deleted. Deviation D-01 remains in the README and changelog as the
  historical record.

---

## Verification obligations

The basic endpoint/client and ordinary allow/throttle contracts now have exact-commit live evidence.
The remaining rows bound every broader claim and stay release gates, not design blockers.

| # | Obligation | Kind |
|---|---|---|
| V-1 | **Complete U-1's extended live checks.** Streaming and non-streaming `LiteLLMModel` completion against a real Gateway inference target passed; still prove a model-mediated tool call, JWT refresh under expiry, and usage-token reconciliation | Partial pass; remaining release gate |
| V-2 | **Complete U-2's extended live enforcement checks.** Authorized HTTP 200 and exact zero-rate HTTP 429 passed; still prove multi-dimension precedence, catch-all coverage, Policy behavior without customer limits, token reconciliation, and missing-decision alarming. Inject an internal limiter outage only if AWS exposes a supported hook; otherwise retain fail-open as documentation-backed evidence | Partial pass; remaining release gate |
| V-3 | Adversarial case A-3 — non-allow-listed model and cross-tenant inference profile denied by **policy forbid**, not by a throttle and not by a 5xx, with an authorized positive twin | Blocking |
| V-4 | Adversarial case A-4 — an inference call with no guardrail identifier denied at every enforcement point | Blocking |
| V-5 | Adversarial cases A-10 and A-11 — throttle behavior, `blocked` combinations denied by Policy, missing-decision telemetry alarmed, and C-1 through C-3 still denying when customer rate-limit configuration is absent; use managed-outage injection only if AWS supports it | Blocking |
| V-6 | Cost attribution reconciliation — per-tenant inference profile records match the five-tag attribution in the Cost and Usage Report | Blocking for the cost claim |
| V-7 | Mutation fixtures — removing the guardrail mandate, the policy forbid, or the catch-all limit makes the adversarial suite fail | Blocking for suite credibility |

If V-1 fails, this ADR is re-opened: the target keeps a managed inference façade but the adapter
contract, and possibly the client library, change. If V-2 shows fail-**closed** behaviour, the ADR
stands and RFC-0001 §7.4 is downgraded from load-bearing to defence-in-depth — a documentation
change, not a design change.

---

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Keep the self-managed proxy, per account — ADR-0001 unchanged** | Retains per-account operational burden and a fifth allow-list enforcement surface; keeps a component inside the trust boundary doing a boundary's job. |
| **Keep the proxy, centralised in the Platform account — D-03 unchanged** | Same single point of failure as the Gateway, but self-managed, so all the availability risk and none of the managed guarantees. |
| **Direct Bedrock calls from agent code, controls by SCP, IAM, and VPC endpoint policy only** | Loses per-call rate limiting, loses the OpenAI-compatible adapter, and makes guardrail attachment dependent on every agent's own code. Direct Bedrock clients are already a contract violation. |
| **Gateway for inference, proxy retained for non-Bedrock providers** | Two inference paths is the problem this revamp exists to remove. If multi-provider becomes a requirement, it re-opens this ADR rather than forking the path. |
| **Wait for documentation and live spikes before recording a target** | Waiting for documentation was appropriate; those sources now support the protocol and rate-limit design. Waiting for every live spike before settling the architecture would leave the current two-path ambiguity in place. This ADR records the accepted target while making live evidence a non-negotiable release gate. |


## Sources

- [Amazon Bedrock AgentCore Gateway inference targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-targets-inference.html)
- [Configure rate limits for AI traffic on AgentCore Gateway](https://aws.amazon.com/blogs/machine-learning/configure-rate-limits-for-ai-traffic-on-agentcore-gateway/)
- [Strands LiteLLM model-provider guide](https://strandsagents.com/docs/user-guide/concepts/model-providers/litellm/index.md)
- [`strands.models.litellm` API reference](https://strandsagents.com/docs/api/python/strands.models.litellm/index.md)

These sources establish protocol and documented service behavior. The basic live endpoint/client and
allow/throttle results are recorded in
[`../../evidence/live/2026-09-18-agentcore-gateway-spike.md`](../../evidence/live/2026-09-18-agentcore-gateway-spike.md)
and
[`../../evidence/live/2026-09-19-platform-pipeline-deployment.md`](../../evidence/live/2026-09-19-platform-pipeline-deployment.md);
they do not close the remaining V-1 or V-2 checks. No AgentCore-specific managed rate-limiter
outage-injection hook was found in the official material reviewed; the test plan must not fabricate
one.