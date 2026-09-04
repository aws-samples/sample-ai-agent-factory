# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added — LiteLLM MCP Gateway as a second gateway provider
AgentCore Gateway remains the default; nothing about it changes. A canvas Gateway
node can now instead point at a customer-run **LiteLLM MCP Gateway**, selected by
`gatewayProvider: "agentcore" | "litellm"` (default `"agentcore"`, so every
existing canvas and stored flow works unmigrated). A platform-wide default lives
in a `SETTING#gateway_provider` row and the per-agent field wins over it.
- Dispatch happens inside the existing `gateway_step` handler, **not** a new Step
  Functions branch: the step's inputs and outputs are identical, so
  `has_gateway`/`HasGateway?`/`HasGatewayForAuth?` and the state machine are
  untouched. The non-SFN direct path in `services/deployment.py` got the same
  dispatch, or it would have silently ignored the provider choice.
- LiteLLM authenticates with a **static virtual key**, so
  `client_info["provider"] = "litellm"` drives a third arm in
  `runtime_configure_step` emitting `GATEWAY_AUTH_MODE=static_bearer`; the
  generated agent skips the token exchange and sends `x-litellm-api-key`, with
  pinned server aliases on `x-mcp-servers`. Both duplicated copies of the
  generator body were updated.
- Readiness is proven, not assumed: `GET /v1/mcp/server` then
  `GET /mcp-rest/tools/list`, **failing loud on zero tools** — the same rule the
  AgentCore path enforces.
- Secret hygiene matches the connector path: the raw key is minted into Secrets
  Manager and popped from the payload before the Step Functions event is
  re-emitted. The base URL goes through the existing SSRF guard.

### Added — LiteLLM as an alternative registry catalog
A LiteLLM proxy can become the **authoritative** catalog in place of the internal
DynamoDB one, behind a `SETTING#registry_provider` row that defaults to
`dynamodb`. With the default selected the call path is equivalent to before, which
is why `test_registry_store.py` and `test_registry_rbac.py` needed **no edits** —
that was the regression signal for this work.
- New `services/registry_providers/` seam (`base.py` Protocol, `dynamo.py`
  adapter, `litellm.py`, `get_registry_provider()`), with a `capabilities()`
  declaration per provider.
- **LiteLLM has no write API for MCP server records** (registration is Admin-UI or
  `config.yaml` only), and its registry object has no canvas snapshot, no
  per-entry owner, and no review state machine. The read-only limit is therefore
  **per entry, not per operation**: a row projected from LiteLLM returns **`501`
  naming LiteLLM** on publish/update/delete, approve/reject and clone rather than
  silently accepting a write that would then diverge, while agents published from a
  canvas live in the platform sidecar and keep the full normal workflow. The
  catalog is the merge of the two, with the sidecar row winning a slug collision so
  an entry published before the switch stays reachable and mutable.
- The pre-deploy governance gate is provider-dispatched and stays **fail-closed**:
  present-and-enabled in LiteLLM is the approval signal, an unreadable catalog
  raises the same `RegistryQueryFailed` → `503`, and an empty-but-readable catalog
  blocks too. A guard test asserts the gate reads only `/v1/mcp/server` and never
  `POST /mcp-rest/tools/call`.
- A private LiteLLM saves as `unverified` instead of being rejected: the control
  plane has no VPC egress, so unreachability is expected there, while a 401/404 is
  a real misconfiguration and still fails. New endpoints
  `GET|POST|DELETE /api/registry/litellm-config` and
  `GET /api/registry/litellm-servers`; the virtual key lives under a new
  `agentcore-registry/` secret namespace and is never returned or logged.
- Reverting to the platform catalog is one call and touches no DynamoDB entry. It
  clears the stored connection as well as the setting, so it is labelled
  **Disconnect & use platform catalog** rather than implying a toggle.

### Added — region-agnostic deployment
`./scripts/deploy.sh` no longer hard-fails outside `us-east-1`; `AWS_REGION`
selects the deployment region and everything derives from it. Verified live by
deploying the whole platform to **eu-central-1** and invoking a deployed agent
there, then redeploying `us-east-1` to prove non-regression.
- **WAF is region-aware.** `us-east-1` is unchanged: one `CLOUDFRONT`-scoped
  WebACL on the distribution. Elsewhere the same rule set is created as a
  `REGIONAL` WebACL associated with the Cognito user pool — AWS accepts only
  `CLOUDFRONT`-scoped ACLs on a distribution and creates those exclusively in
  `us-east-1`, and this stack's HTTP API v2 is not WAF-attachable either. Pass
  `CLOUDFRONT_WEB_ACL_ARN` to also attach an edge ACL you created yourself. These
  are also the **first** WAF assertions in `infra/tests/`, which had none.
