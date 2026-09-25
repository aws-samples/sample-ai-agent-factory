# Consolidated live-run handoff — remaining gated checks

This playbook sequences the live-AWS checks that the offline work on PR #31 has
made ready. Every command names its **target AWS account and region** and runs
in that account's CloudShell (or an equivalent federated session). Nothing here
runs offline; each run creates real, billable resources and tears them down to
zero residue.

- **Platform account:** `<PLATFORM_ACCOUNT>`
- **Workstream account:** `<WORKSTREAM_ACCOUNT>`
- **Region:** `us-west-2`
- **Branch head to check out for every run:** the current PR #31 head
  (`git rev-parse HEAD` after checkout of `revamp/agentcore-platform-blueprint`).

> All Workstream mutations flow through the Workload pipeline. Do **not**
> `cdk deploy` a Workstream stack directly. The pipeline is the only sanctioned
> path (`tasks/todo.md` — "no fast-track in any environment").

---

## Run 1 — Generated-agent live `InvokeAgentRuntime` (closes Round 2.B)

**Goal.** Deploy a Runtime built from the real Strands agent
(`scripts/live-agentcore-generated-agent-spike/agent`), then prove a live
`InvokeAgentRuntime` round-trip exercising `LiteLLMModel` inference, `MCPClient`
`tools/list`+`tools/call`, and an actor-scoped Memory event — plus MCP and
bypass twins — then tear down to zero residue.

