# Live evidence — pipeline-owned generated agent (LiteLLMModel + MCPClient + Memory) on AgentCore Runtime

- **Date:** 2026-09-23
- **Status:** PASS for the bounded nonproduction AND production deployment-and-invoke gate described below; rollback and teardown remain open
- **Deployed Git HEAD (passing invokes, both environments):** `172f78f6ec26bd6560b1f553a5121407b6010acc`
- **Region:** `us-west-2`
- **Topology:** one Platform account (inference Gateway, Cognito M2M, published cross-account M2M secret) and one Workstream test account (tool Gateway, Runtime, Memory, Identity), environment-qualified nonproduction resources
- **Agent:** the reference Strands agent from `scripts/live-agentcore-generated-agent-spike/agent/` — `LiteLLMModel` against the Platform inference Gateway (`CUSTOM_JWT`, Cognito M2M via AgentCore Identity) and `MCPClient` against the workstream tool Gateway (`AWS_IAM`, SigV4), with AgentCore Memory short-term events

This is a sanitized summary. It contains no AWS account IDs, credentials,
pipeline execution IDs, approval tokens, Gateway/Runtime/Memory/identity IDs,
KMS key IDs, secret suffixes, or other account-scoped physical identifiers. Raw
evidence remained in session scratch, CloudWatch Logs and CloudTrail and was not
committed.

## What this gate proves

One `InvokeAgentRuntime` call against the pipeline-deployed nonproduction
Runtime, from a probe that first verifies its credentials belong to the
expected account, returned HTTP 200 in 10.3 s with every positive check true:

| Check | Evidence | Path exercised |
|---|---|---|
| `markerExact` | exact handshake constant returned | the real container ran the reference agent |
| `discoveredToolsPositive` (`discoveredToolCount = 3`) | MCP `tools/list` succeeded | SigV4 from the Runtime execution role to the `AWS_IAM` tool Gateway |
| `echoToolCalled`, `onlySubscribedToolsCalled` (`toolCalls = [echo]`) | MCP `tools/call` of the subscribed echo tool only | SigV4 tool Gateway → Registry-governed Lambda target |
| `inferenceContentBlocksPositive` (`contentBlocks = 2`) | first turn emitted the exact `TOOL` directive, second turn exactly `<done/>` | `GetWorkloadAccessToken` → `GetResourceOauth2Token` (M2M) → Cognito bearer → inference Gateway → LiteLLM → rated `openai.gpt-oss-120b` with the baseline Guardrail |
| `memoryRoundTrip` | `CreateEvent` then `GetEvent` of the exact `eventId` decoded to the record just written | Runtime execution role → Memory data plane → workstream CMK via `kms:ViaService` |