- **Account-global names are region-qualified via `cfg.global_resource_name()`,
  which returns the incumbent unqualified name in `us-east-1`.** So two regions
  can coexist in one account *and* adding a second region renames or replaces
  nothing in an existing `us-east-1` deployment — confirmed by a `cdk diff`
  against the live stack showing zero renames and no resource replacement.

### Fixed — a clean delete reported an error it had already decided to ignore
`DELETE /api/runtime/{id}` returned `success:true` with a body reading
`"[manifest] runtime X: Runtime X deleted; Runtime destroy error: An error occurred
(ConflictException) ... Current status: DELETING."` — observed live in us-east-1 on
a teardown that fully succeeded and left zero orphans. In the opposite race the
second call wins and the same line is merely duplicated (`"... deleted; ...
deleted"`) — observed live in eu-central-1.

Cause: manifest teardown (Step 0a) deletes the `agent_runtime` row, then the legacy
per-component fallback calls `destroy_runtime` a second time on the same runtime.
Bug 159 already knew that second call reports spuriously and stopped *counting* it
toward `success`, but its message was still appended to `cleanup_messages`. So the
one string an operator reads after deleting an agent looked like a failed teardown.

The fallback's message is now suppressed when the manifest actually owned the
runtime delete — keyed on an `agent_runtime` row being present, not merely on a
manifest existing, because a deploy that failed before recording the runtime leaves
a manifest without one and there the fallback *is* the real delete and must keep
reporting. Verified live end to end after the fix: deploy → invoke
(`TEARDOWN-CHECK-OK`) → delete returned the single line
`"[manifest] runtime ...: Runtime ... deleted"`, with no runtimes, IAM roles,
`agentcore-connector/` secrets or gateways left in the account.

This matters disproportionately because customers deploy and delete constantly, so
this was the message on essentially every teardown.

### Fixed — hardening RBAC would have deleted every provisioned Cognito user
Scope enforcement ships advisory (`require_scopes` logs a would-deny and allows
unless `RBAC_ENFORCE` is truthy) — confirmed live: an authenticated caller holding
no Cognito groups still read `GET /api/registry/litellm-config` on the deployed
stack. Turning it on was reachable only through the instruction
`docs/RBAC_ROLLOUT.md` actually printed: a raw `cdk deploy -c rbac_enforce=true`.
That bypasses `deploy.sh`, and with it the `COGNITO_USERS` carry-forward guard
below — so the one command the docs gave an operator for tightening access control
would have offboarded every user as a side effect.

`scripts/deploy.sh` now forwards `-c rbac_enforce="${RBAC_ENFORCE}"` (defaulting to
empty, which the stack reads as `"false"`, so an operator who sets nothing changes
nothing), the doc prescribes `RBAC_ENFORCE=true ./scripts/deploy.sh` and warns off
raw cdk with the reason, and `infra/tests/test_deploy_rbac_enforce.py` pins all
four halves — including that an empty passthrough cannot read as enforcing.

Note the hard group check is unaffected: `caller_is_admin`/`is_registry_admin` is
unconditional, so registry writes stayed gated even in advisory mode.

### Added — live coverage for LiteLLM as a Gateway *target* (the third shape)
`scripts/verify-external-mcp.py` grew `MCP_TARGET_MODE=custom`, which drives the
CUSTOM-endpoint branch of `_deploy_external_mcp_targets` — a raw `endpoint` with no
catalog `server_id`, which is how a self-hosted proxy such as a customer's LiteLLM
becomes a Gateway `mcpServer` target. That branch had unit coverage only, and unit
tests cannot show that AgentCore accepts the target params we synthesize. It now
does, live: real gateway, target `READY`, `tools/list` returning
`mcp-custom-aws-knowledge___aws___*`, a real `tools/call` answer, clean teardown.

All three gateway shapes are therefore live-verified end to end: AgentCore Gateway
(unchanged), LiteLLM *instead of* the Gateway, and LiteLLM *as a target on* it.

