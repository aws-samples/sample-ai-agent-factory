# Live evidence — pipeline-owned AgentCore Runtime and Memory foundation

- **Date:** 2026-09-22
- **Status:** PASS for the bounded pipeline-owned foundation described below
- **Deployed Git HEAD:** `442de00a5402b5588bbe4ba2530495532f258b56`
- **Region:** `us-west-2`
- **Topology:** one Platform account and one Workstream test account, with environment-qualified nonproduction and production resources
- **Agent:** deterministic inert Runtime probe; generated-agent `LiteLLMModel`/`MCPClient` integration remains a separate gate

This is a sanitized summary. It contains no AWS account IDs, credentials, pipeline
execution IDs, approval tokens, Gateway/target/Runtime/Memory IDs, KMS key IDs,
role unique IDs, or other account-scoped physical identifiers. Raw evidence
remained in session scratch and CloudTrail and was not committed.

## Preconditions and reviewed implementation

Before live deployment:

- the native Runtime/Memory foundation and fail-closed image scan gate passed the
  TypeScript build, scoped lint and formatting, leakage scrub, shell checks, and
  focused tests;
- 22 image-gate/Runtime/Memory and 17 teardown tests passed, together with the
  97 affected pipeline tests and 118 isolated compatibility-probe tests from the
  preceding revision;
- strict enabled and default-off two-environment synthesis passed
  `AwsSolutionsChecks` and `NIST80053R5Checks` with zero non-compliant findings;
- generated assemblies proved Docker assets used an ARM publisher while file
  assets remained non-privileged;
- Runtime consumed only a repository URI plus the digest returned by the scan
  gate, never a mutable image tag;
- default-off synthesis contained no Runtime, Memory, image-gate, Runtime role,
  or Docker-publisher resources;
- two independent reviews found no Critical or High issue; and
- exact-head JavaScript/TypeScript and Python CodeQL passed before deployment.

Read-only live preflight found the Workstream bootstrap ECR repository immutable
but with `scanOnPush=false` and no account scanning configuration. That was a
release blocker rather than an acceptable assumption. Commit `442de00` therefore
made an exact-digest scan gate part of Runtime creation: it resolves the
content-addressed CDK asset tag, starts or observes the basic scan for that exact
digest, refuses every terminal/unknown status and any Critical or High finding,
and exposes only the accepted digest to Runtime.

The Workstream CloudFormation execution role was simulated against the exact
Runtime, Memory, image-gate, KMS, and service `iam:PassRole` paths before any
pipeline mutation. Every required action evaluated `allowed`; no IAM-policy
update was required in this test topology.

## Governed role and permission handoff

The Workload pipeline root was created from the reviewed assembly and started at
exact HEAD. Both environment-qualified RegistryRoles stacks created stable
Gateway, Registry-validator, Gateway-admin, and Runtime roles with all five
allocation tags. Runtime trust required the AgentCore service principal, exact
source account, and the environment-qualified Runtime ARN family. Its policy
allowed exact image pull, Runtime logging/tracing/metrics, and explicitly denied
direct Bedrock model invocation.

The pipeline paused before Gateway deployment. The Platform pipeline then added
exactly four cross-account Lambda alias permissions: echo and ping in each
environment, each naming only its matching Gateway service role. All other
Platform stages were no-op. The Workload handoff was approved only after all four
live alias policies matched the reviewed principals.

## Nonproduction deployment and behavior

After the fixed six-minute Runtime-role propagation barrier, the pipeline
created the nonproduction ToolGateway and RuntimeMemory stacks in order.

The live state reached:

- AgentCore Gateway `READY` with two `READY` targets;
- ECR image scan `COMPLETE` with zero findings;
- AgentCore Memory `ACTIVE` on a dedicated customer-managed key;
- AgentCore Runtime `READY`, using the exact accepted image digest in `PUBLIC`
  network mode; and
- Runtime environment containing only the associated Memory ID.

Data-plane verification passed:

- exact deterministic `InvokeAgentRuntime` handshake;
- Runtime observed that Memory was configured without returning its identifier;
- synthetic short-term `CreateEvent`/`GetEvent` round trip with exact actor,
  session, event, Memory, and content identity;
- signed MCP initialization and exact two-target `tools/list`;
- echo and ping `tools/call` positives;
- unsigned initialization denied with HTTP 401;
- missing post-initialization protocol version denied with HTTP 400;
- unknown tool denied; and
- direct cross-account invocation of both backing Lambda aliases denied.

The deployed scan-gate handler was also invoked with valid but unequal
`ImageTag` and `AssetHash` properties. It failed before constructing an ECR
client with the exact content-address mismatch error. The same adversarial test
passed in production.

## No-op redeployment