**Offline prerequisites (already landed on PR #31):**
- `agentImageVariant: "generated-agent"` opt-in on
  `D03WorkstreamRuntimeMemoryStack` (commit `7a49dcb`).
- The real agent + digest-pinned ARM64 container (commit `b00f958`), 15 offline
  tests green.

**Sequence (Workstream `<WORKSTREAM_ACCOUNT>` / `us-west-2`, via the Workload pipeline):**
1. Enable the pipeline Runtime/Memory foundation with the generated-agent image:
   set `agenticai/enablePipelineRuntimeMemory=true` and
   `agenticai/agentImageVariant=generated-agent` in the Workload pipeline root
   context. Push the context change through the pipeline (never a direct
   deploy).
2. The pipeline builds the ARM64 image from the generated-agent directory, runs
   the ECR zero-HIGH scan gate, and deploys Memory (`ACTIVE`) then Runtime
   (`READY`) consuming the image by digest.
3. Supply the Runtime the platform Gateway URL, model id, guardrail id, and the
   AgentCore Identity M2M bearer recipe proven in
   `evidence/live/2026-09-22-agentcore-identity-m2m-compatibility-spike.md`
   (workload identity → OAuth2 credential provider → `GetResourceOauth2Token`).
4. Invoke the Runtime and assert the response:
   - `marker == "agentcore-generated-agent-ok"`,
   - `discoveredToolCount > 0` (MCPClient `tools/list`),
   - at least one `toolCalls` entry (MCPClient `tools/call`),
   - `memoryRoundTrip == true` (actor-scoped Memory event round-trip),
   - `contentBlocks > 0` (LiteLLMModel inference returned content).
5. **Twins:** an unauthenticated invoke → `401`/denied; a direct Lambda/Bedrock
   bypass attempt → denied (the agent never uses those clients).
6. Teardown Runtime → Memory (Runtime-before-Memory ordering), retire grants,
   remove the built image, and run an independent zero-residue inventory.

**Success = DoD Round 2.B closed.** Record sanitized evidence at
`evidence/live/<date>-pipeline-generated-agent.md`.

### Run 1 — live progress (2026-09-22, in flight)

Infra deployed and healthy in nonprod (verified from stack outputs):
Runtime `READY` (`AgenticAI_D03_nonprod_demo_primary_runtime-4FWnYiEydR`, real
generated-agent image by digest `sha256:5805c2fc…`), Memory `ACTIVE`, ToolGateway
`CREATE_COMPLETE` with both cross-account tool targets and the Gateway
service-role InvokeFunction grant applied (Platform pipeline permission phase +
`gaRegistryRecordGenerations={"nonprod":{"tool-echo":2}}` fix). Workload pipeline
paused at `ProdGatewayApproval`.

First live `InvokeAgentRuntime` surfaced defect #5: the container 500'd with
`KeyError: 'AGENTCORE_TENANT_ID'` — the RuntimeMemory stack set only
`AGENTCORE_MEMORY_ID` (built for the inert handler). Root-cause analysis then
found a deeper truth: the deployed D-03 topology is **two Gateways, two auth
models**:

- Tools: workstream tool Gateway `…gw-sknzeminsq`, **AWS_IAM** (SigV4).
- Inference: Platform inference Gateway `agenticai-inference-nonprod-pcneftx12g`,
  **CUSTOM_JWT** (Cognito M2M bearer), target `agenticai-inference-nonprod-bedrock`.

**Inference leg proven live:** Cognito M2M token → `…/inference/v1/chat/completions`
→ **HTTP 200** real completion on the rated `openai.gpt-oss-120b` (unrated models
429 by the designed zero-rate `*` fallback — expected governance). Model ids from
`/models` are already target-qualified; do not re-prepend the target name.

**Landed (agent + stack + tests, commits `4bfc8e7`, `153bd81`):**
- `agent.py`: split into `mcp_gateway_url` + `inference_gateway_url`; SigV4
  `auth_mode` for the AWS_IAM MCP Gateway (`_SigV4HttpxAuth`); Identity-based
  `_fetch_inference_bearer` (`GetResourceOauth2Token` against a `CognitoOauth2`
  provider). Env seams for tests.
- `d03-workstream-runtime-memory-stack.ts`: `generatedAgentRuntimeConfig` prop +
  variant-aware, fail-closed `buildRuntimeEnvironment`. 27/27 conformance green.

**Remaining infra (open) — the cross-account credential-provider decision:**
`GetResourceOauth2Token` needs a `CognitoOauth2` credential provider in the
**Workstream** account seeded with the **Platform** inference Gateway's Cognito
client id + secret. The client secret lives only in Platform Cognito (the stack
intentionally does not output it). Options to resolve before wiring:
1. Platform pipeline mints a dedicated client secret and publishes it to a
   Secrets Manager secret with a cross-account resource policy readable by the
   Workstream runtime/deploy role; the Workload pipeline reads it at deploy time
   to seed the provider. (Explicit, auditable; a secret crosses accounts once.)
2. Workstream deploy role assumes a narrow Platform role that returns a
   short-lived client secret via `AddUserPoolClientSecret` at deploy time.
3. Keep inference in the workstream tool Gateway (add a Bedrock inference target
   there) so one AWS_IAM Gateway serves both — removes cross-account M2M
   entirely but changes the inference-governance boundary.

Plus runtime role IAM grants: `bedrock-agentcore:InvokeGateway` on the workstream
Gateway ARN (currently absent — role only pulls image/logs/traces and *denies*
direct Bedrock), and `GetResourceOauth2Token`/`GetWorkloadAccessToken` for the
inference bearer. Then thread the env (`AGENTCORE_INFERENCE_CREDENTIAL_PROVIDER`,
`AGENTCORE_INFERENCE_SCOPE`, `AGENTCORE_INFERENCE_GATEWAY_URL`,
`AGENTCORE_MODEL_ID=agenticai-inference-nonprod-bedrock/openai.gpt-oss-120b`,
`AGENTCORE_GUARDRAIL_ID`, `AGENTCORE_GATEWAY_URL`, `AGENTCORE_SUBSCRIBED_TOOLS`)
through the pipeline, redeploy nonprod, and re-invoke for the green contract.

---

## Run 2 — R2 Workload live deploy + adversarial matrix

**Goal.** Deploy the GA Registry consumer Tool Gateway through the Workload
pipeline in both environments and prove the full positive + adversarial matrix,
then teardown.

**Offline prerequisites (landed):** R2 consumer (`3870e0e`), teardown hardening
(`7774299`), and the sanctioned surfaces' governing logic — `RegistryReaderRole`
(pre-existing), `AgentBuilderInspectRole` (`f64814f`), `AgentRegistrationApi`
authorization core (`fea0640`).

**Sequence (per README §6.3 Path B, pipeline-owned):**
1. Platform pipeline with R2 code, `enableGaGatewayInvokePermissions=false`
   first; approve updated Registry records.
2. Resolve one GA context file per Platform environment
   (`pipelines/resolve_ga_registry_context.py`).
3. Workload pipeline deploys the three stable roles per environment, stops at
   `GatewayPermissionReady`.
4. Re-run Platform with `enableGaGatewayInvokePermissions=true` and the two
   `GatewayServiceRoleArn` outputs; confirm each alias policy names the role ARN
   (not a stale `AROA...`); approve `GatewayPermissionReady`.
5. Workload deploys the nonproduction Gateway; validator requires `APPROVED` +
   descriptor-digest + target-ARN match. Prove MCP `tools/list`+`tools/call`.
6. **Adversarial twins:** wrong descriptor digest, wrong target ARN, wrong tool,
   missing/`DRAFT`/`DEPRECATED` record, wrong ExternalId, wrong session name,
   wrong account — each must deny with its exact expected code.
7. **`RegistryReaderRole` / `AgentRegistrationApi` live twins** (now unblocked by
   the deployed validator role): positive reader access; wrong-ExternalId and
   wrong-session-name denials; and the REG-07..REG-12 authority denials against
   the deployed intake path.
8. Approve `ProdGatewayApproval`; repeat proof in production.
9. Dependency-ordered teardown (the legacy `allowedToolIds` rollback mode was
   retired on 2026-09-25; retire the Platform grants first, then
   targets → barrier → Gateway → RegistryRoles → pipeline root) with an
   independent residual inventory.

Record evidence at `evidence/live/<date>-pipeline-r2-workload.md`.

---

## Run 3 — PolicyEngine `ENFORCE` live parity (now with per-`sub`)

**Goal.** Re-prove the Gateway PolicyEngine `LOG_ONLY → ENFORCE` path — now
that the Cedar grammar carries per-developer subject entitlements
(`allowedSubjects`, commits `60bba38`/`588b39c`) — and confirm allow/deny parity
between the native PolicyEngine and the retained Lambda wrapper for the
subject/group matrix. Only after parity holds may wrapper retirement proceed.

**Offline prerequisites (landed):** wrapper + catalogue subject grammar with 63
tests; the native `composeAgentCorePolicyDefinitions` subject support is
deliberately **not** yet wired (documented) — extend it as the first step of
this run under review, since it changes the live-proven `f45a12c` behavior.

**Sequence (Workload pipeline, `us-west-2`):**
1. Extend `composeAgentCorePolicyDefinitions` to emit subject permits for
   `CUSTOM_JWT`, under independent review, with conformance pinning the
   rendered template. (This is the one behavior-changing offline step gated to
   this run.)
2. Start in `LOG_ONLY`; deploy nonproduction; assert parity with the wrapper on
   the full subject/group matrix (member/non-member, two developers same group
   different subject, rotated-in/rotated-out subject, missing/forged `sub`).
3. Switch to `ENFORCE`; re-prove `tools/list` filtered and direct-call denial
   for an unpermitted principal.
4. Mode rollback `ENFORCE → LOG_ONLY`; fail-closed teardown; zero residue.
5. Only after parity + rollback + teardown pass in both environments: retire
   `@agenticai/tool-cedar-wrapper` in a separate reviewed revision.

Record evidence at `evidence/live/<date>-pipeline-policyengine-subject.md`.

---

## Run 4+ — org-level gates (after runs 1–3)

These do not depend on runs 1–3 but are heavier / org-scoped:
- **SCP 01–12 org soak** through the sandbox OU (Management `<PLATFORM_ACCOUNT>`? —
  confirm the Management/Governance account; Organizations-level, requires
  explicit confirmation for any SCP attach).
- **EMEA region matrix** — repeat the inference-Gateway + Runtime/Memory +
  PolicyEngine proofs in an EMEA AgentCore region (mandatory per the standing
  EMEA rule; do not extrapolate from `us-west-2`).
- **OTEL rate-limit span correlation** — the reproduced `us-west-2` blocker;
  retry after any AWS-side fix.
- **24-hour cost baseline** — sustained live traffic against the deployed slice.

---

## Standing constraints for every run

- Name the target account + region before each operator command; precheck with
  `aws sts get-caller-identity`.
- No direct destructive production action without explicit confirmation.
- Every behavior-changing revision: reviewed pipeline deploy, real
  positive + adversarial calls, rollback proof, residual cleanup, sanitized
  evidence tied to the commit SHA. Zero known defects before any wide-adoption
  claim.
- PR #31 stays open and unmerged.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