### Fixed — a plain redeploy silently deleted every provisioned Cognito user
Each `COGNITO_USERS` email is a custom resource whose Delete handler calls
`AdminDeleteUser`, so dropping an email is the intended offboarding mechanism. But
bash cannot distinguish an **omitted** variable from an intentionally emptied one:
a routine `./scripts/deploy.sh` with `COGNITO_USERS` unset removed every
provisioner and deleted every user it had created, taking their password and group
memberships with it — silent, unprompted data loss on the most ordinary command in
the repo. **Observed for real** against `agentcore-workflow-dev` on 2026-09-03
(268 → 256 resources; the one live user deleted).

An empty list against an **existing** stack now carries forward whoever is already
provisioned, printing a warning that names them. Removals still work but must be
stated: pass the reduced list, or `COGNITO_USERS=none` to clear it entirely. Fresh
deploys are unaffected. `infra/tests/test_deploy_cognito_guard.py` runs the
extraction snippet embedded in `deploy.sh` itself rather than a copy — the
near-miss while writing it was a filter on `Type` that read correctly and matched
nothing, because CDK emits these as `AWS::CloudFormation::CustomResource`, not
`Custom::*`, and a guard that extracts zero emails is indistinguishable from no
guard. These are the first tests of `deploy.sh` in the repo.

### Added — `scripts/verify-litellm.py`, a live verifier for the LiteLLM path
The unit suites for both LiteLLM workstreams are necessarily mock-based — they
assert what we *believe* LiteLLM returns. This script asserts what it actually
returns, driving the shipped product code against a real proxy: the payload shapes,
the parsers, the readiness gate's fail-loud behavior, the registry projection, the
governance gate, and the sidecar merge against a real DynamoDB table. Companion to
`scripts/verify-external-mcp.py`, which does the same for the AgentCore path.

