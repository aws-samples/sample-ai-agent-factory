# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Pipeline-owned `InferenceGatewayStack` in both Platform deployment stages.
- Native `AWS::BedrockAgentCore::Gateway` with Cognito client-credentials JWT authentication and the MCP `2025-11-25` protocol.
- Native Bedrock Mantle inference target using `GATEWAY_IAM_ROLE` and a source-account/source-Gateway constrained service role.
- Native Gateway rate limits with explicit provider-qualified model RPM/TPM allocations and a zero-rate wildcard fallback.
- Five-tag allocation contract on taggable Gateway resources and non-secret CloudFormation outputs for workload integration.
- Conformance coverage for resource shape, IAM trust, Cognito M2M, model-ID validation, lifecycle ordering, and teardown context.
- Cleanup-first AgentCore Gateway PolicyEngine compatibility runner with four-user `sub`/group semantics, strict Cedar validation, exact JWT negatives, mode rollback, ownership-checked teardown, and 158 focused tests.
- Cleanup-first AgentCore Runtime and Memory compatibility runner with exact live ownership, digest-only images, run-bound state/evidence, partial-create recovery, deterministic Runtime/Memory round trips, completed-run idempotency protection, failure-tolerant ordered cleanup, pinned Boto3/Botocore `1.43.97` contracts, and 118 focused tests.
- Fail-closed pipeline Runtime image gate that binds the CDK asset tag to its exact digest, starts or observes an ECR scan for that digest, blocks `CRITICAL`/`HIGH` findings and every terminal or unknown scan status, and exposes only the scanned digest to `AWS::BedrockAgentCore::Runtime`. Exact commit `442de00` passed live nonproduction and production deployment, zero-finding scan admission, content-address drift rejection before ECR access, full no-op redeployment, Runtime/Memory round trips, MCP/bypass twins, permission retirement, ordered teardown, grant retirement, exact log/image cleanup, and zero unintended residue.
- Opt-in Workload pipeline PolicyEngine path with explicit `OFF`/`LOG_ONLY`/`ENFORCE` modes, exact IAM or OAuth principals, one strict native policy per subscribed tool, CMK encryption, six-minute Gateway-role propagation, signed association retry, convergent mode/detach waiters, and wrapper-preserving rollback.
- Cleanup-first GA Agent Registry compatibility runner with native CloudFormation types, custom governance-document round trip, explicit `DRAFT → submit → APPROVED`, data-plane discovery, deterministic update rollback, ownership-checked teardown, and 30 focused tests.
- Pipeline-owned blue-green GA Registry producer: native Registry and tagged `CUSTOM` governance records, exact `agent-registry` read permissions, conditioned `RegistryReaderRole`, per-record/versioned SSM discovery parameters, and `RetainExceptOnCreate` state protection alongside the unchanged DynamoDB rollback path.
- Template-bound pipeline Registry approval utility with all-`DRAFT` atomic preflight, exact processed-template descriptor comparison, bounded approval/discovery polling, credential-safe evidence, and 77 focused tests.
- R2 GA Registry consumer: pipeline-owned environment-qualified Platform tool aliases, strict SSM/Registry resolver, stable tool-ID developer subscriptions, a three-role Workstream prerequisite stage, explicit Platform permission handoff, and deploy-time `APPROVED` plus descriptor-digest validation.
- Environment-scoped `agenticai/gaRegistryRecordGenerations` recovery control for replacing one terminal GA Registry record while preserving the Registry, reader role, tools, SSM namespace, and DynamoDB rollback path.
- Server-side guardrail enforcement on the central inference Gateway: a required `inputGuardrail` (the same stage's baseline Guardrail id, version and ARN) and a Gateway REQUEST interceptor (`packages/platform-inference-gateway/lambda/guardrail-interceptor/`) that runs `bedrock:ApplyGuardrail` on every text segment of every inference request body before the model is called and fails closed (HTTP 403 `guardrail_intervened` on a `BLOCKED` action, 503 when the guardrail is unavailable or unconfigured, 413 above the 200 000-character evaluation budget, 400 for a non-object body); request headers are never passed to it and it never logs or echoes request text. Guarded content follows Bedrock's guarded-content convention: `user`, `tool`/`function` and role-less turns plus bare `input`/`prompt` strings are evaluated; the pipeline-owned `system`/`developer` prompt, top-level `system`/`instructions` and prior `assistant` output are not, and each untrusted turn is scored on its own `ApplyGuardrail` call, never concatenated with other turns (the first revision guarded the system prompt and the baseline guardrail scored the reference agent's protocol prompt `PROMPT_ATTACK` HIGH, refusing every governed call in nonproduction; the second revision scored a benign request and a benign tool result jointly and tripped `PROMPT_ATTACK` LOW on the agent's follow-up turn; both were rejected at `SecurityReview` and never reached production). The reference agent's Strands adapter now hands prior turns to Strands as conversation history and sends only the newest untrusted turn as the prompt instead of collapsing user and tool turns into one user turn. The interceptor role holds `bedrock:ApplyGuardrail` on exactly that guardrail ARN, the Gateway role may invoke exactly that function, and the Gateway depends on both grants. New outputs `GuardrailInterceptorFunctionArn`, `EnforcedGuardrailIdentifier` and `EnforcedGuardrailVersion`. Offline handler tests (18) and phase-9 conformance tests cover the contract; the live tripping-prompt/benign-control proof in both environments is recorded under Verification. The reference agent's tool-protocol parser also accepts the exact `<done/>` marker after a `TOOL` line's JSON arguments, which the rated model appends in roughly two of three runs.
- Standalone load/rate-limit/concurrency/soak probe (`load_rate_limit_probe.py`) and chaos/dependency-failure probe (`chaos_dependency_probe.py`) under the generated-agent spike, with offline contract tests; the chaos probe's `inference-guardrail` mode now requires prompt-attack, denied-topic and blocked-PII prompts to be refused with the interceptor's `guardrail_intervened` marker with and without the client guardrail parameter.
- Deployment-continuity sampler (`deployment_continuity_probe.py`) that runs a full governed session every 15 s through a deployment and gates on availability, the observed `agentVersion` sequence (`--expect-transition`, `--expect-final-version`, `--expect-stable-version`) and settling, recording the failed check names, `stopReason` and `protocolRepairs` per sample. The generated agent reports `agentVersion`, `stopReason` (`done`, `protocol_violation`, `max_iterations`) and `protocolRepairs` in every response; the positive gate requires `stopReasonDone`.
- Teardown for the pipeline topology. `scripts/final_teardown.py` deletes the pipeline-deployed stacks one account at a time in dependency order, which `scripts/teardown.sh` has no mapping for. It is a dry run by default, stops at the first failure, refuses the wrong account and an SDK too old to model the calls it needs, and saves its plan before every delete so `--resume-residue` can finish an interrupted run. It cleans what each stack retained: GA Registry records and then the Registry, DynamoDB tables, SSM parameters, Cognito pools (protection is switched off only when it is on), log groups, de-duplicated agent image digests, and keys last. `scripts/sweep_orphans.py` is a plan/apply sweep for residue no stack owns. It refuses while project stacks are live and never touches Object-Locked data or the keys that encrypt it. Apply only removes entries that are in the plan and still match the live predicate. `scripts/residue_inventory.py` is a read-only, per-Region count of project resources across 25 surfaces; its KMS matcher is pinned to the exact key descriptions the constructs emit by a test that scans the sources. The three scripts have 66 offline tests; mutation checks confirmed the save-before-delete, not-found, image de-duplication, key ordering, locked-key protection, apply re-validation and live-stack refusal guards.

### Fixed

- Generated agent 1.2.2: the container no longer falls back to `us-west-2` when no Region is set. It signed MCP tool calls for, and looked up AgentCore Memory and the workload identity in, `os.environ.get("AWS_REGION", "us-west-2")`, and the Runtime stack passed no Region, so outside `us-west-2` the agent depended on the Runtime injecting `AWS_REGION`, which the AgentCore Runtime service contract does not document. Every `us-west-2` run was blind to this because the fallback equalled the Region. The stack now sets `AGENTCORE_REGION` to its deploy Region; the agent reads `AGENTCORE_REGION`, then `AWS_REGION`, then `AWS_DEFAULT_REGION`, and fails closed if none is set. An `eu-west-1` synth twin and a no-Region-literal source check pin both sides, and removing either safeguard fails them.
- The workstream Registry validator assumed the cross-account RegistryReader role through the global STS endpoint, signed for `us-east-1`, although the function already received its Region. AWS serves global-endpoint requests locally in Regions enabled by default (so `eu-west-1` was not affected), but from `us-east-1` in opt-in Regions, and global-endpoint session tokens are valid only in Regions enabled by default. The validator now calls `sts.<region>.amazonaws.com` signed for the deploy Region, as AWS recommends; this needs the Regional STS endpoint active in the Platform account, which is the default. An `eu-west-1` twin asserts that no call leaves the Region.
- Teardown verification could report a dirty account as clean. `tests/teardown/` checked only `AgenticAI-D03-` names and KMS aliases, and only as `xfail`. A read-only inventory of the Workstream test account on 2026-09-25 found residue from the May–July campaigns that those checks could not see: 101 customer-managed keys without aliases, 61 service log groups, one workload identity and two stub Cognito pools in `us-east-1`, plus eight EU AI Act buckets under a COMPLIANCE Object Lock until 2033. README §16 now makes the new inventory the gate, documents the pipeline-topology teardown, and states that COMPLIANCE-locked records and their keys cannot be removed before retention ends. The 2026-09-26 final campaign deleted every deletable stack and orphan across all three accounts in `us-west-2` and historical `us-east-1` runs: the Workstream sweep removed 61 old logs, two pools and three buckets and scheduled 92 keys; the Platform sweep removed eight service logs and scheduled four keys; the Management sweep removed two stacks, 62 logs across both Regions and scheduled 42 historical keys after deleted-stack, timestamp, grant and surviving-reference ownership checks. Terminal exceptions are explicit: nine protected buckets and their nine keys, one AWS service-linked workload identity, KMS keys pending deletion, and one temporary read-only teardown policy awaiting host-blocked operator removal. Sanitized evidence is in `evidence/live/2026-09-26-final-teardown.md`.
- SCPs, found with IAM Access Analyzer and the IAM policy evaluator (`evidence/live/2026-09-25-scp-live-evaluation.md`): SCP-01 conditioned on the non-existent `bedrock:FoundationModel` key under `ForAllValues:` and would have denied every model — the allow-list is now `NotResource` over the exact foundation-model and matching inference-profile ARNs; `bedrock:Converse`/`ConverseStream` (not IAM actions) removed from SCP-01/02/04; SCP-02 uses `ArnNotLike` on the approved guardrail and its versions plus an explicit empty-string deny; SCP-03/04 take explicit VPC endpoint ids (a StringList SSM reference resolved to one comma-joined string and denied every AgentCore call) and are emitted only when supplied, as VPC-mode controls; SCP-09/11 deny creation on `*` (create calls have no ARN, so the scoped statements never matched); SCP-11 covers the GA `agent-registry` namespace the platform deploys and exempts the Platform pipeline's CloudFormation execution role; SCP-12 drops never-matchable `Create*` and `organizations:*`; `OrganizationConstruct` enforces the 10-SCPs-per-target Organizations quota and passes guardrail and VPC endpoint ids through `OrgStack`. `scripts/scp-sandbox-soak.sh` now runs allow and deny twins and counts only explicit SCP denies (one of its old commands did not exist and "passed" by failing).
- Generated agent 1.2.0 (`13153b4`): a reply carrying neither a line-start `TOOL` directive nor `<done/>` is no longer treated as task completion. Live, the rated reasoning model occasionally glued a reasoning fragment in front of the directive (`We need to output the TOOL line.TOOL …`), and the agent returned HTTP 200 with the success marker and no tool call (4 of the 74 pre-1.2.0 sessions sampled on 2026-09-25, first seen during the production 1.1.0 upgrade). The loop now sends a plain runtime notice as a user turn at most twice within the iteration bound, never executes a buried directive, reports `protocol_violation` when the budget is spent, no longer turns an empty reply into the done marker, and asks declines to end with `<done/>`.

### Changed

- Added `eu-west-1` to the governed Region SSOT as the sole current EMEA onboarding target (AWS Agent Registry is available in Europe only in Ireland). All eight CDK app stages now resolve their Region through one fail-closed helper: ambient `CDK_DEFAULT_REGION` wins over optional explicit context, and no stack silently falls back to `us-west-2` or `us-east-1`. The misleading repository-wide `agenticai/defaultRegion` and dead `agenticai/approvedRegions` defaults were removed. The shared AgentCore VPC AZ-ID map now drives both network implementations for `us-east-1`, `us-west-2`, and `eu-west-1` (`euw1-az1/az2/az3`); an unreviewed Region fails synthesis unless the caller supplies an explicit set. Five targeted suites (32 tests), mutation checks for Region inclusion/precedence/bin wiring/AZ IDs, build, lint, and strict `eu-west-1` management synth pass. This is offline onboarding only; live EMEA readiness remains gated by the independent pipeline, positive/adversarial, rollback, observability, and teardown matrix.
- Replaced the D-03 NLB/PrivateLink/LiteLLM placeholder in `@agenticai/platform-inference-gateway` with the real AgentCore inference path.
- Platform and pipeline synthesis now fail when `agenticai/inferenceModelRateLimits` is absent or malformed.
- Pipeline synthesis accepts both the parent multi-project checkout and a standalone blueprint checkout, validates stage assemblies in one complete shell command, returns nested `cdk.out` to the ShellStep artifact root, and fails closed for every other source layout.
- Root pipeline artifact stores are explicit CMK-encrypted, rotating, five-tagged, lifecycle-bounded, and automatically emptied on rollback or teardown instead of leaving retained untracked buckets.
- Cross-account pipeline bootstrap guidance requires exact `iam:PassRole` grants for target CDK deploy roles scoped to CodePipeline and target CloudFormation execution roles scoped to CloudFormation.
- Log Archive now provisions its encrypted on-demand Kinesis target and `logs.amazonaws.com` delivery role with sender/recipient `aws:SourceArn` confused-deputy conditions instead of unresolved placeholder ARNs.
- CloudWatch Logs destination access policies now list sender account IDs directly; live `PutDestinationPolicy` rejects IAM root ARNs in `Principal.AWS`.
- Consolidated Platform test deployments reuse the nonproduction `AgenticAI-GuardrailAdmin` role and suffix only the production baseline guardrail name; separate-account deployments keep the stable unsuffixed names.
- Cognito M2M token endpoints now come from `UserPoolDomain.baseUrl()`; the first pipeline-owned invocation proved that constructing a managed domain with the AWS API `urlSuffix` produces an unresolvable endpoint.
- Target-qualified inference model routes now use the exported `InferenceTargetName` rather than the connector ID; live discovery proved AgentCore prefixes model IDs with the target name while rate limits continue to use the provider-qualified ID.
- Nonproduction Log Archive buckets, stream, and CMK use destroy/auto-delete semantics; production retains the audit archive by default. Audit and Log Archive are emitted only once because Management/Governance is shared across environments.
- Management bootstrap guidance now covers the stack-scoped IAM role, managed-policy attach/detach, Lambda lifecycle, and Lambda-only `iam:PassRole` required by CDK's nonproduction S3 auto-delete provider; this boundary was proven by the first live Log Archive rollback.
- Teardown now includes `AgenticAI-Platform-InferenceGatewayStack` and requires the deployment's model-rate context.
- Gateway PolicyEngine migration passed pipeline-owned behavior parity, in-place semantic-search removal, production deployment, mode rollback, fail-closed teardown, and zero unintended residue on exact commit `f45a12c`. The Lambda wrapper remains as an intentional rollback and defense-in-depth control pending a separate maintainer retirement decision.
- Gateway PolicyEngine association now converges through the signed Provider waiter and retries only the explicit `ValidationException` whose message contains `Access denied while calling GetPolicyEngine` and `Gateway role`; every other UpdateGateway error fails immediately. The first pipeline attempt proved a blind six-minute delay was insufficient even though the exact role policy had been attached for 7m09s, IAM simulation returned `allowed`, and CloudTrail showed AgentCore assuming the new RoleId in `GenesisPolicyEngineCheck`.
- The exact-head association retry then ran 61 signed `UpdateGateway` attempts over 10m28s and failed closed at its Provider bound. CloudTrail paired every retry with `GenesisPolicyEngineCheck` `GetPolicyEngine` and `kms:Decrypt` `AccessDenied`; KMS identified the real defect as no applicable identity-based decrypt allow. The Gateway role now grants `kms:Decrypt` only on the exact PolicyEngine CMK without the FAS-oriented condition block that did not authorize the runtime call; metadata-only `kms:DescribeKey` remains exact-key and `kms:ViaService`-conditioned. The CMK key policy retains service conditions on all three statements, source and encryption-context conditions on cryptography, and operation/context constraints on grant creation. CloudTrail confirmed AgentCore's two service-created grants separately constrain operations and the exact PolicyEngine encryption context. Rollback again left no active Gateway, PolicyEngine, or key alias; the run-owned CMK entered its expected seven-day pending-deletion state.
- Exact KMS-fix commit `5b83576` then proved the repair live: Gateway-role KMS authorization returned `DryRunOperationException` (the request would succeed), `GetPolicyEngine` succeeded, signed `UpdateGateway` succeeded on its first attempt, and the `LOG_ONLY` association completed in 13.6 seconds. The next native policy failed closed because AgentCore validates each Cedar action against the Gateway's current target schema and no target existed yet. Enabled-mode creation now orders association → targets → exact READY checks → strict policies → requested mode. The readiness waiter signs the modeled trailing-slash target URI, pins each service-minted ID and expected name, and fails immediately on identity drift, terminal status, or any `*_PENDING_AUTH` state. Deletion preserves the requested mode while policies and targets are removed, waits for zero targets, then rolls to `LOG_ONLY` and detaches; this keeps `ENFORCE` default-deny and retains the Lambda wrapper as the `LOG_ONLY` backstop.
- Exact target-order commit `78ad425` passed pipeline-owned `LOG_ONLY → ENFORCE → LOG_ONLY → ENFORCE` with only the mode mutation/readiness resources changing and every Gateway, engine, policy, target, and CMK identity stable. A temporary pipeline-owned nonproduction principal mismatch then produced an empty `tools/list` and explicit `policy enforcement` denials for direct echo and ping calls, while the retained wrapper had allowed the same Admin caller in `LOG_ONLY`. That test also found the optional semantic-search tool returned both unauthorized schemas despite those denials.
- Exact semantic-search repair commit `f45a12c` omitted `searchType` in PolicyEngine-enabled modes while retaining the exact `OFF` R2 template. An in-place nonproduction `UpdateGateway` removed the live search configuration without replacing any identity. Permitted callers saw exactly echo and ping; unpermitted callers saw an empty list and direct policy denials; the built-in search action was absent with zero metadata disclosure in both environments. Production deployed through the pipeline, repeated positive and principal-mismatch twins, and restored the intended principal. Dependency-ordered teardown held `ENFORCE` through policy/target removal, crossed the zero-target barrier, rolled to `LOG_ONLY`, detached, retired both service grants per CMK, removed four alias permissions and all exact generated logs, and left zero unintended residue while retaining the tool Lambdas, Registries, DynamoDB rollback tables, and Lambda Cedar wrapper.
- Runtime/Memory image preflight rejected the default Python slim image (one High), Bookworm (four Critical/fourteen High), and Alpine (three Critical/eighteen High). Exact commit `88d5381` pins the zero-finding AWS-maintained Lambda Python 3.13 AL2023 ARM64 base digest, retains numeric non-root execution, and clears the inherited Lambda handler command. The isolated live campaign reached Memory `ACTIVE` and Runtime `READY`, passed exact invocation and short-term event round trips, deleted Runtime before Memory, retired service grants and exact logs, and left zero active residue; pipeline-owned Workstream integration remains separate.
- Pipeline Registry approval reads `GetRegistry.discoveryConfiguration.authorizerType`, matching the GA Boto3/Botocore response model; the initial top-level assumption failed closed before any submission.
- Tool governance moves to catalogue revision `2` / RegistryRecord `2.0.0`; target ARNs now point to environment-qualified, pipeline-owned aliases in the Platform pipeline Region.
- Platform and Workload root pipelines self-synthesize independently; Workload R2 resolves both Registry contexts just-in-time and validates the `RegistryRoles` assembly before publishing `cdk.out`.
- The Platform root now creates and uses the stable `AgenticAI-PlatformPipelineRole` and wires that exact ARN into Guardrail administrator trust; pipeline mode no longer requires an externally supplied role ARN that may not exist on first deployment. Existing roots migrate `Pipeline.RoleArn` in place and must repoint any external reference before CDK retires the generated role.
- GA-only production promotion now uses `ProdGatewayApproval` after live nonproduction MCP proof; app evaluation/canary/soak actions remain only in legacy/full-agent mode, where their prerequisite stacks exist.
- Platform alias permissions now consume exactly one pipeline-output Gateway service-role ARN per environment through `agenticai/gaGatewayServiceRoleArns`; account, role-name, uniqueness, and environment cardinality are validated, with no principal reconstruction from Platform tenant/agent settings.
- Pipeline-owned tool Lambdas now use explicit `AgenticAI-Platform-<environment>-<tool>-exec` roles with only scoped log-stream writes; this keeps deployment inside the existing named-role policy boundary after live phase 1 denied CDK's generated `ServiceRole-*` name.
- `RegistryReaderRole` now grants `agent-registry:ListTagsForResource` only on its exact Registry and `/record/*` family, closing the live Workload synth denial while preserving ownership-tag verification and read-only scope.
- The Workstream validator now calls the live-resolvable `agent-registry-control.<region>.api.aws` endpoint while retaining SigV4 service `agent-registry`; the modeled `.amazonaws.com` hostname failed DNS inside Lambda.
- Gateway custom resources now retain the service-returned `gatewayId` as their physical ID and use `PhysicalResourceIdReference` for update/delete, so rollback never sends a friendly synthetic identifier to `DeleteGateway`.
- Workstream Gateway teardown now inserts a Provider-backed `TargetDeleteBarrier` in the dependency chain; it polls `ListGatewayTargets` to empty after asynchronous target deletions before CloudFormation invokes `DeleteGateway`, and only `ResourceNotFoundException` is tolerated so association races fail loudly instead of orphaning a Gateway.
- Workstream bootstrap guidance now requires `iam:PassRole` on the bounded Provider waiter roles only when `iam:PassedToService=states.amazonaws.com`; the first barrier deployment failed closed before resource creation when that service condition was absent.
- Terminal GA Registry record recovery now rotates only the selected record's CloudFormation identity and SSM ID pointer; unchanged records and environments retain their existing logical IDs, and invalid, unknown, reused generation `1`, or out-of-range values fail synthesis.
- Teardown snapshots exact CodeBuild project and Lambda IDs before deletion, recovers those IDs from deleted-stack event history on reruns, verifies each resource is absent, and removes only its exact service-created default log group; discovery, absence-check, and deletion errors fail closed.
- Deploy-time Registry validation now explicitly compares the live governance target ARN with the synth-wired Gateway target in addition to requiring the exact descriptor SHA-256.
- Developer subscriptions now store stable tool IDs in `agenticai/gaRegistryExpectedToolIds`; environment-specific RegistryRecord IDs never enter developer repositories.
- SCP-09 now exempts only environment-qualified `AgenticAI-D03-*-GatewayAdmin` roles in configured Workstream accounts. The prior Platform-account exception could not create a resource in a Workstream account.
- Platform tool-alias grant retirement is now an explicit contract. Lambda stores a granted role principal as its IAM RoleId: after the 2026-09-24 teardown deleted the Workstream Gateway roles without first disabling the Platform permission phase, all four alias policies kept the deleted roles' `AROA...` principals, the recreated roles were denied, and Lambda rejected every further `AddPermission` on those aliases with `The provided principal was invalid` (a RoleId-bound replacement was attempted live and rolled back, because CloudFormation creates before it deletes). The permission phase must be switched off before `RegistryRoles` deletion and on again once the roles exist; the same two-phase toggle repairs a stale grant. The Workload `RegistryRoles` stack now exposes a diagnostic `GatewayServiceRoleId` output, the `GatewayPermissionReady` gate names the verifiable condition, and README §6.3, §16 and the runbook document the procedure and the exact error.
- The inference Gateway role's `bedrock-mantle:CreateInference` grant is now conditioned on `bedrock-mantle:Model` for the allocated models (provider-qualified id and provider-stripped alias); any model outside the allocation is denied by IAM instead of relying on the fail-open zero-rate wildcard. `bedrock-mantle:ListModels` remains unconditioned.
- README, threat model and target-state architecture no longer describe the native Gateway rate limit as a hard quota control: it is approximate, fail-open traffic shaping; per-account Bedrock quotas and SCP/IAM are the quota and abuse controls.

### Removed

- The legacy `allowedToolIds` catalogue consumer path of `D03WorkstreamGatewayStack` (maintainer decision, 2026-09-25): `gaRegistryContext` is now required, a stray `allowedToolIds` fails the synth, `bin` no longer reads `agenticai/d03AllowedToolIds`, and `scripts/teardown.sh` synthesizes the direct workstream-gateway stage from `AGENTICAI_GA_REGISTRY_CONTEXT_FILE` / `AGENTICAI_GA_REGISTRY_EXPECTED_TOOL_IDS`. The legacy-path conformance tests now run on the GA path through a shared fixture (`tests/conformance/fixtures/ga-registry-context.ts`); a before/after synthesis of five GA configurations (IAM, one tool, CUSTOM_JWT, PolicyEngine LOG_ONLY and ENFORCE) differs only in the `SubscribedToolCount` output description.

### Verification

- The isolated AgentCore Runtime and Memory campaign passed on exact commit `88d5381`: a zero-finding digest-pinned `linux/arm64` image built from the reviewed SHA, Memory reached `ACTIVE`, Runtime reached `READY`, exact `InvokeAgentRuntime` and short-term `CreateEvent`/`GetEvent` round trips passed, Runtime-before-Memory cleanup completed, service grants and exact logs were retired, the prerequisite stack deleted, and independent inventory found zero active residue. Sanitized details are in `evidence/live/2026-09-21-agentcore-runtime-memory-compatibility-spike.md`.
- The opt-in PolicyEngine path passes 26 catalogue/compiler tests, 34 Workstream Gateway tests including executable association retry, exact target readiness, and mode/detach convergence, 46 Workload pipeline tests, and 15 fail-closed teardown tests. The rendered template pins `FAIL_ON_ANY_FINDINGS`, per-policy `EnforcementMode: ACTIVE`, exact Gateway and principal resources, CMK grant constraints, six-minute IAM propagation, default `OFF` parity, target-before-policy action discovery, requested-mode preservation during policy/target deletion, post-target `LOG_ONLY` rollback, and final detach. Exact commit `f45a12c` passed the corresponding nonproduction and production pipeline/live matrix and zero-residual cleanup; sanitized details are in `evidence/live/2026-09-21-pipeline-agentcore-policyengine.md`.
- The preceding compatibility spike passed live in `us-west-2` for IAM and Cognito M2M auth, model discovery, Strands `LiteLLMModel` streaming/non-streaming, exact HTTP 429, and zero-residue cleanup.
- Platform pipeline executions passed on exact commits `f037b4e`, `2ca8272`, and `0ef7f50`: Source, Synth, SelfMutate, assets, all five nonproduction deployments, fresh explicit approvals, and all three production deployments. The production Guardrail, Gateway, and inference target are `READY`; the native rate limit is `ACTIVE`; the Management/Governance Log Archive is live; and the pipeline-owned Gateway passed Cognito M2M, 49-model discovery, and Strands `LiteLLMModel` streaming/non-streaming. Sanitized details are in `evidence/live/2026-09-19-platform-pipeline-deployment.md`.
- The isolated Gateway PolicyEngine run passed on exact commit `46c3a62`: eight strict Cedar policies, 20 subject/group decisions, four filtered tool lists, direct-call denial, exact 401/403 JWT negatives, expired-token denial, `ENFORCE → LOG_ONLY → ENFORCE`, and independent zero-residual inventory. Sanitized details are in `evidence/live/2026-09-19-policyengine-compatibility-spike.md`.
- The isolated GA Agent Registry run passed on exact commit `8e66dc3`: both native CloudFormation types were live; a custom governance document round-tripped; explicit `DRAFT → submit → APPROVED` and data-plane discovery passed; a pre-verified nonexistent parent produced deterministic `UPDATE_ROLLBACK_COMPLETE`; the approved record survived; and direct CloudFormation plus Cloud Control inventory confirmed zero residual resources. Sanitized details are in `evidence/live/2026-09-19-agent-registry-compatibility-spike.md`.
- Pipeline-owned GA Registry R1 passed on exact producer commit `f3ec7d6` and approval utility commit `39a13ab`: both Platform environments deployed through the reviewed pipeline, all four records were observed in `DRAFT`, explicitly submitted, independently read back as descriptor-identical `APPROVED` records, and exactly discovered. Wrong-principal and wrong-account trust twins were denied; the legacy DynamoDB path stayed active. Sanitized details are in `evidence/live/2026-09-20-pipeline-ga-agent-registry-r1.md`.
- Pipeline-owned R2 passed on exact deployed commit `3870e0e`: stable tool IDs resolved cross-account into exact approved records, the role/permission handoff completed, nonproduction and production Tool Gateways and targets reached `READY`, MCP positives and denial twins passed, no-op redeployment preserved identity, status drift failed closed, terminal-record generation recovery succeeded, and both environments deleted in target → barrier → Gateway order. Teardown-hardening commit `7774299` removed 39 exact empty service-created log groups recovered from deleted-stack history; independent inventory found zero unintended residue. Sanitized details are in `evidence/live/2026-09-21-pipeline-ga-agent-registry-r2.md`.
- Load, rate-limit, concurrency and soak campaign on the redeployed `d2186b9` revision (`evidence/live/2026-09-24-load-rate-limit.md`): the zero-rate wildcard returned exact HTTP 429; the per-model 10 RPM / 10 000 TPM entries admitted 14 back-to-back requests, 15-23 requests and 22 044-40 870 input tokens per minute (Bedrock Mantle `Inferences`/`TotalInputTokens` corroborate), so the limiter is recorded as approximate traffic shaping; 4- and 8-way concurrent governed sessions passed; a 30-minute soak passed 48 of 48 sessions at 40-second intervals (p50 7.95 s, p95 10.03 s, max 10.72 s) with the Runtime `READY`.
- Chaos and dependency-failure campaign (`evidence/live/2026-09-24-chaos-dependency-failure.md`): no, malformed and forged bearers returned 401 with no model content; empty, non-JSON, wrong-shape, control-character and 900 KB Runtime payloads returned controlled 4xx or the fixed default run with the Runtime `READY` and a positive re-proof; the same campaign found that the Gateway inference path returned HTTP 200 for a bogus or absent `guardrail_identifier`, which is the defect the interceptor above closes.
- Reversible schema discovery against the live nonproduction inference Gateway (temporary unattached PolicyEngine, deleted afterwards) established why AgentCore Policy cannot carry this control today: the inference action is `<target>___POST:/v1/chat/completions`, `context.input.messages` is `Set<record>` while the `BedrockGuardrails` providers accept only `string` arguments, the Dogwood parser has no set traversal, and the authoring service reports guardrail requests as "cannot be expressed in Dogwood".
- Server-side guardrail enforcement passed live in both environments on Platform commit `e72c19c` and agent commit `2d1340f` (`evidence/live/2026-09-24-guardrail-enforcement.md`): through the pipeline-owned inference Gateway, prompt-attack, denied-credential-topic and blocked-PII prompts — each sent with and without the client `guardrail_identifier` — returned exact HTTP 403 `guardrail_intervened` with the tripped policy types and no model content (0.48–1.33 s), while the benign control, the benign absent-parameter and bogus-parameter requests and a two-turn tool-protocol exchange returned 200; the generated agent's positive session passed all eight checks and the unsubscribed-tool twin was refused with the interceptor in the inference path; the production interceptor ran the same `CodeSha256` as the proven nonproduction function and its own decision log for the probe window held exactly six `blocked` and six `allowed` decisions, the governed session's follow-up call logged as two separately scored turns. Two earlier interceptor revisions were rejected at `SecurityReview` (system-prompt scoring; joint scoring of a request and a tool result), and the pre-interceptor run of the same probe on `d2186b9` is the recorded failing twin.
- Gateway OTEL rate-limit span correlation passed on the pipeline-owned nonproduction inference Gateway (`evidence/live/2026-09-25-gateway-otel-span-correlation.md`), superseding the 2026-09-18 blocker: with Transaction Search enabled and `TRACES` + `APPLICATION_LOGS` deliveries on the Gateway, 3 × 200 and 3 × 429 (zero-rate `*` entry) produced exactly six `AgentCore.Gateway.InvokeHttp` decision spans in `aws/spans`, each matched one-to-one — throttled spans by their own `aws.request.id` (limit key `models-nonprod`, entry `*`, metric `requests`), allowed spans through the application log's `request_id` → `trace_id` because allowed spans carry no request id; a `TRACES` → `XRAY` delivery is refused without Transaction Search; every account setting was restored and verified.
- Upgrade and interrupted-deployment campaign (`evidence/live/2026-09-25-upgrade-interrupted-deploy.md`): the Platform no-op run and the Workload no-op run on an unchanged head reported `no changes` for every CloudFormation action, kept every stack's update time, and left the Workload identity snapshot (Runtime versions, image digest, Gateway targets) byte-identical, with 48/48 and 24/24 sessions on one version; sampled upgrades to 1.2.0 switched exactly once in both environments; the nonproduction 1.2.1 deployment was cancelled before (45/45 sessions) and during (16/16) the Runtime update — the late cancel rolled back by creating a new Runtime version with the prior digest — and `retry-stage-execution --retry-mode ALL_ACTIONS` then deployed 1.2.1 with 17/17 sessions in each environment. The first sampled upgrade surfaced the generated-agent defect listed under Fixed.

## [1.0.0] - 2026-08-18

First public release, published as `enterprise-agentic-ai-platform-blueprint` in [`aws-samples/sample-ai-agent-factory`](https://github.com/aws-samples/sample-ai-agent-factory). Consolidates all development phases below (A-Q). The `v0.x` labels retained in the phase headings are the internal development milestones that produced each change set, kept for traceability.

### Phase Q (2026-05-21) — per-developer tool entitlement

Closes the v0.5.0 gap that every authenticated developer in a workstream could invoke every subscribed tool. Adds a second-layer Cedar entitlement check pinned to the developer's Cognito group claim.

**Deliverables:**

- `ToolSpec.allowedGroups?: readonly string[]` (`packages/platform-tool-catalogue/src/tool-catalogue.ts`). `validateToolSpec` enforces non-empty arrays + `COGNITO_GROUP_REGEX = /^[A-Za-z0-9_+=,.@-]{1,128}$/`.
- `composeCedarPolicyDocument` now emits `permit(principal in CognitoGroup::"<g>", action == Action::"InvokeTool", resource == Tool::"<id>");` per group when `allowedGroups` is set; back-compat retains the unconditional permit when absent.
- New package `@agenticai/tool-cedar-wrapper` — `evaluateCedar`, `extractPrincipalGroupsFromEvent`, `withCedarEnforcement`, `CedarDeniedError`. Mini Cedar evaluator scoped to the catalogue grammar; fail-closed on missing inputs. Reads JWT claims from the three documented AgentCore event shapes (preview / GA / test).
- `D03WorkstreamGatewayStack` (legacy catalogue path) throws at synth when any subscribed tool declares `allowedGroups` but `cognitoDiscoveryUrl` is missing — error names the offending tool ids so the developer can fix the subscription.
- `D03WorkstreamGatewayStack` (Registry path) injects `GATEWAY_AUTHORIZER_MODE` into the validator Lambda; deploy fails with an actionable error if the operator left CUSTOM_JWT off while the record carries `metadata.allowedGroups`.
- Validator Lambda extracts `metadata.allowedGroups` from the Registry record and re-composes the per-record Cedar snippet into principal-bound permits before pinning into `Data.cedarPolicy`.
- **Inline Cedar gate in `D03PlatformCoreStack` demo tool Lambdas** — `apps/platform-account/lib/d03-platform-core-stack.ts` prefixes each `Code.fromInline` handler with a verbatim-equivalent gate (same regex grammar, same three-shape claim probe, same fail-closed defaults as the wrapper package). Inline Lambdas can't `require('@agenticai/tool-cedar-wrapper')`, so the wrapper package serves as the tested reference implementation that the inline copy mirrors. New env vars per Lambda: `AGENTICAI_TOOL_ID` (catalogue id) and `AGENTICAI_CEDAR_POLICY_DOCUMENT` (composed Cedar bundle). The default bundle is the v0.5.0 unconditional permit; the live tester rotates the env via `aws lambda update-function-configuration` to drive Q3/Q4 entitlement scenarios without redeploying the stack.

**Tests:**

- `packages/tool-cedar-wrapper/src/index.test.ts` — 20 cases covering allow / deny / fail-closed / event-shape variants / wrap-time validation / env-var fallback.
- `tests/conformance/phase-q-cedar-entitlement.test.ts` — 17 cases pinning composer shape, no-wildcards, legacy-path synth throw, error-message contents, `PerTenantCedarPolicy` output, evaluator integration with the live composer, and the inlined Cedar gate inside the demo tool Lambdas (extract / evaluate / deny path, env-var seeding from the catalogue, behaviour-parity sandbox check that eval-s the synthesised inline code with a principal-bound bundle and asserts both allow + deny).
- `packages/platform-tool-catalogue/src/tool-catalogue.test.ts` extended with 8 cases for `allowedGroups` validation + composer behaviour.
- Suite total: **490 passed, 490 total** (was 453 at end of v0.5.0).

**Documented deviation (historical):** `TODO-GW-POLICY-ENGINE` (README §3.3) originally kept Cedar evaluation in each tool Lambda until the AgentCore Gateway PolicyEngine path was available and proven. The pipeline-owned migration later passed on exact commit `f45a12c`; the wrapper remains intentionally active pending a separate maintainer retirement decision.

### Phase L–O (2026-05-17 → 2026-05-19) — AWS-native Registry + developer-account access (v0.5.0)

Replaces the v0.4.0 TS-constant tool catalogue with the **AWS Bedrock AgentCore Registry** as the run-time source of truth, opens up the workstream developer journey (Identity Center permission sets + scoped SCPs + a TypeScript developer CLI), and rewrites the README around the day-in-the-life flow. The v0.4.0 catalogue path is kept intact for back-compat — Registry mode is opt-in via `agenticai/d03EnableAgentRegistry`.

**Phase L — `@agenticai/agent-registry` (AWS Bedrock AgentCore Registry):**

- `PlatformRegistryConstruct` — provisions a single platform-account Registry via `bedrock-agentcore:CreateRegistry` (Lambda-backed `CustomResource`); inbound auth `AWS_IAM`; manual approval workflow.
- `RegistryRecordConstruct` — one record per `PLATFORM_TOOL_CATALOGUE` tool. Stores `gatewayTargetArn`, `cedarPolicy`, `ownerTeam`, `costCentre`, `inputSchema` under `descriptors.mcp.server.inlineContent.agenticai.*`. `${PLATFORM_ACCOUNT_ID}` substituted with the live account id at seed time. Records start in `DRAFT`; `registryAutoApproveOnSeed: true` flips them to `APPROVED` for nonprod loops.
- `RegistryConsumerGrant` — attaches `bedrock-agentcore:GetRegistryRecord / ListRegistryRecords / SearchRegistryRecords / InvokeRegistryMcp` (resource-scoped to the registry ARN + record-arn pattern) to a workstream runtime role.
- `D03WorkstreamGatewayStack` Registry path — replaces `resolveSubscribedTools` with a Lambda-backed `Custom::AgenticAIRegistryRecordValidator` per subscribed record. Validator runs at deploy time (cross-account `AssumeRole` with externalId), requires `status == APPROVED`, parses metadata from three documented Registry shapes (`descriptors.mcp.server.inlineContent` / `descriptors.mcp.server.metadata` / `descriptors[0].metadata`), and pins the resolved `gatewayTargetArn` + `cedarPolicy` as CFN `Data.*` attribute tokens. Mutually exclusive with `allowedToolIds`; synth throws on conflict or empty subscription.
- `D03PlatformCoreStack` adds `enableAgentRegistry` / `registryName` / `registryAutoApproveOnSeed` props; emits a single `PlatformRegistryConstruct` and one `RegistryRecordConstruct` per catalogue tool when enabled. Back-compat: opt-in flag default off.
- `bin/agentic-ai-platform.ts` plumbs `agenticai/d03EnableAgentRegistry`, `agenticai/d03RegistryName`, `agenticai/d03RegistryAutoApproveOnSeed`, `agenticai/subscribedRegistryRecords`, `agenticai/d03RegistryId`, `agenticai/d03RegistryReaderRoleArn`, `agenticai/d03RegistryReaderExternalId`.

**Phase M — `@agenticai/developer-access` (Identity Center + scoped SCPs):**

- `WorkstreamPermissionSets` — three Identity Center permission sets per workstream (`AgenticAI-WS-Dev-<tenant>`, `-RO-<tenant>`, `-Approver-<tenant>`) with managed-policy + inline scoping. Developer can deploy via the workload pipeline + read CW/X-Ray/DDB; cannot mutate Gateway / Registry / SCPs / inference profiles. Approver gets `codepipeline:PutApprovalResult` on the workload pipeline only. ReadOnly is CW + X-Ray + DDB read.
- `agenticai-workstream-roster-<env>` DDB — permission-set ARN → workstream → developer list (audit pivot for QuickSight + investigations).
- `RegistryConsumerGrant` ties developer permission sets back to Phase L so the workstream runtime role can `Search / GetRegistryRecord / InvokeRegistryMcp`.
- **SCP-11** (Registry mutation lockdown) — denies `bedrock-agentcore:CreateRegistry / DeleteRegistry / UpdateRegistryRecordStatus / DeleteRegistryRecord / SubmitRegistryRecordForApproval / UpdateRegistryRecord` from any principal except `AgenticAI-RegistryAdmin` in the platform account. Mirrors SCP-09's shape.
- **SCP-12** (developer permission-set / platform-tag deny) — denies any `aws:PrincipalTag/agenticai:permission-set` matching the `AgenticAI-WS-Dev-*` prefix from mutating any resource tagged `agenticai:owner=platform`. Belt-and-braces under SCP-09 / SCP-11.
- `buildScpSet` extended with `enableRegistryLockdown` / `enableDeveloperPlatformTagDeny` / `registryAdminRoleName` / `developerPermissionSetPrefix` knobs.

**Phase N — `@agenticai/developer-cli` (Node CLI for the workstream developer journey):**

- `agenticai init <tenantId> <agentId>` — scaffolds a workstream agent repo from `blueprints/agenticai-task-agent/` (default) or `--from chatbot|langgraph|crewai|multi-agent`. Produces `agent.py`, `tools/`, `prompts/system.md`, `eval/cases.jsonl`, `bedrock.config.yaml`, `cdk.context.json` pinned to tenant/agent ids.
- `agenticai registry search <query>` — wraps `bedrock-agentcore:SearchRegistryRecords` for tool discovery.
- `agenticai registry subscribe <recordId>` — appends to `cdk.context.json:agenticai/subscribedRegistryRecords`.
- `agenticai dev run` — local agent harness against a non-prod Registry + stubbed tool runner.
- `agenticai dev eval` — runs the local 7-category evaluation gate against `eval/cases.jsonl`. Score table format identical to the CI `evaluation_gate.py` output so devs can pre-flight before push.
- `agenticai submit` — opens a PR with a templated description (gate thresholds, diff against last green eval, registry records subscribed).
- Uses the standard AWS SDK credential chain (Identity Center session creds inherited).

**Phase O — README.md rewrite:**

- §1.1 day-in-the-life — adds a step-by-step walkthrough of the workstream developer journey (Identity Center login → registry search → subscribe → CLI scaffold → eval → submit → canary → prod).
- §2.4 architecture — adds Registry-MCP search path explanation; per-developer tool entitlement bullet.
- §3.3 D-03 residual-risks — adds the entitlement / `TODO-GW-POLICY-ENGINE` row.
- §13 onboarding — replaces manual `cdk deploy` walkthrough with the CLI walkthrough.
- §16 disclaimers — drops "no developer login" line.

**Tests:**

- `tests/conformance/phase-l-d03-platform-registry.test.ts` — 11 cases (back-compat default-off, opt-in registry seed, `Custom::BedrockAgentCoreRegistryRecord` per catalogue tool, manual vs auto-approval branches).
- `tests/conformance/phase-m-developer-access.test.ts` — 14 cases (3-set emission per workstream, IAM scoping, `agenticai:owner=platform` deny, roster DDB, RegistryConsumerGrant integration).
- `packages/organizations/src/scps/scps.test.ts` — extended with SCP-11 + SCP-12 condition-shape pins (RegistryAdmin role allow-list; permission-set tag deny).
- `tests/conformance/phase-10-d03-workstream-gateway.test.ts` — extended with 6 v0.5.0 Registry-path cases (validator emission, gateway+target counts, `SubscribedToolCount` output, mutual-exclusion synth throws).
- Suite total at end of v0.5.0: **453 passed, 453 total**.

**Live verification (us-east-1, Platform + Workload accounts, 2026-05-17 → 2026-05-19):**

- L1 — Registry exists (`list-registries` returns the platform registry id).
- L2 — Records published & approved (2 demo tools present).
- L3 — Workstream synth fails on deprecated record (synth non-zero exit, actionable msg).
- L4 — Search via MCP from workstream (`tools/list` returns the 2 tools).
- M1 — Developer permission set in Identity Center (`list-permission-sets` returns 3 per workstream).
- M2 — Dev cannot mutate Gateway (Developer role assume → `delete-gateway` → AccessDenied via SCP-09).
- M3 — Dev cannot mutate Registry (Developer role assume → `update-registry-record-status` → AccessDenied via SCP-11).
- N1 — CLI scaffold (`agenticai init t1 a1` produces a deployable repo; `npm test` green).
- N2 — CLI eval matches CI (5-category scores identical to `evaluation_gate.py`).

### Z7 + audit-fix pass (2026-05-15) — close the misses surfaced by self-audit

After the v0.4.0 ship I ran a rigorous self-audit (twice) and found I had been shipping module _definitions_ without the _wirings_. Z7 closes those misses; a second self-audit found another batch which this entry also closes.

**Z7 deliverables:**

- `Z7-A` — `InferenceCircuitBreakerConstruct` (was a data-only module). Real Lambda invokes Bedrock under SSOT retry+fallback policy; emits `AgenticAI/InferenceCB/{CircuitOpenCount,FallbackInvocations,RetryStorm}`; composite alarm to SNS. Verified live.
- `Z7-B` — Pipeline canary stage. `CanaryDeploy` + `CanarySoak` CodeBuild actions added to `WorkloadPipelineStack` between `EvaluationGate` and `ProdApproval`. `RollbackStateMachineConstruct.wireToRegressionAlarm()` auto-fires the rollback SF when the OnlineEval `Regressed` composite alarm transitions to ALARM.
- `Z7-C` — README §12.3 EU AI Act Article-by-Article mapping. `ADR-0006` through `ADR-0012`.
- `Z7-D` — HITL integrated into `agenticai-task-agent` (confidence-threshold + iteration-threshold escalation) and `agenticai-multi-agent` (sensitive-worker-suffix routing).
- `Z7-E` + `Z7-K` — registry `by-card-name` + `by-domain` GSIs; tool catalogue `toolType: 'lambda' | 'agent-a2a'`; synth-time validation of `a2aEndpointUrl`.
- `Z7-F` — `ShowbackConstruct` (QuickSight DataSet over CUR Athena) + per-tenant spend widget.
- `Z7-G` — Real LangGraph `langgraph_real.py` (StateGraph + lazy-import) and CrewAI `crewai_real.py` (Crew + Agent + Task). `requirements.txt` declared. Tests use `pytest.importorskip`.
- `Z7-H` — 6 pytest integration suites under `tests/integration/`: evaluation_gate, online_evaluation, kill_switch, hitl, chargeback, multi_framework.
- `Z7-I` — `evaluation.config.yaml` (promptfoo-compatible) + `golden_corpus.jsonl` (8 reference cases) under `blueprints/agenticai-chatbot-agent/eval/`.
- `Z7-J` — `buildSharedMemoryNamespacePath()` exported from `@agenticai/agentcore-memory`.
- `Z7-L` — 4 bonus packages: `pii-redaction`, `tenant-quota-guard`, `otel-genai-semconv`, `catalogue-drift-detector`.

**Audit-fix pass — second-self-audit gaps:**

- `G-1` — `scripts/evaluation_gate.py` now actually loads the corpus, calls Bedrock Converse, scores from real responses (`load_corpus` + `score_corpus`). Previously stub.
- `G-2` — Online-eval watchdog scoring rewired. Real `ConverseCommand` invocation + numeric verdict parsing.
- `G-3` — HITL Cedar approver policy now lands in **AWS Verified Permissions** (`CfnPolicyStore` + `CfnPolicy`).
- `G-4` — `ShowbackConstruct` instantiated in `GapClosureStack`.
- `G-5a` — `TenantQuotaTableConstruct` instantiated; circuit breaker enforces token-bucket atomically.
- `G-5b` — Online-eval Lambda PII-redacts the DDB sample-preview.
- `G-5c` — Circuit breaker Lambda emits OTel GenAI sem-conv span attributes.
- `G-5d` — `CatalogueDriftDetectorConstruct` (CDK + scheduled Lambda) added.
- `G-6` — `buildSharedMemoryNamespacePath` test coverage (6 new cases).
- `G-7..G-10` — `phase-21-z7-fixes.test.ts` (15 new tests) pinning every Z7 + audit-fix CFN shape.
- `G-11` — this entry.
- `G-12` — chatbot vs task/multi-agent HITL convention divergence documented.
- `G-14` — LangGraph `llm_node` lazy-imports `langchain_aws.ChatBedrockConverse` and invokes Bedrock for real.

### v0.4.0 — Gap closure: every Partial + Missing item from BLUEPRINT_GAP_ANALYSIS (2).md (2026-05-15)

Closes the gap analysis dated 2026-05-06. Builds 8 new construct packages, 2 new framework blueprints, a unified Phase J live-deploy stack, and a boto3 verification harness. **303/303 Jest tests** (170 → 303), 6 new pytest suites green at unit level. Live-verified end-to-end on real AWS (workload account, us-east-1) with a 17/17 verification matrix and three Step Functions exercised live (kill-switch, HITL high-confidence pass-through, HITL low-confidence WAIT_FOR_TASK_TOKEN).

**New packages (`packages/`):**

- `evaluation-gates/` — Phase A. Closes Partial-1. S3 Object Lock GOVERNANCE 90-day corpus bucket (KMS, versioned, TLS-only), DDB run-history table (CMK + PITR + by-status GSI), runner role with `bedrock:InvokeModel` scoped to the SSOT allow-list. 7-category scoring SSOT (5 legacy + new `refusalRateMinPct` 99% and `costPerPromptMaxUsd` $0.05). `buildAgentManifest()` producing deterministic SHA-256 over prompts + tool-permissions + config + thresholds. Workload pipeline + `scripts/evaluation_gate.py` extended to all 7 categories.
- `online-evaluation/` — Phase B. Closes Missing-1. Watchdog Lambda samples CW Logs Insights, scores via locked judge models (Sonnet correctness / Haiku refusal/toxicity), writes CMK-encrypted DDB with TTL, emits `AgenticAI/OnlineEval` metrics, composite alarm with 5 child alarms wired to the SNS failures topic. `cloudwatch:PutMetricData` namespace-conditioned. `bedrock:InvokeModel` SSOT-scoped.
- `eu-ai-act-compliance/` — Phase C. Closes Missing-2. Risk classification SSOT (chatbot=`limited`, task=`limited`, multi-agent=`high`). Three Markdown generators (technical-documentation, risk-assessment, human-oversight-protocol) auto-uploaded post-deploy via `AwsCustomResource` to an Object Lock COMPLIANCE 7-year record-keeping bucket.
- `agent-lifecycle/` — Phase D. Closes Missing-3. Agent-version DDB with `by-alias` + `by-status` GSIs. Rollback Step Functions STANDARD machine (X-Ray + LogLevel ALL + 15min timeout): EnsureSoakWindow → ReadPreviousAlias → FlipAliasToPrevious → WriteAuditRecord → NotifyOps. Default canary 5% / 30 min / auto-promote / 10pp tolerance.
- `agent-protocols/` — Phase E. Closes Partial-2 + Partial-3. A2A v0.2 Agent Card schema with synth-time validation. MCP-native helpers — protocol version locked to `2025-06-18`, qualified-name pattern `target-<name>___<tool-id>` enforced. `McpProbeConstruct` Lambda runs every 5 min, asserts qualified names, emits `AgenticAI/MCP/MCPProbeSuccess` with breaching-on-missing alarm.
- `agent-resilience/` — Phase F. Closes Partial-4 + Partial-5. Kill-switch Step Functions STANDARD machine: 4 parallel revoke branches (Cognito client lockdown via supported SDK integration; AgentCore Identity / Gateway target / inference profile recorded as `Pass` states pending SF SDK coverage of those services), audit DDB write, SNS publish. SSM Run Document trigger. Circuit-breaker SSOT: 3 attempts × exp backoff (200/800/3200 ms), retry only 429/502/503, fallback chain Sonnet 4.5 → Haiku 4.5 (both pinned to platform allow-list).
- `hitl/` — Phase H. Closes Missing-5. Cedar policy generator permitting only the configured approver IAM role to call `ResumeTaskWithApproval`. KMS-encrypted SQS escalation queue + DLQ, CMK-encrypted DDB pause-token table with TTL, KMS-encrypted approver SNS topic. Step Functions STANDARD machine (X-Ray + LogLevel ALL + 24h timeout) with `WaitForTaskToken` integration pattern and confidence-threshold check (default 0.7).
- `federation/` — Phase I. Closes Missing-6 + Missing-7 + Missing-9. Domain-scoped registry SK + SCP condition helpers; shared-memory namespace builder + 3 conflict-resolution policies (`last-write-wins`, `merge-array`, `human-arbitrate-via-hitl`); multi-framework adapter for Strands / LangGraph / CrewAI emitting qualified MCP tool names.

**Extended packages:**

- `cost-allocation/` — Phase G. Closes Missing-4. New `ChargebackConstruct`: KMS-encrypted Object Lock GOVERNANCE 24-month bucket, CMK-encrypted runs DDB + PITR, Node 20 runner Lambda (Athena → S3 → SES), monthly cron at 02:00 UTC.

**New blueprints (`blueprints/`):**

- `agenticai-langgraph-agent/` and `agenticai-crewai-agent/` — Phase I. Closes Missing-7. Each ships `bedrock.config.yaml`, agent module, conftest, and pytest 4/4 unit tests. Both invoke the workstream gateway via MCP with the locked protocol version, enforce guardrail-required, and resolve tools from the platform tool catalogue.

**New stage:**

- `bin/agentic-ai-platform.ts` — `gap-closure` stage instantiates `apps/workload-account/lib/gap-closure-stack.ts`, a unified deployable stack composing every v0.4.0 construct + a stub Cognito UserPool. Used by Phase J end-to-end live verification.

**New tests:**

- 9 conformance suites (`tests/conformance/phase-11..19-*.test.ts`) — 47 new conformance tests pinning every CFN shape (Object Lock retention modes, KMS, IAM least-priv, SF definition fragments, Cedar policy invariants, GSI shapes, SSM document name).
- 8 pure-function unit-test suites — 86 new unit tests across scoring SSOT, agent-manifest determinism, regression detector, risk classification, Markdown templates, canary config, agent-card schema, MCP helpers, multi-fw adapter, federation helpers, circuit-breaker.
- `scripts/test_evaluation_gate.py` extended to 7 categories.
- `scripts/gap_closure_live_verify.py` — boto3 harness for Phase J.

**Live AWS verification (2026-05-15):**

- Deployed `AgenticAI-GapClosureStack` to workload account in `us-east-1` (CDK bootstrap upgraded from v28 → v30).
- 6 real bugs surfaced and fixed during live deploy: (1) IAM role `description` rejects em-dashes; (2) S3 deny-non-TLS bucket policy needs `AnyPrincipal()` not `ServicePrincipal('*')`; (3) Step Functions optimised SDK does not yet recognise `bedrockagentcore:*` or `bedrock:UpdateApplicationInferenceProfile` — replaced with `Pass` states recording the action; (4) online-eval watchdog crashed on missing source log group — added `ResourceNotFoundException` catch-and-continue; (5) CMK-encrypted DDB + RETAIN survives stack rollback ⇒ name collisions on retry — added `bucketSuffix` context for AI-Act bucket; (6) Step Functions `'key.$': '$$.Ref'` substitutes bare strings; DDB needs AttributeValue dict (`{ key: { 'S.$': '...' } }`) — fixed in HITL, KillSwitch and Rollback.
- 17/17 verification matrix PASS. Three Step Functions executed end-to-end live (KillSwitch SUCCEEDED + audit DDB row; HITL high-confidence SUCCEEDED bypass; HITL low-confidence RUNNING with pause-token recorded).
- Stack destroyed; all non-COMPLIANCE-locked residuals cleaned up; the 7-year COMPLIANCE-locked AI Act buckets remain by design.

**Decisions locked for this release:**

- Phase A judge models: Sonnet 4.5 for correctness, Haiku 4.5 for refusal + toxicity (both already on the platform allow-list — no SCP exception).
- Phase D canary default: 5% / 30 min / auto-promote / 10pp regression tolerance.
- Phase J region: `us-east-1` (matches the 2026-05-05 D-03 v3 live-verified region).
- EU AI Act risk classification defaults: chatbot=`limited`, task=`limited`, multi-agent=`high`.

**Known limitations / honest disclaimers:**

- AgentCore-side branches of the kill-switch (DeleteWorkloadIdentity, UpdateGatewayTarget, UpdateApplicationInferenceProfile) record their action as `Pass` states; the Cognito client lockdown branch executes via the supported SDK integration. Operators wire a downstream Lambda to consume the Pass output until Step Functions SDK coverage lands. SCP-09/SCP-10 + the platform-tool-catalogue SSOT continue to enforce the same outcome at the Org level.
- The MCP probe currently targets a placeholder gateway URL (`https://gateway.example.com/a2a`) in the gap-closure stack; the success metric is emitted but the probe itself returns 0 against the placeholder. Once a real workstream gateway is wired (D-03 v3 pattern) the probe asserts qualified names and turns green.
- Multi-framework blueprints (`agenticai-langgraph-agent/`, `agenticai-crewai-agent/`) ship as Strands-shaped scaffolds with the same invariants. They have not been live-tested against a real LangGraph or CrewAI runtime — only their construction-time invariants (guardrail-required, MCP-version-locked, qualified-name-bound) are verified.

### Regions — APAC removed, `us-west-2` is the new reference-deployment default (2026-05-05)

- `PLATFORM_APPROVED_REGIONS` reduced to `['us-west-2', 'us-east-1']` — APAC entries removed from the approved list. Workloads deploying to APAC will be blocked by SCP-06 until re-added.
- `SYSTEM_INFERENCE_PROFILE_PREFIXES` narrowed to `['us', 'eu', 'global']` — the `apac` prefix is removed. `INFERENCE_PROFILE_BACKING_REGIONS` drops APAC regions everywhere, including from the `global` profile's backing-region list.
- Reference deployment renamed: `examples/reference-deployment-ap-southeast-2/` → `examples/reference-deployment-us-west-2/`. `cdk.context.json` updated accordingly.
- All stack defaults, bin-level `CDK_DEFAULT_REGION` fallbacks, bootstrap scripts, smoke tests, blueprint tests, and conformance tests now default to `us-west-2`.
- README choice-architecture + WA sustainability + cost-baseline pricing commentary updated to `us-west-2`.
- Customers needing APAC: re-add `ap-southeast-2` (or similar) to both `approved-regions.ts` AND the `apac` key in `allowed-models.ts` `INFERENCE_PROFILE_BACKING_REGIONS`, and extend `SYSTEM_INFERENCE_PROFILE_PREFIXES` to include `'apac'`. Conformance tests will auto-cover the new region via the SSOT flow.

### D-03 v3 — per-workstream AgentCore Gateway in workstream account, platform-governed (2026-05-05)

Restructures the D-03 Gateway topology:

- **Gateway lives IN the workstream account** (solves AgentCore Gateway service-limit ceiling + removes cross-account Runtime→Gateway hop)
- **Platform-governed** — target list, Cedar policies, mutation actions all controlled by the platform via SCP-09 + catalogue SSOT + platform-owned CFN stack
- **Tool Catalogue SSOT** — `packages/platform-tool-catalogue/` is the authoritative enterprise catalogue; workstream subscription via `D03TenantAllocation.allowedToolIds`; synth-time error on unknown id
- **SCP-09** — denies `bedrock-agentcore:Create/Update/Delete/Tag*` on Gateway resources from any principal except `AgenticAI-D03-GatewayAdmin` (mirrors SCP-05 pattern)
- **SCP-10** — denies `lambda:InvokeFunction` from runtime-role principals to any Lambda ARN not in the catalogue's resolved target list (defence-in-depth at the invocation layer)
- **`AgenticAI-D03-GatewayAdmin` role** in the platform account — the only principal SCP-09 allows to mutate Gateways across workstream accounts
- **Gateway service role** in the workstream account — runs `lambda:InvokeFunction` on only the resolved tool ARNs (exact-ARN allow-list, no wildcards)
- **Demo tool Lambdas** `agenticai-d03-tool-echo` + `agenticai-d03-tool-ping` — pinned to Lambda alias `PROD` per Q5
- **AZ-ID filter** in `D03WorkloadAgentStack` — resolves AgentCore-compatible subnets at deploy time (addresses `use1-az6` rejection from earlier live run)
- **Runtime role** identity policy: `InvokeOwnWorkstreamGateway` scoped to its specific Gateway ARN pattern + explicit DENY on all Gateway-mutation actions

Three-layer governance model:

1. **Synth-time** — `resolveSubscribedTools(allowedToolIds)` throws on unknown / deprecated id
2. **Deploy-time** — only the platform pipeline can mutate Gateways (SCP-09 + platform-owned `D03WorkstreamGatewayStack`)
3. **Runtime** — Gateway service role's exact-ARN Lambda invoke allow-list + Gateway resource policy scoped to one runtime role

**170 tests passing** (128 prior + 12 catalogue + 13 SCP + 12 phase-10 workstream gateway + 5 bypass regression). Synth clean.

#### Live AWS verification (2026-05-05) — partial

Deployed `D03PlatformCoreStack` + `D03WorkloadAgentStack` live in 2 real accounts (platform + workload, us-east-1). Both reached `CREATE_COMPLETE`:

- Platform: `AgenticAI-D03-GatewayAdmin` role + `tool-echo` / `tool-ping` Lambdas (PROD aliases) + inference profile + guardrail + caller role + invocation logging → all live
- Workload: runtime role with `InvokeOwnWorkstreamGateway` + `DenyGatewayMutation` + AZ-ID filter emitting compatible subnet → live

`D03WorkstreamGatewayStack` deploy hit 6 AgentCore-API contract edges surfaced only at live-deploy time (documented in CHANGELOG for posterity; blueprint code fixed all 6):

1. **Lambda `addPermission` Principal wildcard rejected** — `[\w+=,.@-]*` validation. Fix: account-root principal + `sourceAccount` condition. Real scoping moves to SCP-10 + GatewayServiceRole exact-ARN allow-list.
2. **AZ-ID filter custom resource asked for N response fields**, failed when only 1 subnet matched. Fix: emit single `Subnets.0.SubnetId` output (exposes the compatibility, operator enumerates full list via `ec2 describe-subnets` at runtime).
3. **MCP version `2024-11-05` obsolete** — AgentCore now accepts `[2025-11-25, 2025-03-26, 2025-06-18]`. Fix: pinned to `2025-06-18`.
4. **`AwsCustomResource` policy missing `SynchronizeGatewayTargets`** — AgentCore Gateway mutation paths use this for idempotency. Fix: added to both the Gateway and Target custom-resource policies.
5. **Gateway creation requires `CreateWorkloadIdentity`** — AgentCore provisions a Workload Identity in the AgentCore Identity Directory as part of Gateway creation (internal dependency). Fix: added `CreateWorkloadIdentity` / `GetWorkloadIdentity` / `DeleteWorkloadIdentity` / `ListWorkloadIdentities` to the custom-resource policy.
6. **Gateway Target `targetId` is a 10-char AWS-minted id**, not a friendly physical id. Fix: `PhysicalResourceId.fromResponse('targetId')` + `PhysicalResourceIdReference()` for onDelete/onUpdate lookups.

#### D-03 v3 Full end-to-end live verification (2026-05-05 follow-up)

After the initial 6-bug batch, a second live pass continued until every bug was found and fixed end-to-end. **4 more bugs discovered + fixed** (total 10 AgentCore Gateway bugs caught and resolved by the blueprint):

7. **Cross-account Lambda invoke auth rejected by AgentCore's `CreateGatewayTarget` validator.** AgentCore's up-front permission check requires the Lambda resource policy to name the **exact Gateway service role ARN** as `Principal`; the normal `AccountPrincipal + sourceAccount` cross-account pattern is rejected even though runtime IAM would accept it. Proved both in-account (Workaround 1) and cross-account (Workaround 2) work when the principal is the exact role ARN. Fix: `ArnPrincipal(gwRoleArn)` per allocation in `D03PlatformCoreStack`.
8. **Narrow per-action policy fails `CreateGatewayTarget` at IAM evaluation** even though every action name is listed. Fix: broadened custom-resource Lambda policy to `bedrock-agentcore:*` (scoped by SCP-09 at org level + Lambda lifetime + SEC-028 suppression).
9. **`ROLLBACK_FAILED` cascade on target-create failure.** CloudFormation invokes onDelete with the original physical id (logical name) instead of the API-returned 10-char targetId. Fix: `ignoreErrorCodesMatching: '(ResourceNotFoundException|ValidationException)'` on all onDelete paths.
10. **Role-id drift breaks Lambda resource policy across role recreation.** When the Gateway service role is created, rolled-back, and recreated, its IAM `RoleId` changes even though the ARN string is stable. Lambda resource policies captured the OLD RoleId server-side and deny the new role. Fix: new `gatewayServiceRoleArnOverride` prop + `agenticai/d03GatewayRoleArnOverride` context flag — lets operators pre-create a stable role once, then import it into the stack via `Role.fromRoleArn`.

**Also discovered** (MCP client-side, not a blueprint bug): MCP SigV4 clients MUST send `MCP-Protocol-Version: 2025-06-18` as a request header after `initialize`; without it AgentCore defaults to `2025-03-26` which the gateway rejects. Tool names MCP-qualifies as `target-<target-name>___<tool-id>`.

**Full end-to-end tool invocation verified live:**

```
IAM user (workload) → AssumeRole → Runtime role (AgenticAI-D03-demo-primary-runtime)
  → SigV4-sign MCP request → Gateway AWS_IAM authorizer (validates role)
    → Gateway service role → lambda:InvokeFunction cross-account (workload → platform)
      → tool-echo / tool-ping Lambda executes → JSON response
  ← MCP tools/call result (isError: false)
```

Live invocation results:

```
T3 tools/call target-tool-echo___tool-echo {"message":"hello from D-03 v3 live test"}
  → status=200  content="{\"message\":\"hello from D-03 v3 live test\"}"

T4 tools/call target-tool-ping___tool-ping {}
  → status=200  content="{\"pong\":true,\"ts\":\"2026-05-05T13:47:25.486Z\",
                         \"caller\":\"arn:aws:lambda:us-east-1:<platform>:function:agenticai-d03-tool-ping:PROD\"}"
```

Gateway `status: READY`, 2 targets `status: READY` (BMJZGIZRZE `target-tool-echo`, TLYVOWZJZA `target-tool-ping`), Cedar policy composed from 2 per-tool permits + default forbid, subscribed tool count 2.

**This closes out the D-03 v3 live verification — the full three-layer governance model (synth-time catalogue SSOT + deploy-time SCP-09 + runtime Gateway + GatewayServiceRole exact-ARN allow-list) is proven end-to-end on real AWS.** 170/170 tests still passing, scrubber green.

### Added

### v0.2.0 — Production-readiness hardening (2026-04-30)

#### Added

- **D-03 centralised-platform pattern** — `apps/platform-account/lib/d03-platform-core-stack.ts` + `apps/workload-account/lib/d03-workload-agent-stack.ts`. Workload agents on AgentCore Runtime reach Bedrock via cross-account `BedrockCallerRole` (AssumeRole + ExternalId; no session tags — see BUG-005). Registry tables, shared ECR, baseline guardrail, Model Invocation Logging, and per-tenant application inference profiles all owned by the platform.
- **Per-tenant Application Inference Profiles** as the D-03 CUR-attribution mechanism — each `{tenantId, agentId}` allocation gets its own `CfnApplicationInferenceProfile` pre-tagged with `application-id` / `agent-id` / `tenant-id` / `workload-account-id` / `cost-centre` / `environment` so CUR attributes every Bedrock line item without relying on `sts:TagSession` (which does not propagate across role chains).
- **PlatformInferenceGatewayConstruct** (`packages/platform-inference-gateway/`) — internal NLB + `VpcEndpointService` with `acceptanceRequired:false` and per-workload-account root ARN allow-list. Matching consumer `InterfaceVpcEndpoint` emitted by `D03WorkloadAgentStack` when `agenticai/d03PlatformInferenceServiceName` is supplied. Replaces the prior README §3.3 claim that was never implemented.
- **Live-AWS integration test suite** — `tests/integration/` (14 pytest cases across Bedrock/registry/ECR) + `tests/teardown/` (7 cases asserting zero residual resources). Skip-when-no-creds via autouse fixture; collection verified.
- **SCP regression suite** — `tests/conformance/scp-bypass-regression.test.ts` (7 cases) pinning the hardened condition shapes on SCPs 02/03/04/05.

#### Changed — Security hardening

- **Guardrail triple-gate hardened against `Null` bypass** — SCP-02 + D-03 `DenyInferenceWithoutGuardrail` no longer rely on `Null: {bedrock:GuardrailIdentifier: true}` (which empty-string bypasses). Replaced with `ForAnyValue:StringNotEquals` positive allow-list against the approved guardrail id + ARN, with the `Null` gate retained as a defense-in-depth twin.
- **SCP-03 + SCP-04 `IfExists` bypass closed** — removed `IfExists` suffix on `aws:SourceVpce`; added twin `Null: aws:SourceVpce=true` deny statements so console / public-endpoint calls fire the deny instead of silently passing.
- **SCP-05 role-session form** — `StringNotEquals aws:PrincipalArn` replaced with `ArnNotLike` matching both the role ARN and the `arn:aws:sts::*:assumed-role/*/...` session form.
- **All SCPs** — added `BoolIfExists aws:PrincipalIsAWSService=false` guard so AWS service principals aren't self-denied.
- **BedrockCallerRole trust narrowed** — `StringLike aws:PrincipalArn = arn:aws:iam::<acct>:role/AgenticAI-D03-*-runtime` + `StringLike sts:RoleSessionName = workload-*` on top of the existing `sts:ExternalId` condition.
- **AgentRuntimeRole unconditional account-root trust removed** — now gated by `allowLocalRootAssume` prop (default false) + `envName !== 'prod'`. Production posture trusts only the `bedrock-agentcore.amazonaws.com` service principal.
- **Cross-account KMS grants scoped** — registry + invocation-log CMK resource-policy statements add `kms:CallerAccount` + `kms:ViaService` (dynamodb for registry, logs for invocation) so arbitrary `kms:Decrypt` against bare ciphertext is denied.
- **DynamoDB tenancy scoping** — workload runtime role `ReadPlatformRegistry` + `WritePlatformExperiments` now carry `ForAllValues:StringEquals dynamodb:LeadingKeys = [tenantId]`. A compromised runtime cannot read other tenants.
- **Cross-account AssumeRole ExternalId** — workload-side `AssumePlatformBedrockCaller` identity-policy statement now carries `sts:ExternalId` condition so even a relaxed platform-side trust can't be assumed without the secret.
- **CMK removal policy defaults** — all four D-03 CMKs (RegistryKey, InvocationLogKey, FlowLogKey, MemoryKey) default to `RETAIN` + 30-day pending deletion window. Overridable via `retainDataKeys: false` for ephemeral dev loops only.
- **Memory CMK confused-deputy closed** — service-principal grant now requires `aws:SourceArn` to match the per-tenant Memory ARN pattern alongside `aws:SourceAccount`.
- **AgentCore Gateway internal ALB** — optional `certificate` prop promotes the listener to HTTPS:443; without a cert, HTTP+warn (SEC-013 narrowed). Egress SG rule moved from `Peer.ipv4(vpcCidrBlock)` to per-SG entries or fail-secure.
- **LiteLLM hard-coded placeholder prefix list (`pl-0000000000000000`) removed** — replaced with a caller-supplied `bedrockVpceSecurityGroup` prop. Network stack now emits `VpceSecurityGroupId` output; WorkloadAppStack imports and wires through.
- **ECR repository policy principal narrowed** — D-03 shared-image repo `Principal: {AWS: <acct-root>}` now carries `StringLike aws:PrincipalArn = arn:aws:iam::<acct>:role/AgenticAI-D03-*-runtime`.
- **D-03 application-inference-profile resource scope** — `AllowInvokeAllowlistedBedrock` replaced the `application-inference-profile/*` wildcard with the exact tenant profile ARNs.
- **D-03 invocation log group retention** — bumped from 90 days to 10 years, removal policy tied to `retainDataKeys` so the audit trail can't be orphaned.
- **Bedrock Model Invocation Logging actually wired** — D-03 stack previously created the log group + service role but never invoked `PutModelInvocationLoggingConfiguration`. Added the custom resource.
- **Guardrail deny — split into two statements** — `ForAnyValue:StringNotEquals` does NOT fire when the condition key is absent (empty set → no value "is not equal"). Live test 2026-05-01 T7 caught the bypass. Fix: keep the `ForAnyValue:StringNotEquals` for wrong-id case + add a separate `Null: { 'bedrock:GuardrailIdentifier': 'true' }` deny for the missing-key case. Verified: unguardrailed `Converse` now returns `AccessDenied`.
- **Narrowed stack-wide nag suppressions** — D-03 stack's blanket `AwsSolutions-IAM5` suppression removed; replaced with resource-level suppressions (SEC-024, SEC-025) scoped to BedrockLogDeliveryRole + BedrockCallerRole Deny-statement where resource-level scoping is not expressible.

#### Added — SECURITY-EXCEPTIONS

- SEC-024: narrow IAM5 on D-03 BedrockLogDeliveryRole + BedrockCallerRole Deny statement.
- SEC-025: IAM5 on guardrail `*` wildcard — baseline guardrail id not knowable at synth without circular reference.
- SEC-026: NIST.800.53.R5-ELBv2ACMCertificateRequired + AwsSolutions-ELB2 on PlatformInferenceGatewayConstruct NLB listener when no certificate supplied (TCP:443 passthrough; LiteLLM ALB behind owns TLS; surface gated by PrivateLink AllowedPrincipals).

#### Test coverage

- **128 tests passing** across 13 suites (up from 109).
- `cdk synth` clean on all D-03 stages + existing platform/workload/pipeline stages.

#### Live AWS verification (2026-05-01)

Two-account real deployment (us-east-1), 12/12 behavioural assertions PASS:

| #   | Assertion                                                                                       | Result |
| --- | ----------------------------------------------------------------------------------------------- | ------ |
| T1  | workload IAM user → runtime role AssumeRole (allowLocalRootAssume=true)                         | PASS   |
| T2  | runtime role → BedrockCallerRole with ExternalId + matching PrincipalArn + RoleSessionName glob | PASS   |
| T3  | Wrong ExternalId → AccessDenied                                                                 | PASS   |
| T4  | Non-matching RoleSessionName → AccessDenied                                                     | PASS   |
| T5  | `Converse` via demo tenant app-profile + baseline guardrail → 200                               | PASS   |
| T6  | `Converse` via retail tenant app-profile + baseline guardrail → 200                             | PASS   |
| T7  | `Converse` without guardrail → AccessDenied (Null+StringNotEquals dual deny)                    | PASS   |
| T8  | `Converse` via non-allow-listed resource scope → AccessDenied                                   | PASS   |
| T9  | Cross-account DynamoDB query for own tenant → 200 (kms:ViaService intact)                       | PASS   |
| T10 | Cross-account DynamoDB query for OTHER tenant → AccessDenied (dynamodb:LeadingKeys)             | PASS   |
| T11 | CloudTrail carries per-tenant inference-profile ARN (CUR attribution path)                      | PASS   |
| T12 | CloudTrail carries stable RoleSessionName (audit attribution survives role chaining)            | PASS   |

Teardown verified clean — zero residual CFN stacks / log groups / CMKs / guardrails / inference profiles.

#### Follow-up live run — AgentCore Runtime (Strands E2E path) — 2026-05-02

Created a live `AWS::BedrockAgentCore::AgentRuntime` in the workload VPC using `codeConfiguration` (S3-sourced Python 3.12). Two outcomes recorded for honesty:

- **Runtime provisioning succeeds against D-03 workload VPC + runtime role** — reaches `READY` status; VPC network mode + SG attachment + workload-identity directory entry all wired correctly.
- **AZ-ID constraint** — AgentCore Runtime only supports specific AZ IDs (`use1-az1`, `use1-az2`, `use1-az4` in `us-east-1`). The blueprint's `AgenticVpcConstruct` picks AZs by name, not by AZ ID. In our test `us-east-1a` mapped to `use1-az6` (not supported) and `us-east-1b` to `use1-az1` (supported). **Operational follow-up**: filter subnets by AgentCore-supported AZ IDs before attaching to a Runtime resource. Not a security finding.
- **`InvokeAgentRuntime` handshake** blocked by an AgentCore Runtime framework contract — the code-runtime wrapper expects a specific Python startup pattern that the blueprint's minimal `handler.py` does not yet implement. IAM + cross-account AssumeRole + guardrail + inference-profile surfaces were all proven by the prior 12/12 harness — this is an AgentCore SDK integration point, not a blueprint security issue.

### Added

- Pre-v1 planning set — 15 deliverables and 11 research briefs tracing every spec requirement to a migration phase. Kept privately; not part of the published sample.
- Phase 0 security-leakage scrub tooling: `scripts/scrub-security-leakage.sh`, `.gitleaks.toml`.
- Phase 0a scaffold: CDK monorepo directory layout (`packages/`, `apps/`, `pipelines/`, `blueprints/`, `handlers/`, `tests/`, `docs/`, `examples/`, `.github/`).
- Root documentation: canonical `README.md`, `LICENSE` (MIT-0), `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, a standalone deviations register (since folded into README section 3), `NOTICE`, this `CHANGELOG.md`.
- CDK app entrypoint, `cdk.json`, `package.json`, `tsconfig.json`, hardened `.gitignore`.
- Platform-baselines shared constants (`PLATFORM_ALLOWED_MODELS`, `PLATFORM_APPROVED_REGIONS`) as the single source of truth for model and region allow-lists.
- Initial GitHub Actions workflows: `build`, `test`, `gitleaks`, `cfn-lint`, `license-check`, `stale`, `dependabot`.
- Issue and pull-request templates under `.github/`.
- **Phase 1 — Organizations + OUs + SCPs**: `@agenticai/organizations` package emits `CfnOrganization` + 5 OUs (Security, SharedServices, AgenticAI-Platform, AgenticAI-Workloads, AgenticAI-Sandbox) + SCPs 01-08 attached to the Sandbox OU (sandbox-first per Phase 1 exit). SCP bodies sourced from `@agenticai/platform-baselines` SSOT; synth-time size check enforces the 5000-char soft limit. 18 unit tests + 11 conformance tests.
- **Phase 2 — Landing zone**: `@agenticai/landing-zone` emits `LogArchiveConstruct` (CMK-encrypted CloudTrail archive bucket + CUR bucket + S3 server-access-log bucket + CloudWatch Logs cross-account destination) and `AuditConstruct` (CloudWatch OAM sink). Bucket policies scoped to the Organization ID. 10 conformance tests.
- **Phase 3 — Platform resources**: `@agenticai/bedrock-guardrails` ships the `GuardrailAdminRole` (platform-only IAM role trusted solely by the CI/CD pipeline role) and `PlatformBaselineGuardrail` (HIGH content filters, Standard prompt-attack detection, AU-specific TFN/Medicare/BSB regexes, denied topics for financial advice + credential exposure + PII disclosure, PII masking for standard entities + BLOCK for credit card/SSN/AWS keys/passwords). 9 conformance tests.
- **Phase 4 — Workload factory**: `@agenticai/agentic-vpc` emits the per-workload VPC (3 AZs, private-isolated only, no IGW/NAT) with 11 interface VPCEs + 1 S3 gateway endpoint, Bedrock Runtime endpoint policy that restricts `InvokeModel` to the platform allow-list **and** denies when `GuardrailIdentifier` is Null, SG pair (workload-ENI ⇄ VPCE-ENI), VPC flow logs (CMK-encrypted), SSM parameters for SCP-03/04 resolution. `@agenticai/bedrock-invocation-logging` configures `PutModelInvocationLoggingConfiguration` via custom resource. 16 conformance tests.
- **Phase 5 (in progress) — LiteLLM D-01 compensation**: `@agenticai/litellm-gateway` ECS Fargate + internal ALB + CMK + task role with triple-gate guardrail enforcement (layer 2 of SCP-02 / IAM deny / VPCE policy). 6 conformance tests pin the Deny-on-null-GuardrailIdentifier statement and model-allow-list scoping.
- **SECURITY-EXCEPTIONS.md** register populated with 11 entries (SEC-001 through SEC-011), each carrying rule id, scope, requirement ID, justification, and 2027-04-30 expiry. Renewal workflow documented.

### Test coverage

- **79 tests passing** across 7 suites (11 unit + 68 conformance). `cdk synth` emits 5 stacks (management, log-archive, audit, guardrail, workload-network) all clean against `AwsSolutionsChecks` + `NIST80053R5Checks` with only the documented suppressions.

### v0.1.0 — Phases 5–9 complete (2026-04-30)

- **Phase 5 full**: LiteLLM (D-01 triple-gate), API Gateway fronting AgentCore Gateway (§08 Option A), AgentCore Identity (Cognito + Token Vault CMK), AgentCore Memory (static namespace per tenant), AgentCore Runtime (first-party execution role + ECR + log group), Registry (DynamoDB), RAG (S3 + VPCE-only), 3 Strands blueprints (task / chatbot / multi-agent).
- **Phase 6**: Per-app CloudWatch dashboard, SLO alarms with SNS topic, OAM source links to audit sink, per-app AWS Budget filtered by `application-id` tag.
- **Phase 7**: CDK Pipelines for platform + workload with mandatory stage sequence (Synth → Non-prod → Evaluation Gate → Manual Approval → Prod). Evaluation gate reads 5 threshold env vars and exits non-zero on any breach.
- **Phase 8**: `examples/reference-deployment-ap-southeast-2/` with `cdk.context.json` + walkthrough; `scripts/scp-sandbox-soak.sh`, `scripts/teardown.sh`, `pipelines/bootstrap/bootstrap-cross-account.sh`; live smoke-test harness under `tests/smoke/`.
- **Phase 9**: Full docs set — ARCHITECTURE, DEPLOYMENT, COSTS, WELL_ARCHITECTED, NIST-800-53-MAPPING, OPERATIONS, SHARED_RESPONSIBILITY, CHOICE_ARCHITECTURE, MULTI_ACCOUNT_GUIDE; threat model; 8 operational runbooks; 5 ADRs; benchmarks README.
- **130 tests passing** (109 Jest conformance + 21 pytest) across 13 suites.
- **All stages synth clean** against `AwsSolutionsChecks` + `NIST80053R5Checks` with 23 documented SECURITY-EXCEPTIONS entries.
- Scrubber green.

This was the v1.0.0 release candidate, subject to AppSec, legal, and solution-quality review before publication.

### Changed

- Migrated README from a project-starter template to the AWS `aws-solutions-library-samples` 11-section canonical template.
- Swapped `LICENSE` from MIT-with-copyright-preamble to canonical MIT-0.

### Security

- All catalogued leakage items from the original source repo scrubbed or archived privately.
- `scripts/scrub-security-leakage.sh` enforces zero-match on the known leakage patterns.

## [0.1.0] — 2026-04-30

Pre-v1 scaffold. Not a published release.