A fresh exact-head pipeline execution repeated Source, Synth, self-mutation,
asset publication, role stages, permission handoff, propagation, and
nonproduction deployment. Both RegistryRoles, ToolGateway, and RuntimeMemory
change sets were created and executed with no changes.

Gateway, target, Runtime, Memory, key, scan-gate contract, and image-digest
identities remained stable. The image scan remained complete with zero findings.
The execution was rejected at its untouched production gate after the no-op
proof; no production action ran in that execution.

## Production deployment and behavior

A separate clean exact-head execution repeated the no-op nonproduction path and
paused at its own production approval. Read-only preflight found no production
Gateway, Runtime, Memory, key-alias, stack, or Runtime-log collision. The same
exact digest remained scan-complete with zero findings, and production roles and
alias permissions matched their environment-qualified principals.

After explicit human approval, production ToolGateway and RuntimeMemory stacks
reached `CREATE_COMPLETE`. Gateway and both targets reached `READY`; Memory
reached `ACTIVE`; Runtime reached `READY` on the exact scanned digest and
observed its Memory configuration.

Production passed the same deterministic Runtime handshake, exact Memory event
round trip, MCP list/echo/ping positives, HTTP 401/400 negatives, unknown-tool
denial, content-address drift rejection, and direct-Lambda bypass denials.

A final exact-head execution proved full cross-environment idempotency. Both role
stacks, both ToolGateway stacks, and both RuntimeMemory stacks reported explicit
no-change preparation and deployment. Every physical identity and the accepted
digest remained stable.

## Permission retirement and dependency-ordered teardown

Before role deletion, the Platform pipeline removed exactly four Lambda alias
permissions—two per environment. Hash comparison proved every common Registry
resource unchanged. The tool functions, aliases, native Registry records, and
DynamoDB rollback tables remained; all four aliases then had no resource policy.

After explicit destructive confirmation, teardown ran in this order:

1. production RuntimeMemory;
2. nonproduction RuntimeMemory;
3. production ToolGateway;
4. nonproduction ToolGateway;
5. production RegistryRoles;
6. nonproduction RegistryRoles; and
7. the Platform-owned Workload pipeline root.

Both Memory stores and both Runtimes deleted before their role stacks. All twelve
Memory service grants retired and both Memory aliases disappeared. The
nonproduction Memory key entered its seven-day pending-deletion window; the
retained production Memory key was explicitly scheduled for the configured
30-day window. The exact 221 MB container image was deleted only after both
Runtimes were absent.

The cleanup removed 26 exact Workstream service-created log groups and six exact
Platform Workload-root log groups after verifying their owning resources absent
and each group empty. The Workload root removed its pipeline, five CodeBuild
projects, generated provider Lambda, eight artifact objects, artifact bucket,
and IAM roles. Its key entered the expected seven-day pending-deletion window.

Independent final inventories found:

- no scoped Workstream stack, Gateway, target, Runtime, Memory, IAM role,
  generated Lambda, state machine, image, alias, grant, or service log;
- no Platform Workload root, pipeline, build project, generated Lambda, artifact
  bucket, alias permission, or service log;
- only the three expected pending-deletion keys, each with zero grants and no
  alias; and
- the intended Platform pipeline, inference Gateway, Guardrails, Registries,
  approved records, four tool functions/aliases, and DynamoDB rollback path
  unchanged.

## Bounded conclusion and remaining gates

This proves the opt-in pipeline-owned AgentCore Runtime and Memory foundation in
one `us-west-2` two-account test topology: exact-head pipeline deployment through
both environments, stable role propagation, immutable digest resolution,
zero-finding exact-digest image admission, Gateway/target/Runtime/Memory readiness,
Runtime and Memory data-plane round trips, MCP and bypass twins, live
content-address rejection, complete no-op redeployment, access retirement,
dependency-ordered teardown, service-grant retirement, exact log/image cleanup,
and zero unintended residue.

It does **not** prove a generated agent. The deployed image intentionally ran the
inert compatibility handler; `LiteLLMModel`, `MCPClient`, secure inference-token
acquisition, and application-level Memory integration remain unwired. The
blocking-finding branch remains offline-proven: this live campaign admitted a
zero-finding digest and did not inject a High-finding image through the deployed
gate. A pipeline-level induced CloudFormation rollback of the RuntimeMemory
stack also remains open; this campaign proved live input rejection, idempotency,
explicit reversibility, and complete teardown instead.

The next dependency gate is an isolated proof that AgentCore Identity can use an
OAuth2 credential provider with `oauth2Flow=M2M` to obtain the existing Cognito
inference-Gateway token without exposing the client secret, followed by an exact
Strands `MCPClient` SigV4 transport proof. EMEA, organization-SCP, load,
concurrency, quota, soak, chaos, upgrade, interrupted-deployment, OTEL span
correlation, measured 24-hour cost, legacy-consumer rollback, and Lambda-wrapper
retirement gates also remain open.