Two things it caught that mocks could not, both now documented in
[`docs/MCP_GATEWAY_INTEGRATION.md`](docs/MCP_GATEWAY_INTEGRATION.md#the-two-wire-shapes-those-probes-parse):

- **The two probe endpoints return different shapes.** `GET /v1/mcp/server` returns
  a bare JSON list; `GET /mcp-rest/tools/list` returns an object with a `tools` key.
  A parser written for the wrong one returns zero items *silently*, which is the
  exact empty-tool-plane failure the readiness gate exists to catch.
- **Enablement may not be reported at all.** On the release tested, server records
  carry `status: null` and no `enabled`/`disabled`/`active` field, so presence in
  the list is what gates a deploy. `_server_is_enabled` honors a flag where one
  exists and defaults to enabled where none does; the registry docs now say to
  *remove* a server rather than rely on a disable toggle.

Also confirmed live: LiteLLM answers a rejected virtual key with **400**, not 401.

### Fixed — teardown destroyed a co-resident deployment's resources
Customers deploy and delete this platform often, so two deployments sharing one
account — dev + prod, or two teams, or the same environment in two regions — is
routine rather than exotic. Every sweep in `sweep_orphan_resources` matched on an
**account-global name prefix** that carries no deployment identity: Cognito
`AgentCore*`, secrets under `agentcore-connector/` and `agentcore-otel/`, IAM roles
`AgentCoreMemory-*` and `AgentCoreRuntime-*`. Tearing one deployment down deleted the
other's resources, including the secrets holding raw customer API keys.

`ManagedBy=agentcore-flows` could not fix this: it names the **product**, so it is
present on every deployment's resources. Resources are now stamped
`AgentCoreStack={project}-{env}-{region}` at creation (all six sites: Cognito pool,
connector secret, per-agent OTEL secret, memory role, runtime exec role, and the
duplicated direct-deploy copies of the last two), and `cleanup.sh` deletes only what
carries its own value. The region is part of the identity because **IAM is not
regional** and `config.py` deliberately supports the same `{project}-{env}` twice.

- **The worst case was cross-region, and it was not hypothetical.** The runtime-role
  sweep filtered on `starts_with(RoleName, 'AgentCoreRuntime-${PROJECT_NAME}')`, which
  matches `AgentCoreRuntime-{project}-{env}-{region}-shared` — the **CDK-managed shared
  execution role every agent in the other region assumes**. Verified against the live
  account: a us-east-1 teardown deleted the eu-central-1 deployment's shared role.
  Deleting dev broke prod, unrecoverably without a redeploy plus AgentCore's 17–20 min
  IAM-cache wait. IAM roles receive no `aws:cloudformation:*` system tags (confirmed
  live), so the `-shared` name suffix is the only available signal that CloudFormation
  owns a role; the sweep now leaves those to `cdk destroy` *and* checks the owner tag.

  The in-product delete path already had exactly this guard —
  `runtime_deployer` skips the shared role by exact name *and* by `-shared` suffix
  (Bug 62), so deleting one agent could never brick the others. `cleanup.sh` was the
  one place that never got it, which is why the sweep was the only route to this
  failure.

  It is no longer hypothetical in the other direction either: a Frankfurt teardown
  deleted **us-east-1's** `AgentCoreRuntime-agentcore-workflow-dev-shared`, and the
  next `cdk deploy` there failed on ~20 Lambdas at once with
  `Unable to retrieve Arn attribute for AWS::IAM::Role … cannot be found (404)`,
  leaving the stack in `UPDATE_ROLLBACK_FAILED`. Nothing user-facing broke in the
  meantime — the rollback left the previous build serving — but no agent could be
  deployed until the role was restored and its IAM cache repropagated.

  Recovery, in order, because **CloudFormation does not self-heal an
  out-of-band-deleted resource**:
  1. `aws cloudformation continue-update-rollback` to clear `UPDATE_ROLLBACK_FAILED`.
  2. `aws iam create-role` with the same name and trust policy
     (`bedrock-agentcore.amazonaws.com` / `sts:AssumeRole`), so the `Fn::GetAtt … Arn`
     the Lambdas depend on resolves again.
  3. `cdk deploy` — which succeeds, but note it leaves the role **powerless**: the
     separate `AWS::IAM::Policy` resource is unchanged in the template, so CFN never
     re-issues `PutRolePolicy` and the recreated role carries no permissions at all.
     Verified: `list-role-policies` came back empty after a clean `UPDATE_COMPLETE`.
  4. Reattach the inline policy from the synthesized template
     (`infra/cdk.out/*.template.json`, resource `SharedRuntimeExecRoleDefaultPolicy*`),
     resolving its two `Fn::GetAtt` ARNs (artifacts bucket, HITL table) from the live
     stack, then `put-role-policy` and diff the result against the template.

  Step 3's silent no-op is the trap here: the stack reports `UPDATE_COMPLETE` while
  every agent deploy would still fail on permissions.
- **Ownership fails closed.** An untagged resource is treated as foreign. A resource
  predating the tag and a resource belonging to someone else are indistinguishable,
  and only one of those two mistakes is recoverable — deleting a foreign credential
  cannot be undone, skipping a legacy orphan costs one manual delete. Teardown reports
  what it left and why; `CLEANUP_INCLUDE_UNTAGGED=1` opts back into sweeping untagged
  resources.
- **A caller-supplied `AgentCoreStack` tag cannot reassign ownership.** Governance tags
  come from canvas metadata, so without this a tenant could mark its resources as
  belonging to another deployment and have that deployment's teardown delete them.
- `PROJECT_NAME` is now on every API and step Lambda's environment. Without it the
  handlers fell back to the default project name and stamped resources for the *wrong*
  stack — which fails closed, but silently leaks the whole namespace on every teardown.

Verified against real AWS in **both** deployed regions, not with mocks: the behavior
under test is a *refusal*, and a mocked assertion would only re-check a transcription
of the JMESPath filters — which is precisely where the bug lived.
`scripts/verify-cleanup-ownership.sh` plants three decoys per swept namespace (owned
by this stack / owned by a different stack / untagged), runs the real
`sweep_orphan_resources`, and asserts exactly one of the three is gone. **15/15 in
us-east-1 and 15/15 in eu-central-1**, including the real Frankfurt shared runtime role
surviving a us-east-1 sweep. It removes everything it plants, including on failure.

### Fixed — five teardown leaks, every one found by reading the live account
None of these was visible from a test suite or from a teardown's own return value:
each one reported `success=True` and left a resource behind. They were found by
deploying the LiteLLM paths for real, tearing them down through the product's own
delete path, and then *inventorying the account* — which is the only step that can
catch a cleanup that lies. Sixteen resources were stranded in the verification
account before these fixes, including nine secrets holding raw customer API keys.

- **`scripts/cleanup.sh` never deleted a single connector credential provider.**
  `gateway_result.connector_credential_providers` records each entry as `"TYPE:name"`
  (`API_KEY:` / `OAUTH:`), the shape `_record_gateway_resources` partitions on. The
  script passed that entry straight to `--name`, where the `':'` violates the
  provider-name pattern `[a-zA-Z0-9\-_]+`, so **both** deletes failed with
  `ValidationException` — swallowed by the `2>/dev/null || true` on every call, so the
  teardown printed the provider name and reported success while the provider and its
  credential survived. Found by watching a real teardown's log; verified against live
  AWS by creating a provider, confirming the raw entry left it listed, and confirming
  the stripped name deleted it. Legacy bare names contain no `':'` and are unaffected.
  Regression test: `infra/tests/test_cleanup_provider_prefix.py` executes the shipped
  bash expansion rather than a transcription of it.

- **API-key credential providers survived teardown.** The external-MCP path recorded
  provider names *untyped*, and `gateway_step` defaults an untyped entry to
  `oauth2_credential_provider`. The two vaults are independent namespaces behind one
  account-global API, and — verified live — `delete_oauth2_credential_provider` on an
  API-key provider **returns success without deleting it**. So a mis-typed row
  produced a clean-looking teardown and a stranded credential. The producer now
  records `API_KEY:`/`OAUTH:` like the connector path, and the new
  `purge_credential_provider` purges *both* namespaces, using each namespace's own
  `get_*` as the discriminator so rows already written are repaired too.
- **Pre-minted external-MCP secrets were orphaned, raw key and all.**
  `gateway_step` mints the api_key secret early so the plaintext is dropped before
  the SFN event is re-emitted, but the deployer tracked only secrets it minted
  *itself*, so no `secret` manifest row was ever written. Nine such secrets outlived
  their agents. Now tracked with parity to the connector path, guarded by an
  ownership check on the `agentcore-connector/` prefix so a `secret_arn` the customer
  supplies is never deleted along with the agent.
- **A gateway deploy that failed after creating the gateway recorded nothing.**
  `created_resources` came back `null`, so with no runtime to scan for, nothing named
  the gateway and no teardown could ever find it. `deploy_gateway` does attempt its
  own abort cleanup, but `cleanup_gateway_resources` reports per-resource failures by
  *returning* them and the abort path discarded that list while logging success at
  INFO — so the one time it failed, it failed invisibly and permanently. The abort now
  inspects what it gets back and warns, and the failure path returns its partial
  inventory so the step handler writes manifest rows; the normal manifest-driven
  teardown (which already accepts a `deployment_id` for exactly this case) finishes
  the job on a later delete.
- **Cognito pools were invisible to both cleanup layers.** The pool is created near
  the top of `deploy_gateway` but `client_info` is not bound until the very end, so
  for a failure anywhere in between — most of the deploy, including all target
  creation — the abort path's `client_info` lookup found nothing. That is why every
  stranded gateway in the account had a stranded pool beside it, each one counting
  against an account quota. The error path now falls back to `cognito_response`.

Verified on real AWS after each fix, against the deployed Lambda rather than local
code: a Path-3 deploy whose manifest now reads `api_key_credential_provider` plus a
`secret` row, then a product teardown after which the account holds neither; a
caller-supplied secret that survives teardown while its credential provider is
purged; and a deliberately-failed deploy whose gateway and pool are both recorded
and both gone after teardown.

### Fixed — Bedrock cross-region inference prefix outside us-east-1
`_to_cross_region_model_id()` force-prefixed every model with `us.`, so an agent
deployed to any non-`us-east-1` region would fail at invoke time against an
inference profile that does not exist. The prefix is now derived from the region.
- The APAC prefix is **`apac`, not `ap`** — this repo used a bare `ap` at all
  three prefix sites and in the frontend helper. Verified against the live
  `bedrock list-inference-profiles` in five regions: `ap.` exists in **no**
  region. A regression test asserts this across all four sites (Python, TypeScript
  and the CDK f-string), since no type checker links them.
- A stale hand-typed `ap.` prefix is still *recognised* as already-prefixed rather
  than becoming `eu.ap.anthropic…`.
- Worth knowing when picking a region: the `apac.` family covers only older Claude
  models — current-generation APAC models publish under *country* prefixes (`jp.`,
  `au.`) or as `global.`, so an APAC deployment may need its model ID set
  explicitly.

### Fixed — `provider_base_url` had no validation
The customer-supplied model-provider base URL is injected as `PROVIDER_BASE_URL`
and is the destination the runtime sends `PROVIDER_API_KEY` to as a bearer
credential, but had no validation beyond a 512-character cap — so a typo'd or
hostile value silently became the recipient of the customer's provider key. Now
https-only, host required, no `user:pass@` userinfo, no whitespace or control
characters (a newline would forge a second runtime environment variable), and no
link-local literal (IMDS). Deliberately **not** routed through the private-CIDR
SSRF guard: the dialer here is the AgentCore Runtime, which supports VPC egress,
so a self-hosted proxy on a private address is the intended configuration for this
field and a private-CIDR denylist would reject the very setup it exists to serve.

### Fixed — one outbound allowlist was doing two jobs
`_validate_outbound_url` guards non-OIDC fetches (connector spec URLs, a LiteLLM
base URL) but read `OIDC_DISCOVERY_HOST_ALLOWLIST` for all of them, so an operator
who pinned their identity provider silently pinned their LiteLLM proxy to the same
host list and got a rejection citing OIDC config they had set for an unrelated
reason. Non-discovery fetches now prefer `OUTBOUND_HOST_ALLOWLIST` and fall back
to the OIDC variable when it is unset, so no existing deployment is loosened. OIDC
discovery reads only its own variable — a general outbound allowlist must not
widen which identity providers the platform will fetch metadata from. The
private-IP denylist still runs regardless of any allowlist match.

### Fixed — `GATEWAY` was missing from the AWS Agent Registry record enum
`RECORD_TYPES` modelled four of the live GA service's five members, so
`normalize_record_type("GATEWAY")` fell through and silently returned `"CUSTOM"`,
and `DESCRIPTOR_KEY_FOR_TYPE` modelled four of six `Descriptors` members. Latent
until now — production only ever passed `AGENT` or `CUSTOM` — but it stops being
latent the moment a gateway-provider concept exists that someone would reasonably
register as a `GATEWAY` record.

### Changed — AWS Agent Registry: preview → GA
Agent Registry graduated out of AgentCore into its own AWS service. The rename is
a **silent** break: the deprecated `bedrock-agentcore-control` model still exposes
the Registry operations with the old `descriptorType` parameter, so preview code
keeps "succeeding" against a shim under an IAM prefix it no longer has. Migrated
end-to-end:
- boto3 clients `bedrock-agentcore-control`/`bedrock-agentcore` →
  `agent-registry-control`/`agent-registry`; IAM actions `bedrock-agentcore:*` →
  `agent-registry:*` (both planes sign as `agent-registry`)
- `descriptorType` → `recordType`, with the enum renamed
  `MCP|A2A|CUSTOM|AGENT_SKILLS` → `MCP|AGENT|CUSTOM|SKILL` (preview spellings are
  still accepted as input aliases)
- Descriptors reshaped: `a2a.agentCard.inlineContent` → `a2aAgentCard.data`,
  `custom.inlineContent` → `custom.data`, `schemaVersion` → `dataSchemaVersion`;
  added `mcpServer` and `agentSkillsDefinition` builders
- Data-plane `SearchRegistryRecords` → `SearchDiscoverableRegistryRecords`, with
  the GA structured filter shape (`{"recordType": {"$in": [...]}}`)
- `boto3 >= 1.43.66` is now a hard floor (first release carrying the
  `agent-registry` service models) in both `pyproject.toml` and
  `requirements-lambda.txt`
- `GET /api/registry/aws-config` gained `sdk_supported`, and `POST` now returns a
  400 naming the SDK instead of blaming the `registry_id`, so an under-pinned
  bundle is distinguishable from a bad registryId

All of the below was verified against the live GA service, not just the boto3
models: a throwaway registry, every descriptor builder submitted through the
shipping adapter, and the approval lifecycle exercised end to end.

### Fixed — found by live verification against GA
- **Every redeploy silently failed to re-register.** `name` + `recordVersion` is a
  uniqueness key and `recordVersion` is `"1.0"` for everything the platform
  registers, so the *second* deployment of an agent raised `ConflictException`
  inside the best-effort auto-register handler. The symptom was a governance record
  frozen at the first deployment's runtime ARN and endpoint — stale forever, with
  nothing surfaced anywhere. `register()` is now an upsert (falling back to
  `UpdateRegistryRecord`), which needs the new
  `agent-registry:UpdateRegistryRecord` grant on the `status_update` step role.
  Note updating content demotes a record `APPROVED` → `DRAFT`, so an upsert cannot
  slip changed content past an old approval — a redeployed integration must be
  re-approved, which is the fail-closed reading.
- **`available()` reported a still-provisioning registry as usable.** It returned
  True the instant `GetRegistry` succeeded, but a registry in
  `CREATING`/`UPDATING`/`DELETING` rejects `CreateRegistryRecord` with
  `ConflictException`. Enabling federation on a freshly created registry — the
  common sequence — therefore passed validation and then raced into that conflict
  on the first deploy. Now gated on `READY`, with a new `registry_status()` that
  keeps "not READY" distinct from "could not ask"; `POST /aws-config` returns 409
  ("still provisioning") instead of a 400 blaming the registryId.
- **Search results could show a stale `APPROVED` badge.** The data plane is a
  search index, not the record store: a record demoted to `DRAFT` keeps being
  served as `APPROVED` for many minutes (still drifting 20 minutes after
  demotion). Combined with the upsert this is reachable on the ordinary redeploy
  path. `GET /api/registry/aws-search` now reconciles every hit's status against
  the control plane and reports `status_authoritative: false` — dropping `status`
  rather than serving the index's copy — when it cannot. Approval *gating* always
  read the control plane and was never affected; a new AST-level guard test keeps
  it that way.
- **Descriptor content contracts corrected** (each one an outright rejection by the
  live schema validator, reported only as an unactionable descriptor-wide error):
  A2A card skills require *all* of `id`/`name`/`description`/`tags` (empty `tags`
  is fine, absent is not) and the card requires `url`; `mcpServer.data` is an MCP
  server.json whose `name` must be namespaced `<namespace>/<server>` (a bare name
  is rejected) with `description` and `version` required; `agentSkillsDefinition`
  must omit `dataSchemaVersion` entirely, unlike every other descriptor; and both
  the tools and skills payloads must be objects (`{"tools": [...]}`), never bare
  arrays. Under-specified inputs are now normalized rather than forwarded.
- `UpdateRegistryRecord` takes a different shape from `CreateRegistryRecord` —
  every branch and scalar leaf is wrapped in an `optionalValue` patch envelope.
  Passing the create shape fails in botocore's *client-side* validation, never
  reaching AWS, and on the deploy path that lands in the best-effort handler.

### Fixed
- **Auto-register on deploy never worked**: the `status_update` step Lambda — the
  role that actually calls `CreateRegistryRecord` — had no registry permissions at
  all, so every federation attempt was an `AccessDenied` swallowed by the
  best-effort handler. The exception cause is now logged rather than discarded.
- **An unqueryable registry was indistinguishable from a rejected integration.**
  Gating swallowed every error into "nothing is approved", so an `AccessDenied` on
  `agent-registry:ListRegistryRecords` — or a registryId typo — rendered as a 403
  telling the operator their integrations had been *rejected*, sending them to fix
  a governance record when the fault was an IAM policy. Absent data and negative
  data are now distinct: `list_records_strict()` raises `RegistryQueryFailed`,
  which surfaces as a 503 naming the registry as unreachable. Gating stays
  fail-closed for a *successful* query that finds no approval.
- **`list_records()` returned only the first page**, so fail-closed integration
  gating could block a deploy against an integration that *is* `APPROVED` further
  down the list. Now follows `nextToken`, and pushes the `APPROVED` narrowing
  server-side via the GA `filters` parameter.
- Registry adapter degrades instead of raising when the bundled boto3 predates GA
  (`boto3.client()` raising `UnknownServiceError` used to 500
  `GET /api/registry/aws-config`).
- Descriptor `data` payloads are now checked against the service's 102400-**byte**
  cap (measured in bytes, not characters) with an error naming which descriptor
  overflowed. AWS's `ValidationException` identifies neither, and on the deploy
  path it lands in a best-effort handler that would reduce it to a log line.
- `frontend/src/services/api.ts` carried a second, independent declaration of
  `getAwsRegistryConfig()`'s return type; only `tsc -b` (project references, as CI
  runs it) surfaced the mismatch — `tsc -p` on the root project did not.