The Memory record was also read back **independently** of the agent (list
events with payloads under the probe's actor): the passing run's event decodes
as JSON with `toolCalls = [echo]`, the two twin runs' events decode with
`toolCalls = []`, and the one pre-fix event is still stored as the lossy
`{k=v, ...}` rendering — the defect and the fix side by side.

### Adversarial twins (same deployment, same session)

| Twin | Result | What held |
|---|---|---|
| `wrong-account` — probe run with credentials for a different account | refused before any invoke (`guardRefused = true`) | the probe's own account guard |
| `unsubscribed-tool` — user prompt orders a `TOOL` directive for a tool that is **not** subscribed | HTTP 200, marker, `toolCalls = []`, `contentBlocks = 1`, `refusalLayer = model-allowlist` | layer 1: the system prompt lists only subscribed tools and the model declines the directive (standalone reproduction 2/2, deterministic at temperature 0: "I'm sorry, but I can't comply with that."). Layer 2 — the core's `PermissionError` before any Gateway call when a non-subscribed `TOOL` line **is** parsed — stays unit-tested; it was not reachable live through prompting because layer 1 holds |

## Governed handoff

Every pipeline execution in this gate ran from an exact pushed commit through
Source → Build → self-mutation → Assets → RegistryRoles → manual
`GatewayPermissionReady` → enforced role-propagation delay → Nonprod. Before
each approval the deployed Runtime execution role was verified in the
Workstream account with `iam:SimulatePrincipalPolicy` against the exact
resources the next invoke would touch (Memory event actions, and the KMS pair
with `kms:ViaService` + `kms:ResourceAliases` request context), and negatives
(foreign environment Memory, foreign CMK alias, Secrets Manager KMS path,
direct KMS call, `CreateGrant`, `DeleteEvent`, `ListEvents`) stayed denied.
The same policy is simulated offline before every run with
`scripts/live-agentcore-generated-agent-spike/simulate_runtime_identity_grant.py`
against `scripts/render-registry-roles-template.ts` output (12/12 allowed,
15/15 denied at the passing revision).

## Live defects found only by this gate

Each defect below rolled back or failed closed live, was fixed in a separate
commit with a regression test, and is a runbook row in `README.md`. None was
catchable by synth, unit tests or the earlier inert-handler pipeline run.

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | `CreateOauth2CredentialProvider` denied `bedrock-agentcore:CreateTokenVault` | hidden dependent action initialising the account's `default` token vault | grant on exactly `token-vault/default` (+ `TagResource` for mandatory tags) |
| 2 | `CreateWorkloadIdentity` "already exists" as `ValidationException`; `TagResource` on an existing identity 500s; deleted names tombstone | a fixed deterministic identity name is not a safe dependency | mint `<prefix>_<12 hex>` per Create, surface it into the Runtime env, scope IAM to the prefix family |
| 3 | provider role denied `TagResource` on the literal `workload-identity/*` family | create-time tags authorise against the literal family string | grant the full handler action set on the two default containers + two literal families |
| 4 | provider creation denied `secretsmanager:CreateSecret`; failed Create orphaned its identity | managed client secret is created on the caller's behalf; rollback Delete carries a different physical id | scoped `CreateSecret`/`TagResource` on the service's secret family; compensating delete in the handler |
| 5 | Runtime denied `GetWorkloadAccessToken` on the bare directory | token calls authorise on the parent containers too | add both default containers to the Runtime role's token grant |
| 6 | Runtime denied `secretsmanager:GetSecretValue` on the provider's managed secret | Identity reads the managed secret as the caller | grant exactly that secret; drop the unneeded Platform secret/KMS grants |
| 7 | Strands `MaxTokensReachedException` at 256 tokens | the rated model is a reasoning model whose hidden reasoning counts toward `max_tokens` | hard per-turn cap of 2048 alongside the six-iteration guard |
| 8 | `CreateEvent` `ParamValidationError: eventTimestamp` | required member omitted; `ListEvents` ordering unspecified | timezone-aware `eventTimestamp`; read back the exact `eventId` with `GetEvent` |
| 9 | `CreateEvent` `implicitDeny` on the Memory | Runtime role carried no Memory data-plane grant | `CreateEvent` + `GetEvent` on exactly the deterministic Memory name family |
| 10 | `CreateEvent` "Unable to perform KMS operations" | Memory does KMS **as the caller**; key trusted only the service principal, role had no KMS identity grant | key policy trusts exactly the Runtime role via `kms:ViaService`; role mirrors it pinned by the CMK alias |
| 11 | `memoryRoundTrip = false` on the first HTTP 200 | the `blob` document member is returned as a lossy `{k=v, ...}` rendering | carry the record as canonical JSON text in a `conversational` payload |
| 12 | `toolCalls = []` on the first HTTP 200 | the LiteLLM adapter dropped the `system` role, so the model never saw the `TOOL` protocol | pass `system_prompt` to Strands; state the protocol and subscribed names; reproduce standalone 4/4 before redeploying |

Standalone reproduction (`inference_prompt_probe.py`) drove the same
`_LiteLlmAdapter` through the real inference Gateway with a bearer minted
in-process from the sanctioned Platform M2M secret (nothing printed): the
first turn produced the exact `TOOL` directive and the second turn exactly
`<done/>`, identical fingerprints across repeats, for both the strengthened
and the legacy system prompt.

## Offline verification at the passing revision

- 32 generated-agent Python tests (agent core, adapters, probe assessment)
  under a venv built from the container's exact pins; 31 phase-23 conformance
  tests; TypeScript build; formatter; leakage scrub — all green.
- The CDK Runtime image is the exact `@sha256` digest admitted by the
  zero-Critical/High scan gate; Runtime `READY`, Memory `ACTIVE`.

## Production stage

The same exact-head execution then presented the pipeline's `ProdGatewayApproval`.
A stale approval token belonging to an earlier, superseded execution (an old
image) was explicitly **rejected** so the verified revision advanced and
presented its own gate. Before approving, the **production** Runtime execution
role was verified in the Workstream account with `iam:SimulatePrincipalPolicy`:
`CreateEvent`/`GetEvent` allowed on the production Memory name family and the
KMS pair allowed with the AgentCore `ViaService` + production CMK alias context,
while `ListEvents`, the **nonproduction** Memory and the **nonproduction** CMK
alias were denied from the production role (environment isolation).

Production `ToolGateway` and `RuntimeMemory` (first generated-agent production
deploy: minted workload identity, CognitoOauth2 credential provider seeded from
the production M2M secret, digest-pinned Runtime, CMK-encrypted Memory) reached
`CREATE_COMPLETE` with Runtime `READY` and Memory `ACTIVE`, and:

| Probe | Result |
|---|---|
| `positive` | HTTP 200 in 9.9 s; all eight checks true (`discoveredToolCount = 3`, `toolCalls = [echo]`, `contentBlocks = 2`, `memoryRoundTrip = true`) |
| `unsubscribed-tool` | pass, `refusalLayer = model-allowlist`, forbidden tool never called |
| `wrong-account` | refused before any invoke |

Independent read-back: both production Memory events decode as JSON
(`toolCalls = [echo]` for the positive run, `[]` for the twin); exactly one
production credential provider and exactly two production workload identities
exist (the Runtime-managed one and the single identity the custom resource
minted) — no orphans.

## Bounded conclusion and remaining gates

Proven: the pipeline-owned generated agent runs end to end in the real
two-Gateway topology in **both** nonproduction and production — SigV4 MCP
discovery and tool call, AgentCore Identity M2M inference on the rated model
behind the baseline Guardrail, and a verified Memory round trip — with a
wrong-account twin and an unsubscribed-tool twin holding in each environment,
every IAM/KMS grant scoped to exact resource families, simulated positive and
negative before each approval, and production isolated from nonproduction
resources at the role level.

Not yet proven and therefore **not claimed**: an induced RuntimeMemory rollback
through the pipeline, live High-finding image rejection, a live exercise of the
core's layer-2 `PermissionError` (unreachable while layer 1 holds),
matching-principal wrong-ExternalId / wrong-session-name twins, load,
concurrency, quota, soak, chaos, upgrade and interrupted-deployment campaigns,
EMEA regional coverage, Gateway OTEL span correlation, the 24-hour cost
baseline, and dependency-ordered teardown with zero-residue inventory for this
revision.