### Added
- GitHub Actions CI: ruff lint/format, backend unit tests with coverage floor,
  CDK assertion tests + `cdk synth` (cdk-nag gate), frontend lint/typecheck/tests/build
- Dependabot for npm, pip, and GitHub Actions; `SECURITY.md` vulnerability policy
- Pyright (basic mode, advisory) and wider ruff rule set (`I`, `B`, `UP`)
- Committed `frontend/package-lock.json` for reproducible builds (`npm ci`)

### Fixed
- README/`.env.example` no longer instruct deploying to `us-west-2`, which
  `deploy.sh` rejects (the WAF WebACL is CLOUDFRONT-scoped and requires
  `us-east-1`)
- Stale CDK assertion tests updated to the current architecture (14 DynamoDB
  tables, 3 S3 buckets, no `States.TaskFailed` retry, CloudFront Function SPA
  routing instead of CustomErrorResponses)

### Fixed — full-matrix verification (12 live-found deploy/runtime defects)
Every deployable pattern was verified end-to-end against real AWS —
**94 patterns PASS with canary evidence, 0 FAIL, 0 PARTIAL** (the remaining
294 are BLOCKED by design: non-Bedrock frameworks / third-party
IdPs / SaaS creds / customer VPC infra, each code-cited). Fixes:
- Web-crawler KB verified end-to-end (example.com → ingest → index → agent
  retrieves the crawled content); the ingestion wait is bounded to the SFN
  task budget and an in-progress crawl is treated as success, not failure
- Generated memory agents now retrieve long-term memory records across sessions
  (`retrieve_memories` was never called); memory+knowledge-base canvases no
  longer silently drop KB retrieval
- `CreateMemory` retries the IAM trust-policy propagation race; failed deploys
  no longer leak gateways (targets deleted before the gateway)
- OpenSearch Serverless KBs: `aoss:BatchGetCollection` scoped correctly
  (account-level API); BDA parsing uses the correct
  `supplementalDataStorageConfiguration` shape + bucket-root URI + role grants
- Knowledge-base deploys are idempotent on retry (`CreateDataSource` /
  `StartIngestionJob` conflict-adopt); KB step role gains
  `ListDataSources`/`GetDataSource`
- KB-backed runtime deletion is now asynchronous — returns immediately with a
  `delete_status` pointer instead of timing out API Gateway's 29s cap (503);
  double-delete is tolerated
- Cedar ENFORCE policy engine self-heals a regressed `UPDATE_FAILED` permit
  (previously could stay deny-all forever if no touchpoint fired); the
  scheduled sweep reconciles ENFORCE engines against live policy status
- `GET /evaluation-config` resolves custom-named online-evaluation configs by
  CloudWatch target (not just the `eval_<id>` name heuristic)
- `list_gateways` conflict recovery is paginated (multi-page accounts)

### Added — multi-target gateways & custom MCP endpoints
- One gateway node can now carry **multiple targets of different families**
  (Lambda ARNs, external MCP servers, OpenAPI specs, Smithy models) via a
  repeatable target-array editor; the deploy creates one gateway target per
  entry with family-appropriate outbound credentials
- The MCP-server picker gained a **Custom endpoint…** option (any https MCP
  URL with none / API-key / OAuth2-CC / IAM SigV4 outbound auth, SSRF-validated)
- Generate Agent emits gateway nodes with the required `targetType`/
  `targetConfig` (deterministic spec normalization — no more "Target Type is
  required" errors after Apply to Canvas)

### Fixed — gateway deploy/teardown hardening (live-verified end-to-end)
- **"AddPermission … The provided principal was invalid"** on multi-target and
  multi-gateway deploys: the orphaned-permission prune was inert because the
  gateway step role lacked `lambda:GetPolicy`; granted, and the prune now warns
  instead of silently swallowing AccessDenied
- OpenAPI targets in the multi-target path no longer request
  `GATEWAY_IAM_ROLE` (AgentCore rejects it); public specs omit the credential
  block, API-key/OAuth are honored
- Shared singleton tool Lambdas (`AgentCoreDynamicTools` /
  `AgentCoreCustomerSupportTools`) are released by **reference count** on every
  teardown path (user delete, failure auto-cleanup, manifest) — tearing down
  one gateway no longer breaks other live gateways sharing the Lambda, and the
  Lambda is deleted when the last gateway releases it (including the
  empty-policy vs missing-function `ResourceNotFoundException` ambiguity)
- Failed gateway deploys release everything they provisioned (no orphan
  gateway/role/Cognito/grants)
- Bedrock Converse calls omit `temperature` for Claude Sonnet 5+ / Opus 5 /
  Fable models (param deprecated → ValidationException broke Generate Agent)
- Chat panel always renders the message input on a fresh session

## [0.1.0] - 2026-07-17

Initial public sample: visual drag-and-drop workflow builder for Amazon Bedrock
AgentCore with Step Functions-orchestrated deployment, gateway/tool wiring,
memory, knowledge bases, guardrails, observability, evaluations, enterprise
governance (RBAC/ABAC, Cedar policies, approvals, budgets), and manifest-driven
teardown.
