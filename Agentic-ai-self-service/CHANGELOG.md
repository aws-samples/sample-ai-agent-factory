# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed — a stalled CloudFormation response upload is retried instead of waited out

- `cfn_response.send` opened the pre-signed PUT with no socket timeout. A connection that stalled
  sat inside `urlopen` until the Lambda's own 300 s budget killed the invocation, so none of the
  four retries ran, no FAILED was sent, and CloudFormation waited out the hour-long custom-resource
  timeout. Each attempt is now bounded to 10 s. Raised by the independent review of the export path.

### Fixed — the two spellings of the gateway provider must agree

- `resolve_gateway_provider` read `gateway_provider or gatewayProvider` on the raw values, so a
  whitespace-only first spelling hid a real second one and the empty result took the platform
  default: the silent wrong-backend path the function exists to refuse, reachable from imported
  JSON and direct API calls. Both spellings are read; blank ones are ignored; two different
  values are refused by name.

### Fixed — a fresh clone can deploy: the CDK CLI lock file is tracked

- `scripts/deploy.sh` installs the pinned CDK CLI with `npm ci`, which needs `infra/package.json`
  AND `infra/package-lock.json`, and refuses to continue without them. The lock file was still
  listed in `.gitignore` from the era when the CLI came from `npx`, so it existed on every machine
  that had run a deploy and on no fresh clone. `infra/tests/test_the_stack_passes_its_own_nag_gate.py`
  reads the same file and was the first to notice, in CI.

### Fixed — the exported `deploy.sh` passes ShellCheck 0.9

- `bundle_digest_in_bucket` read its S3 key as `${1:-$BUNDLE_KEY}` and two callers invoked it bare.
  ShellCheck 0.9.0 (the Ubuntu package) reports that as SC2120/SC2119; 0.11 does not, which is why
  the shipped-script lint gate was green locally and red in CI. Every caller now passes the key.

### CI — the `cdk assertions` job installs the backend

- Several CDK assertions import the backend they grant for (route parity, the owner tag the
  backend stamps, the ownership getters). The job installed only `infra/requirements-dev.txt`, so
  six of them failed with `ModuleNotFoundError` in CI while the certified local environment, which
  carries the backend, passed all 653.

### Fixed — a slow Memory delete is confirmed in the background instead of being reported as retained

- The asynchronous teardown gives each delete one Lambda invocation's confirmation budget (about six
  minutes for a Memory). A harness's managed Memory outlived it on 2026-10-02: the teardown recorded
  `delete_retained` ("Resources retained by deletion-authority policy: iam_role, memory") for a Memory
  the service was still deleting, although the API's own reply had promised to confirm slow resources in
  the background. The Memory was gone minutes later and a second DELETE converged in twelve seconds. A
  delete the service accepted and had not finished is now the one case the teardown carries into a fresh
  background invocation, up to four more times (about thirty minutes), renewing its claim so a concurrent
  DELETE still reads "already in progress"; only a real refusal or failure ends it early, and the stored
  message now holds 4 KiB instead of 1 KiB, which had cut off the kept memory role's reason.

### Fixed — a harness's deploy-time warm-up no longer times out in the browser or lands in a conversation

- The deploy panel pings every new HTTP deployment once, fire-and-forget, to absorb its cold start. For a
  harness, that first turn ran past the HTTP API's 30-second integration limit (measured 2026-10-02: over
  30 s once after deploy, then 3.5-5 s per turn), so the browser recorded a 504 while the Lambda finished the
  turn. The harness branch also ignored the warm-up flag, so "ping" was stored as a conversation turn in the
  session named after the harness. The route now hands the ping to an asynchronous invocation of itself (the
  tool-test route's pattern) on a session no conversation uses, and answers at once.

### Fixed — redeploying a gateway after a teardown no longer leaves its Lambda targets unauthorized

- A gateway's role is granted `lambda:InvokeFunction` through a statement in each target function's
  resource policy. When the role is deleted, IAM rewrites that statement's principal to the deleted role's
  unique id. The prune that clears such leftovers asked only whether a role of that NAME existed. A redeploy
  had just recreated the role, so the dead statement was kept, the new grant conflicted on the same
  statement id, and the conflict was read as "already permitted". CreateGatewayTarget then refused:
  "Gateway execution role lacks permission to invoke Lambda function". Measured live on 2026-10-02 in the
  first gallery deploy after a full teardown. A statement now survives only if its principal is the
  role's current ARN, on every path that grants a gateway role invoke access.

### Fixed — a harness's conversation log no longer lives forever

- AgentCore hosts each harness on a runtime it creates itself, and that runtime's DEFAULT log group
  (`/aws/bedrock-agentcore/runtimes/harness_<name>-<id>-DEFAULT`) holds every conversation the harness
  serves. The service creates the group with no retention. The runtime path already bounds the groups of
  the runtimes it creates; the harness path did not. On 2026-10-02 all 60 harness groups in the platform
  account were set to never expire, including the one left by that morning's deploy-and-delete. The
  harness step now reads the backing runtime from the ready harness and gives its group the same 30-day
  retention. As on the runtime path, it fails closed: a harness whose conversation log cannot be bounded
  does not report success. Groups that already exist keep whatever retention they have.

### Fixed — the Runtime and Observability configuration dialogs save on a platform without OTEL

- With platform OpenTelemetry not configured (the default), `GET /api/observability/platform-defaults`
  answers `{"enabled": false, "endpoint": null, "sample_rate": null, "service_name_prefix": null}`. The
  UI's policy validator accepted an optional field only when it was absent. It read those nulls as an
  unreadable admin policy and kept Save disabled on every Runtime and Observability configuration dialog,
  so no runtime could be configured from the palette and no template's runtime could be edited. A null
  optional now means unset, as the route's model declares, and a wrong type still fails closed. Found by
  building a canvas from the palette in a real browser on 2026-10-02. Every earlier test mocked
  `{enabled: false}`, a shape the route never sends. Both sides are now pinned to the route's real bytes.

### Fixed — the active-deployment banner no longer offers a deleted deployment

- The banner asks for the caller's `status=succeeded` deployments, and a delete keeps `status` while
  setting `delete_status`. Every one of the matrix user's 103 succeeded deployments was deleted, and the
  banner offered to restore the newest. A deployment any delete has touched is no longer active.

### Fixed — the CloudFormation export carries a gateway's configured Lambda targets

- The platform deploys every target configured on a gateway node (a Lambda by ARN, an OpenAPI
  spec, a Smithy model). The export never read them: it answered 200 with a gateway that lacked
  those targets, and a gateway whose only target was a Lambda came out with no target at all. The
  live matrix exported two gallery canvases this way, strands-gateway-agent and
  customer-support-assistant. The UI requires their gateway to serve something, so refusing would
  have left them unexportable.
- A configured Lambda target is now exported as the platform deploys it. It becomes a gateway
  target named `cfgtgt-lambda-<index>`, serving the entry's inline tool schema or the platform's
  pass-through tool. Its function ARN is a parameter defaulting to the configured one, and the
  gateway role may invoke exactly that ARN. The stack never edits the function's own resource
  policy. The default Cedar permit names its tool.
- An OpenAPI spec or Smithy model configured as a target, a malformed ARN, or a staged tool schema
  is refused with a 400 that names it. The empty row a new gateway starts with is not a target.

### Fixed — failed deployments that share an MCP server runtime can be deleted

- Four failed mcp-server-gateway-target deploys (no runtime of their own, unsealed manifests)
  all listed one shared MCP server runtime. Deleting one handed the runtime off in the manifest
  loop, and then the legacy MCP-server step, which an unsealed manifest keeps enabled, retained
  the same runtime because another of them still referenced it. That retention counted as a
  consumer that may still be running, so every hand-off became a retention and the teardown
  ended `delete_retained`. A `delete_retained` tombstone is a live reference, so the last two
  protected the runtime, gateway, OAuth2 provider and roles for each other, and retrying either
  repeated that indefinitely. Measured on the matrix account, 2026-10-02.
- A single-resource legacy step (MCP server runtime, memory and its role, guardrail) now defers
  to the manifest when the manifest loop already decided that exact resource. The legacy steps
  still run for a resource no manifest row records, which is what they exist for. The coarse
  steps (gateway, Knowledge Base) are unchanged, because they also clean children an unsealed
  manifest may not record.

### Fixed — a tool Lambda's log group is bounded by the platform's retention

- Lambda creates `/aws/lambda/<function>` itself on a tool function's first invocation, with no
  retention, and nothing in the platform governed or deleted it. Measured on the matrix
  account after every deployment had been deleted: both of the stack's shared tool functions
  (`AgentCore-<token>-DynamicTools` and `-CustomerSupportTools`) were gone, and both log groups
  were still there with no retention, holding the tool calls of every gateway that had used
  them. The gateway deploy now creates or adopts each tool function's group (shared, custom
  and Knowledge Base) with the 30-day retention the runtime's DEFAULT group gets. It does
  this before the function's Gateway Target exists, and only once the function is in the
  deploy's abort inventory. The call is fail-closed. Teardown leaves the groups to expire,
  as it does the runtime's, so deleting a deployment does not erase its audit trail. The CFN
  export never had this gap: it declares a retention-bounded group for every function.
- On upgrade, the next gateway deploy applies the 30-day retention to the existing shared tool
  groups too, so their events older than 30 days expire.
- The gateway step role gains `logs:CreateLogGroup` and `logs:PutRetentionPolicy` on exactly
  the stack's own `/aws/lambda/AgentCore-<token>-*` groups, one region-exact prefix per
  supported region, mirroring its function grant. Cross-account target roles must add the new
  `ToolLambdaLogGroupGovernance` statement from `docs/cross-account-deploy-role.json`, or a
  gateway deploy that creates a tool function fails at `PutRetentionPolicy`.
- A Knowledge Base tool function created before a later failure in the same gateway deploy is
  now in the failed deploy's manifest rows. The abort inventory had no field for it.

### Fixed — a governed export's OAuth2 credential provider is created instead of rolling the stack back

- An exported MCP-server-gateway-target stack failed its `McpOAuth2CredentialProvider` custom
  resource with AccessDeniedException whenever the canvas carried governance tags, and the
  stack rolled back. A tagged `CreateOauth2CredentialProvider` is authorized for
  `bedrock-agentcore:TagResource` on the `token-vault/default` container as well as on the
  provider. The platform already granted both to its own roles. The exported provider role
  granted only the provider. Proven live with two throwaway roles built from the rendered
  statements: the provider-only grant was refused on `token-vault/default`, the fixed grant
  created the provider. A test now requires the export and the platform to grant the same two
  ARNs.

### Fixed — deleting an older version no longer stays "retained" forever over a name a live version holds

- Measured live: a retry of an older version's teardown reported "Runtime name kept locked (no
  version row proves this deployment owns it)" on every attempt. Its first attempt had already
  released its own version row, and a live version held the name, so no retry could ever change
  the verdict. A `delete_retained` deployment is a live reference, so each one also pinned every
  shared row it listed (the shared gateway, its target and the MCP server runtime) for good. A
  deployment that holds no row under the name while live versions hold it now reports that
  nothing of it remains to release. An orphan slot, a dangling pointer at its own version, or a
  row that carries its deployment id still keeps the name locked.
- The same teardown counted a trigger that belongs to another live version of the same name as
  its own residue ("kept locked (4 trigger row(s) still registered under it)"). While a live
  version keeps the name, the slot stays and those triggers stay deletable through it. Only a
  trigger aimed at the runtime being deleted, or one nobody can attribute, keeps the name locked
  now. A release that removes the name keeps the strict rule.

### Fixed — an exported stack's dependency bundle builds on a stock Mac, at the platform's pins

- `build-dependency-bundle.sh` in a CloudFormation export ran the first `pip3` on `PATH`. On
  a stock Mac that is the Command Line Tools pip 21.2.4. Measured live: it backtracked for 25
  minutes, then crashed with `RequirementParseError`, and `deploy.sh` stopped before a stack
  existed. Every pip up to 24.1 checks Requires-Python against the interpreter it runs on
  rather than the Python 3.13 target ("bedrock-agentcore requires a different Python: 3.9.6
  not in '>=3.10'"); 24.2 and later build the bundle in under a minute. The script now uses
  `pip3` only when it is 24.2 or newer. Otherwise it builds with a current pip in a temporary
  virtualenv, and otherwise it stops before downloading anything and says what to install.
- The same build floated every version the platform pins. Unpinned, it resolved
  strands-agents 1.57.1, bedrock-agentcore 1.24.0, OpenTelemetry 1.45.0 and websockets 17.1,
  a set the platform never built or tested. The script now carries the platform's
  constraint file (`backend/agentcore-deps-constraints.txt`) and a recipient's bundle
  matches the platform's own. A test fails when the exporter's copy and the file differ.

### Added — every generated agent reports the tools it actually ran

- A generated agent now returns `tool_receipts` beside its reply: one receipt per tool call its
  own loop executed during that turn, read from the conversation the loop wrote (each `toolUse`
  the model emitted and the `toolResult` the loop produced for it), with the tool name, a
  status (`success`, `error` or `missing`) and SHA-256 digests of the arguments. The model's
  reply text cannot forge one, so a caller can tell a tool the runtime ran from a tool result
  the model invented. Argument values never leave the runtime; only their digests do.
- Covered: the web-search agent, the Strands gateway agent, the tools agent (browser, code
  interpreter, knowledge base), the memory agent and the default Strands agent. Multi-agent
  graph, swarm and workflow agents do not report receipts yet.
- A gateway tool whose name was shortened to fit Bedrock's 64-character limit is reported under
  the name the gateway published, not the alias the model saw.
- `POST /api/test-runtime-stream` and the streaming Function URL add `tool_receipts` to the
  `done` event, and `POST /api/test-runtime` and slot invocations return `toolReceipts`. The
  platform validates each list before relaying it and drops a malformed list whole, never in
  part. Existing clients are unaffected: the field is optional and new.
- **Upgrade note:** receipts appear once an agent is redeployed, since they are part of the
  generated code.

### Fixed — a test console MCP session no longer lands in a new microVM on every call

- `POST /api/test-mcp-runtime/tools` and `/call` forwarded only the MCP session id. AgentCore
  routes by the runtime session id, so every request (initialize, each tool-list page, each
  call) started a new runtime session, cold-started a new microVM, and returned a new session
  id. Measured live: three calls with the same session id returned three new ones. The proxy
  now sends the id AgentCore issued at initialize as the runtime session too, so a session
  stays in one microVM; an id that cannot name a runtime session is forwarded as before.

### Fixed — a memory agent no longer contradicts the user with an out-of-date remembered fact

- Long-term memory records were handed to the model as plain "Relevant long-term memory". A
  memory outlives sessions and is shared by every agent of the same owner that names it, so an
  older fact could contradict what the user had just said, and the agent sided with the stale
  record ("according to the long-term memory I have on file, your code is X, not Y"). The
  generated memory agent now marks long-term records as possibly out of date and states that
  the current conversation wins.

### Security — the shared runtime role reads only its own stack's connector secrets

- `SharedRuntimeExecRole` could read every `agentcore-connector/*` secret in the account. The
  read is now conditioned on the secret's `ManagedBy=agentcore-flows` and
  `AgentCoreStack={project}-{env}-*` tags, which closes reads of other stacks' tenants, of
  untagged secrets and of anything foreign under that prefix. Inside one stack the shared role
  is still one principal for every tenant; `per_agent` identity mode is what isolates tenants
  from each other there.
- **Upgrade note:** a connector secret created before ownership tags existed is no longer
  readable by a shared-mode runtime. Redeploying the agent re-binds its connector into a
  freshly tagged copy.

### Security — the OIDC client secret no longer appears in the synthesized template

- The CDK context value `oidc_client_secret` was written verbatim into the Cognito identity
  provider's properties. The stack now takes `-c oidc_client_secret_arn=<Secrets Manager ARN or
  name>` (and optionally `-c oidc_client_secret_json_key=<field>`) and renders a
  `{{resolve:secretsmanager:...}}` dynamic reference, which CloudFormation resolves at deploy
  time.
- **Upgrade note:** the old `-c oidc_client_secret` key now fails synth with a message naming the
  replacement, so a stale deploy script cannot silently ship a plaintext secret.

### Added — a permissions boundary for every role the platform creates (enforcement off by default)

- A managed policy, `{project}-{env}[-{region}]-agentcore-role-boundary`, allows only the
  actions the platform writes onto the roles it creates and explicitly denies IAM writes, role
  assumption, Organizations, account and CloudFormation actions. Its ARN is published to every
  deployment and step Lambda as `AGENTCORE_ROLE_PERMISSIONS_BOUNDARY_ARN`.
- Enforcement is off by default, because no role the backend creates carries the boundary yet.
  With `-c enforce_role_permissions_boundary=true` the platform may create or change a role only
  when the boundary is attached (`iam:PermissionsBoundary` condition). Leave it off until every
  role creator attaches the boundary.

### Fixed — request bodies over 8 KB are no longer blocked at the edge

- The CloudFront web ACL attached AWS's common managed rule set unchanged, and its
  `SizeRestrictions_BODY` rule blocks any request body over 8 KB with an HTML 403. Measured live:
  deploying a canvas with a 9 KB system prompt and posting a 9 KB webhook both failed before
  reaching the platform, although the webhook route accepts 240 KB. That one rule now counts
  instead of blocking; every other rule in the group still blocks. The platform bounds bodies
  itself (per-route limits such as the webhook's 413, API Gateway's 10 MB, Lambda's 6 MB).

### Fixed — deleting old versions of an MCP-gateway agent no longer strands its shared resources

- Every version of an agent whose gateway fronts an MCP server runtime adopts that runtime, the
  gateway, its target, the credential provider, the resource server and the role. Deleting an
  older version handed those off to the newer one, but because one of them was a runtime, the
  teardown assumed its own runtime might still be running and recorded every hand-off as a
  retention. That `delete_retained` record kept protecting the shared resources, so deleting the
  last version could never remove them. Measured live: nine older versions ended
  `delete_retained`. A hand-off now counts as "may still be running" only when it is the
  deployment's own runtime or harness, so older versions end `deleted` and the last version
  removes the shared resources.
- A retry of such a deletion then stopped at the version's own Cognito user pool: the first
  attempt had already deleted it, and a pool that no longer exists can never prove it belongs to
  this stack, so it was reported "skipped (protected)" on every retry. A pool, app client or
  resource server whose pool Cognito reports as not existing is now treated as already gone, in
  both the delete path and the deploy-failure cleanup path. Any other failure to read the pool
  still fails the step so it is retried, and an existing pool that cannot be proven ours is still
  left untouched.
- **Upgrade note:** an older version already recorded as `delete_retained` for this reason is
  recovered by deleting it again.

### Fixed — concurrent trigger events on one agent are no longer delayed by an hour or dropped

- Every trigger delivery takes the deployment's exclusive teardown lease, so two deliveries for
  one agent exclude each other. The one that lost the race failed its queue message, waited out
  the dispatch queue's one-hour visibility timeout, and after five losses went to the
  dead-letter queue. Measured live: an S3 upload that coincided with a scheduled delivery on the
  same agent was not delivered within twelve minutes. A delivery that loses the race has
  invoked nothing, so it is now re-enqueued with a delay of 15 to 45 seconds and an attempt count
  (at most 60 attempts, after which the queue's own retry applies). The completed-delivery record
  still suppresses any duplicate.
- A failed trigger delivery's log line now names the dispatcher's reason (a fixed sentence),
  not only the error type.

### Fixed — a trigger can no longer be registered against a runtime that is being torn down

- `POST /api/runtimes/{name}/triggers` resolved the caller's ownership through the
  production slot and then wrote the trigger row unconditionally. A teardown landing in
  between deleted the slot and version rows first, the write then succeeded, and the owner's
  own `DELETE` of that trigger answered `404` forever, because the same resolver answers
  through the slot that was just removed. Reproduced against the real stores by the audit
  session. The row is now written in one DynamoDB transaction conditioned on the exact slot
  and version rows that authorized it (owner, production pointer, deployment id, status,
  recorded target), and the same transaction moves a fence attribute on the slot row.
- The teardown's name release pins that fence and, after its strongly consistent slot read,
  re-reads the triggers partition: a trigger registered after the destroy's own enumeration
  keeps the name locked with a message saying so, and one registered after the release's
  read cancels the release. Either ordering leaves the owner a slot to delete the trigger
  through.
- A create that loses this race answers `409` with nothing written; a webhook trigger's
  freshly minted signing secret is deleted in the same path.
- **Upgrade note:** `RuntimeSlots` rows gain an opaque `trigger_fence` attribute the first time
  a trigger is registered against them. Rows without it release exactly as before.

### Security — target accounts no longer hold a grant on the platform's dependency bundles

- The platform artifacts bucket policy used to grant `s3:GetObject` and `s3:PutObject` on
  `deployments/*` and `agentcore-deps/*` to two roles in every account listed under the
  CDK context `deploy_target_accounts`. Nothing used it: cross-account deploys write
  `code.zip` to a bucket in the target account, and read the dependency bundles with the
  platform's own session. What the grant did allow was a target account overwriting the
  `agentcore-deps/*` bundles that every platform deploy ships into every runtime. The
  grant is removed.
- **Upgrade note:** a stack synthesized with `-c deploy_target_accounts=...` now fails at
  synth with a message naming the replacement (register the target through the admin
  API). It is refused rather than ignored so a stale deploy script cannot look like it
  still grants something.
- Export bundles (`POST /api/generate-cfn-template`, `POST /api/export-python`) are now
  pinned as unreachable through the platform: an inventory of every S3 call in the
  backend (every operation in boto3's S3 model, transfer helpers, presigns, paginators,
  waiters, and every caller of a helper that hosts one) with the reason each key cannot
  be another caller's export key, plus refusal of dynamic dispatch (`getattr`, aliases,
  `resource()`, `_make_api_call`), and a check that the presigned URL is returned once,
  under `download_url`, and never logged, stored or passed on. A new S3 call fails the
  suite until it is reviewed.
- A CloudFormation export whose resolved governance tags exceed S3's 10-tags-per-object
  limit is now refused with `400` and the remedy *before* anything is generated or
  staged. The exported stack's `AgentCodePackage` writes every workload tag onto the
  agent code object, so a larger set would have produced a template that was either
  undeployable or only partly tagged. The download bundle itself keeps its fixed
  four-tag set and is unaffected.

### Changed — every API route declares a scope, and the API enforces by default

**Upgrade note:** users who were not created through `COGNITO_USERS` and are in no
Cognito group now get `403`. Assign them a group, or deploy with
`RBAC_ENFORCE=false` first (docs/RBAC_ROLLOUT.md, "Upgrading an existing
deployment").

- 21 of 108 mounted routes had no scope, including `POST /api/deploy`,
  `DELETE /api/runtime/{id}`, `POST /api/test-runtime`, the `/api/flows` routes and
  git-token/git-sync. A caller in no group could deploy, delete and invoke even
  with `RBAC_ENFORCE=true`, because only a declared scope is enforced. Ownership
  still confined those calls to the caller's own rows. Each now requires
  `agent:write`, `agent:read` or `invoke`. A test walks both apps' mounted routes
  and fails on any unguarded route except `/health` and `/api/identity/token-info`.
- `RBAC_ENFORCE` now defaults to `true` on both API Lambdas, and the backend reads
  an absent or misspelled value as enforcing. Only an explicit false value is
  advisory.
- The user provisioner puts every `COGNITO_USERS` user in `g-users-default` +
  `t-user`. `g-users-default` gains `agent:write`, matching the documented standard
  user who builds and deploys their own agents. The upgrade's Update only syncs
  groups: re-running the create would have reset every live user's password.
- The provisioner no longer replies to CloudFormation itself. It sat behind a CDK
  Provider, which also replies, so every event got two responses with different
  physical ids, and which one won varied by deploy (a UUID on one live stack,
  `pool:email` on another). If the handler's reply won an Update, the id change
  made it a replacement, and the cleanup Delete removed the user being updated.
  The handler now returns the id to the framework and keeps it on a same-user
  Update. Proven live under the real framework: the upgrade Update kept a
  CONFIRMED user CONFIRMED, and an email change deleted only the old user.
- `g-admins-registry` and `g-admins-super`, the admin groups the pool actually
  creates, are now registry approvers alongside legacy `registry-admin` /
  `org-admin`. Before this, a user granted `g-admins-registry` held
  `registry:write` but could not approve.
- Tests pin the frontend's group→scope table and registry-admin list equal to the
  backend's.

### Added — CloudFormation export now carries LiteLLM, naming, and tagging intent

The customer export path now recognizes LiteLLM gateways instead of silently
emitting an unrelated AgentCore gateway. It exports the validated HTTPS proxy
URL, MCP-server aliases, and a Secrets Manager ARN for the LiteLLM key. Inline
virtual keys are deliberately never copied into the artifact. A LiteLLM canvas
without the configuration needed to reproduce it is refused with an actionable
400 response rather than producing a partial stack.

Exports also accept a declarative naming profile: a constrained prefix plus
per-resource-family templates over a fixed placeholder vocabulary. This shape
can cross the JSON API boundary, unlike a Python naming callback, while still
defaulting to the existing names when no profile is supplied. Resolved names
and governance tags are recorded in the template metadata and generated README.
Tag property names and shapes follow each CloudFormation resource schema,
including AgentCore's mix of map and list tags and Cognito's `UserPoolTags`.
Unknown naming families, placeholders, resource types, and invalid tag values
fail closed.

### Fixed — the exported bundle is validated as the customer consumes it

The export contract now exercises every supported component combination with
reference resolution and full `cfn-lint` diagnostics. Redundant explicit
`DependsOn` edges were removed where `Ref` or `GetAtt` already establishes the
dependency; dependencies that carry real ordering semantics remain explicit.
The one exact warning exception is
`bedrock-agentcore:CreateTokenVault`: the public action catalog used by
`cfn-lint` does not currently list it, but a live AgentCore authorization
failure names that permission as required, so the deployable permission is
retained and the exception is narrowly pinned.

A representative exported ZIP is now checked from a clean recipient directory:
ZIP integrity, executable script modes, `terraform fmt/init/validate`, strict
CloudFormation linting, Bash parsing, and ShellCheck all have to pass. The
Terraform example in the generated README is the exact block the gate parses,
so documentation and the shipped deployment path cannot drift independently.

### Security — a deploy step can no longer replace code in a Lambda it never touches

Four Step Functions task roles were granted the same eleven-action Lambda
mutation policy on `function:AgentCore*` because all four steps sounded like
they deploy code. Three of them make no Lambda API call at all. Since
`lambda:UpdateFunctionCode` on a function makes that function's *execution*
role run the caller's code, each of those three roles silently held every tool
Lambda's identity for a capability it never used — the risk being sharpest on
the code-generation step, whose input is model output. The grant is now split
per step: only the gateway step creates or replaces function code, the
knowledge-base step keeps just the tag read it makes (to check ownership of a
caller-supplied knowledge-base transformation function, which is why that one
read is not name-scoped), and the MCP-server and code-generation steps hold no
Lambda permission at all.

`lambda:InvokeFunction` was removed from all four: a tool Lambda is invoked by
the AgentCore gateway's own role through the function's resource policy, never
by a deploy step. On the deployment Lambda's role, `lambda:CreateFunction` and
`lambda:InvokeFunction` likewise moved off `function:AgentCore*`/`MCPServer*`
onto the tool-test sandbox prefix, which is the only function that path creates
or invokes apart from itself.

Each removal was established from a transitive call graph over the real handler
entrypoints rather than from what a step is nominally for, because the failure
direction here is an outage: these calls sit on paths with no fallback. A new
check re-derives that call graph at synth time and fails if any step role holds
a Lambda action its own handler cannot reach, so a step added later is covered
without anyone remembering to add it.

A second check covers the dimension the first one cannot see. The narrowing above
is about *which* actions a role holds; a role can hold exactly the right action
and still be scoped to every function in the account. Reading the deployed policy
back out of IAM showed this happening: a deliberately narrow tag-read statement
had been absorbed into a pre-existing broad one, because CloudFormation
statements with identical action lists are merged by combining their resources, so
the narrow one contributed nothing. Every action-level check passed through it.
Account-wide Lambda permissions on a deploy step now have to be either bounded by
a condition or listed as a named exception with its reason, and the check fails if
an exception outlives the permission it covers.

### Fixed — the IAM grant checks could not tell which role held a permission

The suites asserting that a deploy path has the permissions it needs scanned
every policy in the synthesized template, which cannot answer a question about
one principal: IAM grants a role only what is attached to it. In practice one
suite's assertions were being satisfied by an unrelated role's broader grant, so
deleting the permission the suite existed to protect left it entirely green.
Attachment is now resolved per role — inline policies, attached policy
documents, and the managed policies CDK spills into when a role exceeds the
inline size limit, which is where the affected statements actually lived.
Deliberately unchanged: checks asserting that *nobody* holds an over-broad
permission remain template-wide, since narrowing those would let the next such
grant appear elsewhere unnoticed.

Paired-capability checks ("anything that can replace a function's code must be
able to read its ownership tag") now compare full resource ARNs per role rather
than function names across the template. Two grants that agree on name but
differ in account or region do not work together — tags supplied at creation are
authorized against the resource being created in the same request — and a
name-only comparison reported such a pair as consistent.

### Security — release gates now cover authored source and the real repository boundary

CI and local release checks include Ruff formatting/linting, Semgrep, Bandit,
dependency vulnerability audits, and detect-secrets over tracked and untracked
files that can actually be committed. The pre-commit configuration is scoped to
`Agentic-ai-self-service/`, preventing `--all-files` from inspecting or
rewriting sibling projects in the containing repository. New baseline entries
are reviewed individually; generated lockfiles are excluded from the
committable-source scan rather than treated as authored credentials.

### Clarified — trigger support is a definition registry preview

The API, UI, and documentation now consistently call cron, EventBridge, S3, and
webhook entries trigger **definitions**. This release validates and stores the
definitions and manages the platform-owned webhook HMAC secret, but it does not
provision Scheduler, EventBridge routing, or a webhook dispatcher. Definitions
therefore remain `registered` and do not fire.

### Clarified — the export uses exactly four custom resource types

The generator, provider dispatch, generated README, and contract tests now
agree on the complete inventory: `Custom::AgentCodePackage`,
`Custom::RuntimeLogGroup`, `Custom::OAuth2CredentialProvider`, and
`Custom::AgentCorePolicy`. All four are served by the same provider Lambda and
are emitted only when their corresponding capability is present.

### Fixed — deployments that create AgentCore resources no longer fail on their own tags

Every AgentCore resource this platform creates is tagged with its owner in the
same API call that creates it, so teardown can attribute it later. AWS
authorizes that as a separate `bedrock-agentcore:TagResource` permission on each
resource the call tags, which the step roles did not hold. The result was a
deployment that failed partway with an access-denied error naming a tag
permission rather than the resource being created.

The grant is now derived from what each step's handler actually creates, one
statement per role, scoped to the specific AgentCore resource types that step
tags. That distinction matters in one non-obvious case: creating a runtime or a
gateway also mints a workload identity and tags it, even though nothing here
creates a workload identity directly, so a grant covering only the named
resource type was not enough. Creating a gateway or a harness likewise tags the
outbound credential providers it establishes.

There is deliberately no untagged fallback. An untagged resource cannot be
attributed to the deployment that created it, so a missing tag permission now
fails the deployment rather than quietly leaving an unattributable resource
behind.

### Fixed — live runtime conversation logs no longer retain forever

The UI/Step Functions deployment path now creates or adopts the exact
`/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT` log group after creating
both primary and generated MCP runtimes, and applies a 30-day retention policy.
The runtime is recorded in the teardown manifest before governance runs, and a
retention failure fails the deployment instead of reporting success with
unbounded conversation logs.

The step and documented cross-account roles scope this authority to the
AgentCore runtime-log prefix. They intentionally do not receive
`logs:TagResource` on that account-wide prefix: the runtime ID is not known when
the role is synthesized, and a wildcard tag grant could relabel unrelated
historical runtime groups. The customer CloudFormation export retains its
separate configurable retention and optional customer-managed-key support.

### Fixed — CDK now uses the Python environment whose dependencies were installed

`npx cdk` can prepend a different Python installation to `PATH`. On machines
with more than one Python, that allowed dependency installation to use one
interpreter while synthesis used another, producing a materially different
template from the same checkout. Deploy and cleanup now resolve one interpreter,
export it through `CDK_PYTHON`, and invoke pip through that interpreter. The CDK
app also compares the installed `aws-cdk-lib`, `constructs`, and `cdk-nag`
versions with the exact pins in `infra/requirements.txt` and fails before synth
when they differ.

The CDK context opts Node.js Lambda bundling into the latest supported runtime,
so synthesis no longer falls back to an older runtime merely because a stale
CDK library was selected.

### Fixed — the generated Terraform wrapper is valid canonical HCL

The customer-facing README now emits a `terraform fmt`-clean
`aws_cloudformation_stack` wrapper using `template_url` and the complete staged
artifact-parameter map. The export CI gate installs a pinned Terraform CLI and
parses/formats that exact generated block, so malformed interpolation, braces,
or non-canonical HCL fail before the bundle reaches a customer.

### Fixed — a pasted AWS identifier could become Knowledge Base authority

Knowledge Base configuration accepted existing resource IDs and ARNs as if
identifying a resource also authorized the platform to grant Bedrock access to
it. Customer-owned Knowledge Bases, S3 buckets, S3 Vectors indexes, OpenSearch
Serverless collections, Aurora clusters, transformation Lambdas, KMS keys, and
credential source secrets now require a live
`AgentCoreFlowsAccess=allow` owner opt-in. An optional `OwnerSubHash` restricts
that consent to one authenticated caller. Account, region, ARN family, index
compatibility, and same-deployment retry ownership are checked before the first
IAM mutation.

Customer KB credentials are copied into deployment-bound target-account secrets,
so the source ARN is never placed in Step Functions history or granted to the
Bedrock role. Provider and OTEL credentials use the same copy boundary with
their stricter platform-stack and caller ownership proof. Platform-created KB
resources must prove the exact stack, deployment, and caller before conflict
recovery or retry can reuse them.

The Knowledge Base role trust now constrains both `aws:SourceAccount` and the
region/account Knowledge Base `aws:SourceArn`. Its S3 Vectors policy contains
only data-plane actions on the selected index; provisioning remains on the
deployment step role.

### Fixed — failed-deployment cleanup lacked reads required by its own safety checks

Failure cleanup verified OpenSearch Serverless policy ownership and confirmed
Harness deletion, but its role lacked `aoss:GetAccessPolicy`,
`aoss:GetSecurityPolicy`, and `bedrock-agentcore:GetHarness`. Those checks would
therefore retain resources after an `AccessDenied`. The grants now match the
calls, and the synthesized-policy oracle follows destructive calls delegated to
the shared Harness and secret helpers so refactoring cannot make the test
vacuous.

The backend Lambda asset also excludes `uv.lock`; dependency-resolution metadata
is a build input and is not read by any deployed handler.

### Fixed — a delete could skip a resource without telling you

When you delete an agent, the platform works through a list of everything that deployment created
and removes each item by type. If it met an entry of a type it did not recognise, it moved on. That
part is deliberate: stopping at an unknown entry would abandon every resource listed after it, which
is worse. What was wrong is that it said nothing. The entry appeared in neither the list of things
removed nor the list of things that failed, so the delete reported success and you had no way to
learn that something had been left behind in your account.

Unrecognised entries now appear in the result, naming the type, the resource, and the fact that it is
still in your account and needs removing by hand. The delete still finishes rather than stopping, and
still reports success, because every other resource really was removed — but it no longer reports
success silently.

You would only have seen this on a deployment old enough to predate one of the resource types the
platform handles today. The equivalent gap on automatic clean-up after a *failed* deployment was
fixed earlier; this closes the same gap on deletes you start yourself, which is the half that is
visible to you.

### Fixed — deleting an agent could delete a tool function that was never ours

The fix above stopped one installation *overwriting* another's tool code. Deleting had the same blind
spot and a worse ending, because an overwrite can be redeployed from the canvas and a deleted
function cannot.

Tool functions with fixed names — `AgentCoreDynamicTools`, `AgentCoreCustomerSupportTools` — are
shared by every gateway in an account, so the platform reference-counted them: when the last gateway
that used one went away, the function went with it. The count it consulted was the number of invoke
permissions *this platform had added*. A function the platform did not create has none of those, so
the count came out zero the first time it was asked — and zero was taken as permission to delete. The
refcount can only tell you that nothing *of ours* still needs the function, which is not the same as
the function being ours to delete. The same reasoning applied to per-gateway tool functions, custom
tool functions and the knowledge-base tool function, all of which were deleted by name, and function
names are account-global.

To be precise about what was and was not found: no deletion of somebody else's function has been
observed in our own accounts, and we went back through the full audit trail to check rather than
assume. What was missing was the check itself — nothing in the delete path asked who owned the
function, so whether your function survived depended entirely on whether its name happened to
collide.

Ownership is now checked before any tool function is deleted, in all six places that delete one. A
function belonging to a different deployment is kept, and the message names both deployments. A
function with **no** ownership tag is also kept — deliberately the opposite of what the create path
does with an untagged function, because adopting one can be undone and deleting one cannot — and the
log says so and tells you to remove it by hand once nothing uses it. If the tag cannot be read, the
function is kept. Declining to delete is reported as a decision, not as an error, so it does not fail
your teardown.

Two things went with it. Functions are now tagged with the region they are created *in* rather than
the region the deployment ran *from*, which only differed for cross-region deployments but would have
made the platform refuse to clean up its own functions. And the permission to read those tags was
added to the two roles that perform deletions; without it every teardown would have kept every
function and said nothing you were likely to notice.

### Fixed — a second installation could overwrite the first one's tool code

The platform's tool Lambda functions have fixed, predictable names — `AgentCoreDynamicTools`,
`AgentCoreCustomerSupportTools`, and `AgentCore-KBTool-<id>`. Function names are unique per account
and per region, so if a function of that name already existed, creating it failed and the platform
fell straight through to *replacing its code and its configuration*. It never checked whose function
it was. Two installations of this platform in one AWS account therefore fought over the same names,
and whoever deployed last decided what the other one's agents actually ran. That matters more than a
broken tool: replacing a function's code changes what runs under that function's existing execution
role, which may be permitted to do things this platform is not.

Functions are now tagged with the deployment that created them, and ownership is checked before any
replacement. A function tagged by a *different* deployment is refused, with a message naming both
deployments so you can tell which is which. A function with no ownership tag at all — every function
created before this change, including this platform's own — is still adopted, because refusing would
break existing installations, but it now logs a warning and the tag is filled in so the question is
settled after one deploy. If the tag cannot be read at all, the deploy refuses rather than guessing.

Two neighbouring problems went with it. Knowledge-base tool deployment attached a Bedrock access
policy to whatever role already held the expected name, even when the platform had explicitly
declined to modify that role because it could not prove it owned it; it no longer grants permissions
to a principal it does not recognise, and says so. And the permission that writes these ownership
tags is restricted to the two tag keys the platform actually uses, so it cannot be turned around and
used to write the separate opt-in tag that lets an external Lambda function be attached to a gateway.

### Fixed — anyone with a link could read the tool you had just generated

Generating a tool and testing a tool both hand you back an id, and the page then polls that id until
the work finishes. Neither the record being polled nor the poll itself recorded who had asked for it,
so the id was the only thing standing between another signed-in user and your prompt, the tool source
that was generated from it, and the output of running it. An id is not a password, and these ids
travel through logs and browser history.

The record now names the person who created it, and polling it as anyone else returns "not found" —
the same response, word for word, as polling an id that never existed, so the response cannot be used
to discover which ids are real. These records also now expire after a day; previously every tool
anyone had ever generated was kept indefinitely. Tests that were already running when this ships stay
readable so nothing in flight breaks, and they age out within the day.

Alongside this, failures on these two paths no longer quote the underlying error to the browser. They
previously passed through the raw message, which could name internal roles and the AWS account. If
the *code you submitted* is what was rejected, you still get the exact reason — for example
`Blocked import: os` — because that is your own code and you need it to fix the problem.

### Fixed — generated code was tested with fewer restrictions than intended

Testing a generated tool runs it for real, which means the platform runs code that a language model
wrote from your description. Three problems made that looser than it was meant to be.

The safety check that rejects dangerous code inspected the import statements it could see, so code
that assembled the same import at runtime out of smaller pieces passed it. The check now rejects the
building blocks used to do that, rather than trying to recognise each spelling.

The test ran under a single shared role, reused by name across every installation, and adopted
whatever role already had that name in the account. Each deployment now creates and tags its own
role, and if a role of that name already exists without belonging to this deployment, the platform
refuses to touch it and tells you how to resolve it — either rename yours or adopt the existing one
by tagging it.

The temporary function also had no limit on how much of the account's capacity it could take. It now
reserves a small amount, adjustable with `TOOL_SANDBOX_RESERVED_CONCURRENCY`. If the platform is not
permitted to set that limit, it says so in the log and carries on, which is the existing behaviour
and is safe for a single-tenant account.

Most importantly, the generated code now runs in a network of its own that has no route to the
internet. The platform creates that network for you — two private subnets with no gateway out, a
firewall that permits nothing inbound and, outbound, only the one connection needed to write logs,
and a traffic log so the posture can be audited rather than asserted. Nothing about this needs
configuring, and the platform refuses to test a tool at all if the network is missing, explaining
which part it could not find. You can opt out with `TOOL_SANDBOX_REQUIRE_ISOLATION=0` if your
installation cannot place functions in a network; leaving the setting unset is not an opt-out.

The obvious cost of this is that a tool written to call an HTTP API cannot reach it while being
tested. Rather than report such a tool as broken, the platform now tells you what actually happened:
a test case that fails *only* on a connection error comes back with a note saying the sandbox has no
internet access by design and that the tool will work once the agent is deployed. So an offline tool
is tested properly, and a networked tool is reported honestly instead of either being failed or being
quietly given internet access.

That note turned out to depend on *how* the tool reported the failure, which was only visible from a
browser. A tool that let the connection error escape got the note. A tool that caught it and returned
a status code instead — which is the better-written tool, and what the platform's own guidance asks
for — got no note, and the page then offered to rewrite the tool to fix a problem that was the
sandbox rather than the code. One such rewrite made a tool that had failed in 8.7 seconds fail by
timing out instead. The note now also reads what the tool returned, not only the error it raised, so
both shapes are explained the same way and neither is offered a rewrite it does not need.

Two further tightenings to the same sandbox, neither of them visible in normal use. The one
connection the sandbox is allowed to make — writing logs — was permitted to reach the whole of that
service; it is now limited to the sandbox's own log streams and refuses credentials from any other
AWS account. And while the sandbox had no route to the internet, it could still *look up* names,
which is a way to send data out without opening a connection at all. Name lookups are now refused
except for the one service it is allowed to talk to, and refused in a way that fails in about 300
milliseconds rather than after the eight or nine seconds a connection timeout used to take.

That change had a consequence we did not anticipate and found by testing the deployed system rather
than by running the tests. Refusing the name lookup made the sandbox report a *different* underlying
error than it did when lookups still succeeded, and the explanatory note above recognises failures by
what they say. It did not recognise the new wording, so for tools that reach the network without going
through Python's `urllib` the note stopped appearing — and with no note, the page went back to
offering a rewrite for a problem that was the sandbox rather than the code. That is the same defect
described two paragraphs up, reintroduced by the fix below it. The note now recognises both wordings,
the old one is kept as well as the new, and the exact text the deployed sandbox produces in each case
is recorded in the tests so a future change to the sandbox's networking cannot quietly break the
explanation again.

One consequence worth knowing about: the first tool test after a period of inactivity takes around
four minutes, because AWS has to build the network interface for the sandbox before any code can run.
Subsequent tests take seconds. Previously the platform, the API and the page all gave up well before
that point, so the very first test you ran would be reported as failed even though it went on to
succeed. All three now wait long enough, and if a test genuinely does run out of time the message
says what the wait was for and that retrying usually works.

Separately, a custom tool added to a deployed agent took its function and role names from the tool's
name as typed. Two people naming a tool the same thing meant the second deploy wrote its code into
the first one's function. Those names now carry a short marker derived from the tool's owner, so two
users cannot collide, while the same user redeploying still updates their own function in place.
Existing deployments keep the names recorded when they were created, and are removed correctly.

One last thing about testing a tool, which is about what it leaves behind rather than what it can do.
Every test ran in a function with a fresh name, and AWS creates the log for a function the first time
it runs — with no expiry, so the log is kept forever, and deleting the function does not delete it.
On the installation we test against there were 53 such logs, none of them with an expiry, holding
whatever the tools under test had printed. The platform now creates that log itself, before the
function can, with a seven-day expiry and the same ownership labels as everything else it creates. It
has to be done in that order: deleting the log afterwards does not work, because AWS simply recreates
it. Logs that already exist from earlier tests are left alone — changing them retroactively would
touch logs this installation may not own.

### Changed — the platform's permission to attach policies is now limited to its own policies

Deploying an agent involves attaching AWS-managed policies to the roles the platform creates. The
permission to do that was unrestricted: it named which roles could be changed, but not which policies
could be attached to them. Anything the platform could be induced to attach, it would attach —
including a policy far broader than the two it actually uses. That permission now lists the exact
policies by name and refuses any other. Nothing about deploying or testing changes; if a future
feature needs a third policy, the deploy fails with a permission error naming the policy rather than
silently attaching it.

### Fixed — an agent you had never deployed could be chatted with, and deleted, by anyone signed in

Testing an agent and deleting one both take the runtime's id from the caller. Both looked up your
deployment record to confirm the runtime was yours — but when that lookup came back empty, the check
was skipped rather than failed, and the platform went on to build the runtime's address out of the id
it had just been handed. So a signed-in user who named a runtime this platform had no record of could
chat with it, and could destroy it. Runtimes built outside the platform and runtimes belonging to
another tenant whose record had not been written both fell in that gap.

The same gap opened for runtimes that *were* properly recorded whenever the lookup itself failed —
a throttle or a transient database error was enough — because a failed lookup was treated as "no
record", and "no record" was treated as "no need to check".

A deployment record is now required before either action proceeds. A runtime you do not own and a
runtime with no record give the same "not found" response, so the response cannot be used to work out
which runtimes exist. If the lookup cannot be completed, the request is now refused with a "try again
shortly" message rather than being allowed through: the platform will not act on an ownership
question it could not answer. Four paths were affected, including the one every chat message in the
UI uses and the one that performs the deletion, and all four now behave the same way. Your own
agents are unaffected, including ones whose deployment failed part-way and never recorded an address.

### Changed — deleting an imported agent no longer destroys it

Importing an existing runtime adopts it so you can manage it from the platform; the platform did not
create it. Deleting it previously destroyed the runtime in AWS, which meant adopting something and
then removing it from your list took it away for everyone. Deleting an imported agent now releases it
from the platform and leaves the runtime running. If you do want the AWS runtime destroyed as well,
repeat the delete with `?destroy=true`, and the response tells you so. Agents the platform created
are deleted exactly as before.

### Fixed — an exported deploy script could run commands from whoever designed the agent

The CloudFormation export writes a `deploy.sh` for the recipient to run. One value taken from the
agent's design — the reference to the Secrets Manager secret holding a LiteLLM virtual key — was
written into that script in a form the shell would interpret, and the check in front of it accepted
any characters at all after `:secret:`. A reference ending in `$(…)` therefore became a command that
ran when the script ran. This matters because the export is a zip that gets forwarded to colleagues
and attached to tickets: the commands would have run on the machine of whoever deployed it, with
their credentials, not the designer's.

Two independent changes, either of which is sufficient on its own. Values taken from a design are
now written into generated scripts so that the shell treats them strictly as text, whatever they
contain. And a secret reference must now match the characters AWS actually allows in a secret name,
which is also enforced when the stack is deployed, so it applies to a value supplied at deploy time
as well as one carried in the design. Every other value written into every generated script was
audited the same way, and an automated check now requires each one to be accounted for, so a new
one cannot be added without that question being asked.

Two smaller problems found during the same review are fixed with it. A pinned LiteLLM server name
is sent as a request header on every tool call, and was unchecked: a name containing a line break
produced a stack that deployed cleanly and then failed every call. It is now rejected at export
time, with the offending name quoted so you can find it, and checked at deploy time too. And the
proxy URL now rejects characters that cannot legally appear in a URL. Ordinary values are unaffected
in all three cases — secret references with the usual `/`, `+`, `=`, `.`, `@` and `-` characters,
GovCloud and China partitions, and proxy URLs with ports, credentials, paths, queries and fragments
all continue to work, and are covered by tests.

### Fixed — the platform's own activity log was empty in production

Every routine, successful action the platform took was written at a level the deployed functions
never emitted, so none of it reached CloudWatch. Warnings and errors were recorded normally, which
is why this was easy to miss: the log showed the occasions when the platform declined to do
something and was silent on the occasions when it did it. Affected records include sharing a
workflow with another user, promoting a version into production, rolling production back, approving
or rejecting a human review request, creating and deleting triggers, deleting a user pool, and the
startup facts identifying which environment, region and tables a deployment is using.

The cause was a single missing piece of configuration rather than anything wrong with the messages
themselves, and the messages already carried the details you would want — who acted, on what, and
whether it succeeded. They are now emitted. The level can be changed per deployment with a
`LOG_LEVEL` environment variable on the functions, and it still applies only to this application's
own messages: the AWS SDK's internal logging is deliberately left alone, so raising the level to
`DEBUG` for troubleshooting cannot cause request contents or signing material to be written to your
logs. Every message this change switched on was reviewed first to confirm none of them record a
secret; the ones that mention credentials log only a length or an identifier.

Verified on a live deployment: a log group that had not contained a single such record now receives
them, with the AWS SDK's logging confirmed still quiet.

### Fixed — redeploying a gateway left its previous sign-in credential working

Gateways authenticate with a client id and secret that the deploy creates for them in Cognito.
Redeploying a gateway created a new one and pointed the gateway at it, but never retired the old
one — and because the deployment record was overwritten with the new id, nothing in the system
named the old credential any more. It stayed valid indefinitely, and a teardown could not remove
it because nothing recorded it. A gateway redeployed weekly accumulated a working credential every
week.

A redeploy now deletes the credential it replaces. Only credentials the previous configuration
actually listed are removed, and only from a user pool the deployment can prove is its own or this
platform's shared pool — anything else is left alone and noted in the logs. Verified against live
Cognito: the replaced credential stops working immediately after a redeploy, and the new one keeps
working.

### Fixed — deleting a credential or a user pool was not written to the logs at all

Two clean-up steps that destroy things wrote their confirmation at a level the deployed functions
do not emit, so in practice nothing was recorded: retiring a gateway's previous sign-in credential,
and deleting a user pool that is no longer in use along with every identity in it. The messages
explaining why a deletion was *skipped* were recorded, which made the gap easy to miss — the logs
showed the cases where nothing happened and were silent on the cases where something did. Both are
now recorded. The credential message names the user pool and deliberately does not name the
credential itself.

### Fixed — a gateway redeploy could report success with a sign-in that no longer worked

The same redeploy had to move the gateway onto the new credential and clean up the old user pool.
It did those in the wrong order, and it only logged a warning if moving the gateway failed. Two
consequences: a deploy could delete the user pool and then fail to move the gateway off it,
leaving the gateway pointing at an identity provider that no longer existed; and either way the
deploy still reported success, so the first sign that anything was wrong was every call to the
agent's tools being rejected.

The gateway is now moved onto the new credential first, and only once that has succeeded is
anything from the previous configuration removed. If the move fails, the deploy fails and says so,
naming the gateway and why — and the existing gateway is left exactly as it was found rather than
being cleaned up as though this deploy had created it.

### Fixed — a deploy could overwrite an unrelated IAM role that happened to share a name

The roles this platform creates are named after what you called the thing that needs them — an
agent named `support` gets `AgentCoreRuntime-support`. IAM role names are unique across a whole
AWS account, not per deployment, so that name can already be taken by something that has nothing
to do with this platform. When it was, the deploy carried on: it adopted the existing role,
stamped its own tags on it, and replaced that role's permissions with the ones the agent needed.
Whatever was using the role lost its access, and a later teardown of this deployment would have
deleted it.

A deploy now checks, before changing anything, that the role really belongs to it — either
because this deployment created it, or because it was provisioned as part of this platform's own
stack. If it cannot tell, the deploy stops and says so, naming the role and the two ways forward:
rename the agent so a fresh name is used, or, if the role genuinely is yours from an earlier
deployment, tag it with the values the message gives you and deploy again. A role the deploy
refuses is left exactly as it was found — no new tags, no changed permissions.

Two places deliberately keep reusing a same-named role instead of stopping, because neither
changes the role in any way and refusing would break existing deployments: the shared Lambda role
behind generated tools, and the evaluation role. Both now write a warning to the logs naming the
role, and neither is deleted on teardown unless this deployment can prove it created it.

Roles that previously carried no tags at all — the knowledge-base, gateway and evaluation roles —
are now tagged when created, so the same question can be answered on the next deploy.

### Fixed — a deploy could take over someone else's gateway that happened to share a name

Gateway names, like role names, are unique across a whole AWS account rather than per deployment.
When a deploy found a gateway that already had the name it wanted, it assumed the gateway was its
own from a previous deploy and repointed it at this deployment's sign-in configuration. If the
gateway actually belonged to something else — another deployment of this platform in the same
account, or a gateway built by hand — that gateway immediately began rejecting the tokens its own
callers present, and the configuration it had before was gone, so it could not be put back.

A deploy now establishes that the gateway is really its own before changing anything. It is
accepted if the gateway already runs under the exact IAM role this deploy owns, or if its sign-in
comes from a user pool this deployment created or from this platform's shared pool. Otherwise the
deploy stops, names the gateway and the user pool providing its sign-in, and gives the two ways
forward: rename the gateway on the canvas, or delete the existing one if it is yours. A gateway the
deploy refuses is left exactly as it was found. Which of the two checks allowed an adoption is
written to the logs, so a take-over can be traced after the fact.

This needs no tags on the gateway and no extra permissions, so it works on gateways created before
the check existed, and gateways that use an external OAuth provider rather than Cognito are
unaffected. Verified against a live deployment with a real second gateway: the deploy was refused,
and that gateway's configuration was byte-for-byte unchanged afterwards.

### Fixed — Escape did not close the template gallery or the deploy panel

Both of those panels open over the canvas, and pressing Escape did nothing. The only way out was
to click the dimmed area behind them or the small `×` in their corner. Escape now closes both, and
reopening them afterwards works as before.

Screen readers were also told nothing useful about either panel: neither announced itself as a
dialog, and the `×` button had no name to read out, so it was announced as an unlabelled button.
Both panels now identify themselves as modal dialogs named by their own heading — "Workflow
Templates" and "Deploy & Test" — and their close buttons say what they close.

If you have a delete confirmation open inside the deploy panel, Escape cancels the confirmation
and leaves the panel open, so one keypress cannot dismiss both at once.

Keyboard navigation in these two panels is still not complete: Tab can still move out of an open
panel into the page behind it. That is being addressed separately, across every dialog in the app
rather than in these two.

### Fixed — a dead session showed the full builder instead of saying you needed to sign in

When the app could not work out who you were, it assumed the most permissive answer: every
permission, and administrator status. That was intended for local development, where there is no
sign-in at all, but it also applied to a signed-in session that had broken or expired.

The effect was a complete administrator view whose every action then failed. Nothing was actually
accessible — the backend rejects a request it cannot authenticate regardless of what the browser
believes — but the screen implied the opposite, and gave no hint that signing in again was the fix.

A build with sign-in configured now treats an unresolvable session as having no permissions, which
routes you to the page that reports the expired session. Local development without sign-in
configured keeps full access, unchanged.

### Fixed — an expired session looked like a feature that was switched off

On an agent's Evaluations, Cost, Triggers and Observability tabs, a signed-out or expired session
produced a calm "nothing configured here" panel instead of telling you to sign in again.

These tabs deliberately show an empty state rather than an error when an agent has no data yet —
a newly created agent has no cost history, no triggers and no evaluation config, and an error
banner for each of those would be noise. The check for "no data yet" also treated `401
Unauthorized` as that condition. It isn't one: a 401 only ever means the request was not
authenticated. Nothing in the platform returns 401 because an agent has no data.

So when a session expired, all four tabs reported that the feature was not set up for that agent.
The natural next step is to go looking for a setting to turn on, which is the one thing that could
not have helped. These tabs now show that the session expired and that signing in again is the fix.
Genuinely absent data still shows the quiet empty state, and a caller who is signed in but lacks
permission for a tab still sees the empty state rather than an error, as before.

Sessions renew automatically in the background, so this needed a session left idle long enough for
the renewal itself to lapse — but that is exactly when the message mattered most.

### Fixed — the chat sidebar showed an internal error code instead of saying the session had expired

In the chat experience, an expired session made the agent list show `Agent list failed (401)` with a
loading indicator spinning underneath it that never resolved. The same session expiring mid-conversation
replied `⚠️ Invocation failed`.

None of those three things tells you what happened or what to do. The agent list and the chat both
reported failures without going through the platform's normal error handling, so the clearer
session-expired message above never reached them, and the raw HTTP status ended up on screen. The
spinner appeared because the sidebar could not distinguish "still loading" from "loading failed".

The chat now uses the same error handling as the rest of the platform: an expired session says so and
tells you to sign in again, in the sidebar and in the conversation. The loading indicator disappears
when a load fails instead of running forever. Errors that are genuinely the agent's own — a runtime
that refused a request, for example — still report the agent's message, unchanged.

### Fixed — a failed deployment left its gateway behind and reported it as cleaned up

When a deployment fails part-way through, the platform automatically cleans up whatever it had
already created in your account. For gateways, that clean-up could not finish, and could not tell
that it had not finished.

The delete request itself was accepted. Behind it, AgentCore then makes a further call — using the
platform's own credentials — to remove the internal identity record it created alongside the
gateway, and the clean-up role was not allowed to make that call. Because the denial happens
*after* the delete request has already been answered, nothing failed from the platform's point of
view: the gateway was recorded as cleaned up, while in your account it stopped mid-delete and
stayed there in a `FAILED` state. Its execution role was then deleted out from under it, since
roles are removed later in the same pass than gateways.

So a deployment that failed could leave a permanent, unusable gateway that the platform believed it
had removed. The clean-up role now holds that permission, scoped to the one directory it needs
rather than granted broadly. The same permission was missing for two other resource types the same
clean-up pass deletes — agent runtimes and harnesses — and has been added for those too.

This only affected automatic clean-up after a *failed* deployment. Deleting an agent yourself from
the UI or the API was never affected: that path already had the permission. If you have failed
deployments from before this change, check for gateways in `FAILED` state — the platform's own
delete will now remove them.

### Fixed — the only record that automatic clean-up ran was missing from the logs

The line reporting what automatic clean-up did — `Auto-cleanup completed for <deployment>: N/N
resources` — was logged at a level the deployed functions discard, so it never appeared in
CloudWatch at all. For a failed deployment, the logs held only the failure itself, and the sole way
to establish what had been deleted from your account was to go and look. Three other outcome lines
in that path were invisible for the same reason, including which branch the shared tool-Lambda
release took and whether the resource inventory was empty — and at that level, "there was nothing
to clean up" and "clean-up never ran" were indistinguishable, which is the difference between no
resources left behind and an unknown number.

All four are now logged at a level that survives. For gateways the ratio now means *confirmed
gone* — see the next entry. For every other resource type it still means *deletes accepted*: as
the entry above shows, AWS can accept a delete and fail it afterwards.

### Changed — automatic clean-up now checks that a gateway was really deleted

The count in `Auto-cleanup completed for <deployment>: N/N resources` used to mean "N delete
requests were accepted", which is not the same as "N resources are gone". The entry above is a
case where those two differed: every delete was accepted, and a gateway was still sitting in your
account afterwards.

Clean-up now confirms the outcome for gateways instead of assuming it. After the delete is
accepted it waits briefly — a few seconds at most — for the gateway to actually disappear, and
reports one of three things:

- the gateway is gone: it counts towards the total, as before;
- the delete failed after being accepted: it does **not** count, and an error naming the gateway
  and quoting AWS's own explanation appears in the logs;
- the outcome is still undecided when the short wait runs out: it counts, with a warning saying
  explicitly that the deletion was not confirmed.

The permission fix in the entry above removes the known cause of the second case. This change is
what makes the *next* such cause visible rather than silent, and it is why the reported ratio can
now be relied on for gateways. The other resource types are unchanged: agent runtimes already
report a failure of this kind immediately, and a harness delete takes several minutes to complete,
which is longer than a clean-up pass can wait — a harness is therefore still reported as accepted
rather than confirmed.

The function that performs clean-up also had the shortest time limit of any step in the platform
(15 seconds) while being the only one that performs a full teardown. It has been raised, along
with the orchestration timeouts around it, so that a teardown of a large deployment cannot be cut
off part-way — which previously would have left resources behind *and* skipped the line that says
so.

### Fixed — deleting one agent could destroy your identity provider's client secret

If you connect a gateway to your own identity provider, you give the platform a reference to a
Secrets Manager secret holding that provider's OAuth client secret. The platform recorded that
reference in the agent's resource inventory — the list "Delete" works from — as though it had
created the secret itself. It had not: the secret is yours, it lives in your account under a name
you chose, and it is normally the same credential every agent on that provider authenticates
with.

Deleting a single agent therefore deleted that secret, and did so with recovery disabled, so
there was no 7-day window to undo it. Every other agent using the same identity provider would
start failing to obtain tokens, and the only way back was re-issuing the client secret in the
provider and updating each agent.

The inventory now records only the secret a deployment created itself — the per-deployment copy
the platform mints for gateways that use Cognito. A reference you supplied is still read
normally at run time; it is simply never listed as something to delete. Deployments that *do*
mint a secret still record it, including deployments that fail part-way through, so nothing is
left behind either.

If you have already deleted an agent that used your own identity provider, check that the
provider's client secret still exists in Secrets Manager before assuming this did not affect you.

### Fixed — a failed deployment could list an IAM role it never created

The resource inventory a deployment keeps (and that "Delete" works from) named the gateway's
execution role whenever the deployment had a gateway name, which it always does. A deployment
that failed early enough — while setting up your identity provider, or because the deploy role
was not allowed to create IAM roles — therefore listed a role that was never created. Nothing
was destroyed by this: deleting an absent role is treated as already done. But the inventory is
what you read to find out what a failed deployment left behind, and it was answering for
resources that do not exist.

The inventory now lists the role only after AWS confirms it. The reverse case is preserved and
now tested: if the role is created and the deployment fails afterwards — including when your
account's permissions boundary blocks attaching the role's policy — the role *is* listed, so a
later delete still cleans it up.

### Fixed — an external identity provider could redirect your OAuth client secret

If you connect a gateway to your own identity provider, the platform reads that provider's OIDC
discovery document to find out where to request tokens. It then POSTs your OAuth client secret to
whatever address the document names — and it was not checking that address at all.

So the destination for your credential was chosen by the *content of a remote document*, not by
you. A discovery document naming `http://169.254.169.254/…` was accepted, and the secret went
there in cleartext. A compromised or malicious IDP, or anyone able to alter that document in
transit, could collect the secret without touching this platform. The discovery URL *you type*
was already validated; the address it hands back was not.

The token endpoint is now validated everywhere it is used or stored:

- Where the discovery document is read, so a bad document fails your deploy with an error naming
  the document rather than your network.
- Where the platform requests a token, before the secret is even read out of Secrets Manager.
- Where the endpoint is written into your agent's environment, on both deployment paths. Your
  agent sends its own secret from inside the runtime, where none of the platform's checks run, so
  a deploy that would hand it a cleartext or link-local address now fails instead of creating the
  agent.

Non-https and link-local or private addresses are refused; public endpoints resolved through DNS
are refused if they resolve into that same private space. You can additionally pin allowed token
endpoint hosts with `OAUTH_TOKEN_ENDPOINT_HOST_ALLOWLIST`. That is a separate setting from the
discovery-URL allowlist on purpose: pinning your IDP's hostname should not stop the platform's own
Cognito gateways from working.

### Fixed — a leftover IAM role could be reported as a leftover Lambda

When tearing down a gateway, a failure to delete a shared tool Lambda's execution role was
reported under the same label as a failure to delete the Lambda itself. The two fail
independently — the function can be removed cleanly and leave the role behind — so you could be
told to go look at a Lambda that no longer exists while an orphaned IAM role was the actual
leftover. The role now has its own label.

### Fixed — "cleanup complete" could mean resources were left running

When a deploy failed, the automatic cleanup counted every resource in its inventory as successfully
removed — including types it had no code to remove. Three of them: uploaded code bundles,
OpenSearch Serverless collections, and LiteLLM gateway entries. An unrecognised type fell off the
end of the dispatcher and was tallied as cleaned, so a standing OpenSearch Serverless collection
(around $350/month at the two-OCU minimum) could be left running while the log read
`Auto-cleanup complete: 9/9 resources`.

All three types now have real cleanup, and an unrecognised type is no longer counted. It is logged
by name — `Auto-cleanup has NO deleter for type 'x' — left in the account for <id>` — so the thing
you are told is the thing that happened.

To stop this recurring, the two cleanup paths (the one that runs when a deploy fails, and the one
that runs when you delete a deployment) are now compared against each other and against every
resource type the platform records, by a test that reads the source rather than a maintained list.
A new resource type cannot be added to one path only.

### Fixed — failed deploys left your credentials and your gateways behind

The cleanup role could not perform ten of the deletions the cleanup code itself issues. Each one is
a resource a failed deploy kept, and the two that matter most to you:

- **Connector credentials.** A failed connector deploy could not delete the secret holding the
  credential it had just stored, so it stayed in Secrets Manager indefinitely. Six such secrets
  were found in a test account.
- **Gateways.** A gateway cannot be deleted while it still has targets, and the cleanup could
  delete neither the targets nor list them — so any deploy that failed after creating a gateway
  target leaked the gateway *permanently*.

Also fixed for AgentCore Memory (which holds conversation data), OAuth and API-key credential
providers (which hold your OAuth client secret and raw API keys), policy engines and their
policies, harnesses, and guardrails.

The new permissions are delete-and-describe only. Cleanup explicitly **cannot read** a secret's
value — a failure path that could read every stored credential in the account would be a worse
outcome than the leak it repairs. A test enforces that, and the whole permission set is now derived
from the cleanup code in CI, so code that deletes a new resource type without the permission to do
so fails the build.

### Fixed — a resource that had never been created was reported as a cleanup failure

IAM reports an absent role as `NoSuchEntity: … cannot be found`, which the cleanup did not
recognise as "already gone". A role a failed deploy had not got far enough to create was reported
as a cleanup failure and excluded from the cleaned count — the same misleading count as above, in
the opposite direction.

### Fixed — agent code bundles were uploaded and never cleaned up

Every deploy uploads a code bundle of 18–43 MB. Nothing recorded it, so a deploy that uploaded its
bundle and then failed (creating the runtime role, configuring or launching the runtime, setting up
evaluation or JWT auth) left the object in S3 forever. Ten such orphans, about 420 MB, were found in
a single test account's artifacts bucket. The MCP-server path had the same gap for its own bundle.

Both now record the bundle as they upload it, using the exact key the upload used rather than one
rebuilt later from the agent name — deleting a *wrong* S3 key reports success, so a rebuilt key
would have produced a cleanup that claimed to work and removed nothing.

This matters most for cross-account deploys, where the bundle lands in your own artifacts bucket.
The platform's bucket expires `deployments/` after 90 days; your bucket has no such rule unless you
add one, so the inventory record is the only thing that removes the object.

### Fixed — the execution role of a shared tool Lambda was never removed

Gateways share tool Lambda functions, and each function's IAM execution role was deleted by
nothing: an account accumulated one permanent role per tool Lambda ever created.

The role is now removed together with the function — but only when the *last* gateway using that
function releases it, never on the first teardown, so tearing down one gateway cannot break another
that is still running. And only when the platform can prove it created the role, from a tag applied
at creation: these role names are account-global, and a role of the same name that the platform did
not create is left alone and reported rather than deleted.

**One-time operator action if you have been running this platform already.** The ownership tag is
applied when the role is *created*. These roles are account-global singletons, so if your account
already has one from an earlier version it has no tag, it will never be created again, and the new
cleanup will keep skipping it. Teardown now says exactly that rather than something that reads like
the check working:

> Shared tool Lambda role `AgentCoreCustomerSupportLambdaRole` kept (NO OWNER TAG — ownership
> unprovable, so it is never auto-deleted; it predates ownership tagging or belongs to something
> else. To bring it in scope, delete it by hand once no function uses it and the next deploy
> recreates it tagged)

The platform deliberately does not tag the role when it reuses one. A role with this name that
*you* created for something else would then be claimed and eventually deleted, so adopting it is
left as your decision: either delete the role once no function uses it, or tag it yourself.

### Fixed — an error message that told you how to fix the problem was cut off before the fix

A deploy that is refused for a reason you can act on now shows you the whole reason. Failure
messages were truncated at 300 characters, which is shorter than a remedy: the bring-your-own
Lambda refusal described below reached the UI as

> … must be opted in by its owner: tag the function `AgentCoreGatewayTarget=allow` (aws lambda…

naming the tag but not the command that sets it, and not the consequence of skipping it. Measured
on a real failed deploy, where the stored message was exactly 300 characters and ended mid-word.

The limit is now 800 characters, sized from the longest message the platform writes (706 characters
at the maximum Lambda function-name length, since the function ARN appears twice in it). Nothing
about what is *removed* from a message changed: the stack trace, the request id, absolute paths
inside the platform's Lambdas, the platform's own IAM principal and account id, pydantic debug
annotations and anything credential-shaped are still stripped or refused, and a long unrecognised
paste is still truncated. Only the room for an actionable sentence changed.

Found in the deployment record that proved the previous fix: alongside the expected failure, its
`created_resources[]` carried `{"type": "cognito_user_pool", "id": "us-east-1_qiYLOs3Ij"}` — the
**platform's** RETAINed shared gateway-auth pool, which holds the app client of every gateway in
the environment. A deploy that created nothing of the sort had written a manifest row asserting it
owned it, and a manifest row is a deletion capability.

Nothing was destroyed: the teardown path refuses a `cognito_user_pool` twice and independently,
first on a pure env/id comparison (`is_platform_owned_user_pool`) and then on live tag
classification (`classify_user_pool`). Defence in depth held, but the inventory still said the
opposite of the truth.

One root cause. `deploy_gateway`'s exception handler returns a partial inventory, and that
inventory reduced `client_info` to `{provider, user_pool_id}`. The reduction exists for a real
reason — the dict travels into a `RuntimeError` message and a Step Functions failure cause, so a
client secret in it would sit in stack events for 90 days — but it dropped three *non-secret*
teardown handles with it: `shared_pool` (so the recorder took its "a pool THIS deployment created"
branch), `client_id` and `scope` (so neither the abort cleanup nor the manifest could name the app
client or the resource server it really did create, and both were left live in the shared pool),
and `client_secret_ref` (so the minted Secrets Manager copy was orphaned).

It is now an explicit allow-list of the five keys that may be published, chosen over a
copy-and-pop so that a field added to `client_info` later is **not** published by default — it has
to be named. The raw secret still cannot be there, and a test fails if it is put back.

### Fixed — a placeholder function name was handed to `delete_function` by deploys that never created it

`lambda_function_name` was initialised to the literal `"AgentCoreLambdaTestFunction"`, meaning
"this deploy built no tool Lambda" — a name no branch anywhere creates. But every consumer reads
that field as a function to **delete**, and the name is not in the shared-Lambda set, so it
bypassed the reference-counted release and went straight to the hard delete: the abort cleanup
called `delete_function` on it, and both manifest writers turned it into a `lambda` row that every
later teardown re-deleted.

Confirmed in CloudTrail: one failed gateway deploy issued `DeleteFunction` for that name twice,
seven seconds apart, under two different step roles — both returning `ResourceNotFoundException`
only because no function happened to hold the name in that account. It is not a failure-path-only
defect either: any gateway whose targets are all config-driven builds no tool Lambda, so the
success path recorded the row too.

The sentinel is now falsy, the abort inventory no longer re-defaults it, and the cleanup path has
an explicit no-op branch instead of letting an empty `FunctionName` reach the API. One existing
test had pinned the defect — it used the placeholder as its example of a per-gateway Lambda and
asserted the delete — so its example changed to a genuinely per-gateway name and two tests were
added for the falsy and absent-key cases (the latter being every LiteLLM gateway).

### Fixed — a bring-your-own Lambda gateway target could not deploy, and the refusal named no remedy

The canvas accepts any Lambda ARN as a gateway target, but a target naming a function the platform
did not build failed the whole deploy with a raw
`not authorized to perform: lambda:AddPermission on resource: …`, because the step roles hold that
action only on `function:AgentCore*`.

That prefix is a deliberate boundary, not an oversight: `lambda:AddPermission` is permission
management, so an unconditional `function:*` would let any tenant's canvas make this platform
rewrite the resource policy of *any* function in the account. Naming a function is not authority
over it.

The capability therefore exists in exactly one shape — `function:*` **conditioned on the function
carrying the tag `AgentCoreGatewayTarget=allow`**, which only someone who can already tag that
function can set, so owner consent is enforced in IAM rather than only in application code.
`AddPermission` additionally requires that the principal being granted is an
`AgentCoreGateway-*` role, so even a tagged function cannot be opened up to an arbitrary account.
`GetPolicy` and `RemovePermission` are a separate statement carrying the tag condition **only**,
because `GetPolicy` supports no action condition keys at all (read off the AWS Service Reference
feed, not the docs) and a `lambda:Principal` condition would leave the key absent from its request
context and deny the call outright — which is how the orphan-permission prune goes silently inert.
Teardown gets the removal verbs and deliberately no `lambda:DeleteFunction`: a function the
platform did not create is never ours to delete, however it was tagged.

On the application side the denial is now an actionable error naming the ARN, the tag, the exact
`aws lambda tag-resource` command, and why the deploy stops here rather than creating a gateway
whose tool would fail at invoke time. It branches on the error *code*, so an unrelated throttle
still propagates untouched.

### Fixed — a gateway target with no payload deployed "successfully" with the tool silently absent

Three layers of the same defect, found by chasing one live log line:
`Gateway lambda target #0 has no function_arn; skipping` — on a deployment that reported
**success**.

**1. The backend skipped the target instead of failing.** `_deploy_config_targets`
(`services/gateway_deployer.py`) warned and continued for a `lambda` target with no
`function_arn`, an `openapi` target with neither `spec_url` nor `spec_content`, and a `smithy`
target with no inline model. A declared target of a *known* family that is missing its payload
is now fatal. Unknown families and `mcp_server` entries still skip, and that asymmetry is
deliberate: the first is forward compatibility, and the second is deployed by
`_deploy_external_mcp_targets` (where the secret hygiene and SSRF validation live), so acting
on it here would double-deploy.

Raising is only safe because of where the call sits, and that was measured rather than assumed.
Nothing in `gateway_deployer` writes a manifest row; `gateway_step` writes them from a
*returned* dict (`step_handlers/gateway_step.py:304`). The Step-4e call site is inside
`deploy_gateway`'s own `try`, whose handler runs the abort cleanup and returns the partial
inventory, so the gateway, its IAM role and its Cognito pool all come back and stay
recoverable. An exception raised anywhere that escaped `deploy_gateway` instead would leak all
three permanently, with nothing naming them. There is a regression test for exactly that, and
a mutation that drops `**_partial` from the handler's return is caught.

**2. The frontend validated a field the deploy no longer reads.** `validateGatewayConfig`
(`utils/validation.ts`) checked `config.targetType` / `config.targetConfig` — the legacy
*single* target. Once the multi-target editor landed, `resolveGatewayTargets` returns
`config.targets` whenever it is non-empty and ignores `targetConfig` entirely, so on every
multi-target gateway the entire `targets[]` array went out **unvalidated**: the file never
referenced `config.targets` at all. Validation now iterates the resolved list, which is the
only arrangement in which the check and the payload cannot disagree. A blank Lambda ARN is now
a required-field error rather than passing because the *format* check only ran when a value was
present — its `openapi` sibling four lines below had always required the field, and
`createDefaultTargetConfig` hands out `{ type: 'lambda', functionArn: '' }`, so leaving the
canvas field untouched was enough to ship a toolless agent. Incomplete `mcp_server` entries are
rejected for the same reason: `mapMcpTargetToDeployEntry` returns `null` for them and they are
dropped from the payload silently.

**3. The Smithy target family could never deploy at all.** The canvas offered "Use
pre-configured Smithy models (e.g., DynamoDB)". AgentCore's `smithyModel` takes an
`ApiSchemaConfiguration` — an inline or S3-staged Smithy JSON AST, with no notion of a model
*name* (verified against the botocore `bedrock-agentcore-control/2023-06-05` service model) —
and nothing in this platform turns `modelName: 'dynamodb'` into one. Picking it produced a
green deploy with no tool. The option is withdrawn from `TARGET_TYPE_OPTIONS` and a saved
canvas carrying one now fails validation with the reason. The `smithy` type, its editor case
and the backend branch remain, so an API caller that supplies real `model_content` still works;
only the dead choice is gone. Offering it again means shipping a source of Smithy models first.

### Fixed — a gateway on the canvas was deployed, paid for, and granted a secret the generated agent could not read

Two definitions of "this canvas has a gateway" coexisted in `WorkflowExecutor.deploy`
(`services/deployment.py`, the direct path behind `POST /api/workflows/{id}/deploy`). The
deploy decision counted the gateway **node**. The codegen decision required
`"gateway" in connected_tools` — a list nothing derived from the canvas, and the route passes
none, so it was permanently `[]`.

Measured live on runtime `llgw_direct_…-sM5DJrATZS`: deploy `succeeded`, runtime `READY`,
`InvokeAgentRuntime` returned **200**, all four `GATEWAY_*` environment variables present on
the runtime, and the `secretsmanager:GetSecretValue` grant confirmed with
`simulate-principal-policy`. Then `grep -c GATEWAY_URL agent.py` on the artifact actually
deployed to S3 returned **0**. The agent had no gateway, and the generator's own zero-tools
wiring proof could not fire because it is emitted only inside the branch that never ran. A
200 in 2.19s was the tell: no retry loop can complete that fast.

`canvas_connected_tools()` now derives the component types from the canvas and unions the
caller's list, so both decisions read one list. `AgentCoreComponentType` values are exactly
the strings the generator and the per-tool IAM builders test, and an unrecognised string is
ignored by every consumer. A per-canvas Observability node now also takes effect on this
path, which the surrounding comment already claimed.

**The same canvas exposed a second defect, in the generator.** `generate_unified_agent_code`
— the direct path's default generator — implemented *only* the Cognito OAuth transport. It
had no `GATEWAY_AUTH_MODE`, no `static_bearer`, no `x-litellm-api-key`, while
`code_generator.py` implements all three in both of its variants. So fixing the list alone
would have produced an agent that attempted a token exchange against a LiteLLM proxy with no
Cognito to exchange against: empty token, no headers, no tools. It now emits
`_resolve_gateway_key()` (reading the virtual key from Secrets Manager at the moment of use,
never from an env var, because `GetAgentRuntime` returns those in plaintext) and the
`x-litellm-api-key` / `x-mcp-servers` header pair, matching the contract measured against a
real LiteLLM 1.102.0 proxy.

**Re-deployed and re-invoked to prove the hop.** Runtime
`llgw_direct_00b6f404-P7W3FXCc6R`, against an https peer that replays a real proxy's bytes
and records header *names* only — never the key value. The deployed `agent.py` now carries
three `GATEWAY_URL` references, and the peer recorded, inside the invoke window, on the
`GATEWAY_URL` path:

```
"has_x_litellm_api_key": true, "x_litellm_api_key_has_bearer_prefix": true,
"x_mcp_servers": "aws_knowledge", "has_authorization_header": false
```

The peer rejects a wrong key with a 400 *before* it records anything, so a record existing
at all proves the container read the Secrets Manager secret at invoke time — nothing else
holds that value. The agent then failed **loudly** (`Invocation failed (0.464s)`,
`MCPClientInitializationError` from `agent.py:221 tools=list(_get_gateway_tools())`) on the
peer's deliberate "no MCP session" 400, which is the wiring proof working rather than an
agent coming up with zero tools.

Non-vacuity: `tests/test_the_canvas_decides_what_gets_wired.py` drives the real `deploy()`
with the real generator and asserts on the source the generator returned. Re-run against the
pre-fix line it fails with `connected_tools was []` — deliberately not a test of the new
helper, since the helper is not where the bug was.

**A parity gap closed at the same time.** The existing "no plaintext gateway key in the
runtime environment" assertions all drive `runtime_configure_step` — the Step Functions path.
The direct path builds its environment inline in `deploy()` and had no equivalent test, so it
was backed only by the live `GetAgentRuntime` reading. The new file now pins the same
contract for it under moto: `GATEWAY_AUTH_MODE=static_bearer`, the scoped
`GATEWAY_MCP_SERVERS`, the secret ARN, and **no** `GATEWAY_API_KEY` and no `COGNITO_*`.
Injecting the key in plaintext, dropping the LiteLLM branch, or dropping the server scoping
each fail it.

### Fixed — every non-Bedrock model provider deployed green and could never start

Thirteen providers are selectable (`StrandsModelProvider`, and `RuntimeConfig.model_provider`
accepts all thirteen). Twelve of them are not Bedrock, and **none of the dependency bundles
carried a model-provider SDK**. Nothing pip-installs at container start (`requirements_txt` is
`""`), and per ARCC `cnt_Vsqr5LAdJVd1Il` / `cnt_mYvaeqAKMTfIlZ` it must not — third-party
packages have to be served from infrastructure we control.

Measured live: an OpenAI agent deployed `succeeded`, then the container died at
`from strands.models.openai import OpenAIModel` with `ModuleNotFoundError: No module named
'openai'`. AgentCore surfaces that as **"Runtime initialization time exceeded. Please make sure
that initialization completes in 30s"**, which reads like a cold-start budget problem and is not
one. That misattribution is why this survived: the platform reported success, and the only
symptom pointed at the wrong cause. Both deploy paths were affected.

`scripts/install-agentcore-deps.sh` now builds one `provider-<extra>.zip` per `strands-agents`
extra as a **delta** on top of `strands-mcp.zip`, and a canvas ships only the providers it uses
— the 30-second budget is spent on every cold start, so a whole tree would tax agents that need
none of it. **Eight bundles, not twelve**: groq, deepseek and writer all emit `OpenAIModel` and
together emits `LiteLLMModel`, so four providers share another's bundle. The value is decided by
the import line `_get_model_init_code` actually returns, not by the provider's name, and
`test_provider_sdks_ship_in_a_bundle.py` derives the mapping from that function so the two
cannot disagree. Bundle size now has a test of its own: the CDK `BucketDeployment` Lambda
unzips into `/tmp`, so the ceiling is its ephemeral storage, not any S3 limit — 131.8 MiB asset
plus 131.8 MiB extracted against the 1024 MiB configured, with
`infra/tests/test_agentcore_deps_fit_in_ephemeral_storage.py` measuring it. The cliff is the
512 MiB default, and hitting it fails the whole platform deploy.

**Three more defects in the same area, each a silent substitution.**

`llamaapi` had no branch in `_get_model_init_code` at all, so it fell through to the Bedrock
fallback: a canvas that selected Llama API deployed a **Bedrock** agent with a Llama model name
passed as a Bedrock model id, and was granted and handed a provider API key the emitted code
never read.

A non-Bedrock model id was being rewritten as a Bedrock cross-region inference profile. Measured
through the real API: an OpenAI agent came up with `MODEL_ID` = **`us.gpt-4o-mini`**. A geography
prefix is a Bedrock inference-profile namespace and nothing else, and the mangling is
unrecoverable downstream because only the Bedrock and SageMaker branches read `MODEL_ID` from the
environment — every foreign-catalog branch embeds the string into the generated module as a
literal.

The provider gate read only the parent's provider, while `code_generator` builds one model per
sub-agent from `multi_agent_config["agents"][*]["modelProvider"]`. A Bedrock parent with one
OpenAI sub-agent was therefore denied a key — a green deploy whose sub-agent 401s on its first
model call. `canvas_model_providers()` now returns the whole canvas's list, parent first, and is
the one input to both the key gate and the bundle selection.

**And a stringification defect that made the bundle fix a no-op on one path.**
`deployment_models.RuntimeConfig` declares `model_provider` as a `str`;
`components.RuntimeConfiguration` declares it as `StrandsModelProvider`; the two deploy paths
pass one each. `StrandsModelProvider` is a `str, Enum` and **not** a `StrEnum`, so `__str__` is
`Enum`'s and a plain `str()` yields `"StrandsModelProvider.OPENAI"` — matching no key in
`PROVIDER_STRANDS_EXTRA`. The direct path selected no provider bundle at all while
`needs_provider_api_key` still said `True`: a green deploy, a granted key, and a container that
cannot import. Normalization now happens inside `canvas_model_providers`, not at each call site,
because a caller that forgets gets silence rather than an error.

`PROVIDER_PACKAGES` — the older hand-maintained list, used only to write a `requirements.txt` for
a human reading the export — had drifted on five entries and is corrected: `google-generativeai`
→ `google-genai` (what strands actually imports), groq/writer/deepseek → the OpenAI SDK their
generated code really uses, `sagemaker` → `mypy-boto3-sagemaker-runtime` (imported at module
scope, *not* under `TYPE_CHECKING`), and `llamaapi` → `llama-api-client`.

### Security — deleting a gateway left a usable Cognito app client, secret and all, plus its resource server, in the shared pool

Every torn-down deployment whose gateway used the platform's shared Cognito pool left its own app
client behind, with its client secret still mintable and the gateway's invoke scope still attached.
Measured on a live pool after teardown: client `agent-gateway-client`, secret present, scope
`agentcore-agent-gateway/invoke`.

Two correct halves with a dead path between them. `cleanup_gateway_resources` does delete that
client — and its caller gates the entire function on `not manifest_used`, so on every deployment
that wrote a manifest it never ran. The manifest teardown, meanwhile, had nothing to delete:
`_record_gateway_resources` deliberately records no deletable pool row for the shared pool (correct
— it holds every other gateway's client and a >381s hosted domain) and its comment claimed
`cleanup_gateway_resources` covered the client. The comment asserting the coverage is what kept it
invisible.

The shared-pool path now records a `cognito_app_client` row carrying the client id **and** its
pool, and both teardown dispatchers gained the matching arm: the delete path in
`deployment_handler._delete_managed_resource`, and — the half no report reached —
`status_update_step._cleanup_resource`, the hand-mirrored copy that runs when a deploy *fails*.
Fixing only the delete path would have left the client leaking on every failed deploy. Both
priority maps place the client before the pool (an owned pool's delete would otherwise race its
clients) and after the gateway (its `customJWTAuthorizer` pins the client id).

`cognito-idp:DeleteUserPoolClient` was added to the `status_update` step role, which had
`DeleteUserPool`, `DeleteUserPoolDomain` and `DescribeUserPool` but not this one — the code arm
would have failed with AccessDenied and the leak would have survived the fix. A test asserts the
grant against the CDK source that mints the role, so the arm and the grant cannot drift apart.

A client id alone is not authority to delete: the container is re-verified live with
`classify_user_pool`, and a row whose pool cannot be proven platform-owned is skipped as protected
rather than guessed. The client arm accepts `SHARED_EXACT` where the pool arm refuses it — the
shared pool is exactly where this deployment's own client lives, so one shared predicate would have
to be wrong for one of the two. With the shared-pool environment variable unset the guard fails
**closed**, so the worst case is a skipped client, never a stranger's.

The resource server (`agentcore-<gateway>`) is now deleted too, gated on a co-residency check. It
holds no credential — it is a scope definition — but it accumulates in the shared pool forever, and
its identifier is derived from the gateway *name*, which is not proof of ownership:
`create_resource_server` treats AlreadyExists as success, so two deployments that picked the same
gateway name **share one resource server**, and deleting it on the first teardown would revoke the
co-resident gateway's scope. So `resource_server_is_unused` proves no client can still be holding
it first, using `ListUserPoolClients`, which returns client ids and *names* only and never a
secret. `DescribeUserPoolClient` would answer the question directly but authorizes on the *pool*,
so the only workable grant reads every gateway's client secret — which is why the check is built
out of the weaker call. It paginates, because a co-resident sitting on page 2 of an unpaginated read
is invisible and "no clients found" is precisely the answer that authorizes the delete: a truncated
list would be a *wrong delete*, not a missed one. Any list error, or an identifier without the
expected `agentcore-` prefix, returns "still in use" and the resource server is kept.
`cognito-idp:DeleteResourceServer` and `cognito-idp:ListUserPoolClients` were added to both teardown
roles; the deployment role had **neither**, so even the pre-existing `cleanup_gateway_resources`
resource-server delete was already inert there.

Verified against the real leaked resources in the live pool, not in a unit test. The app client: it
is gone, a co-resident client created in the same pool first survived, a re-run reports "already
gone" rather than a teardown failure, the failure-path arm deleted a client for real, and another
stack's pool was refused before any API call with its own client verified untouched afterwards. The
resource server: with a co-resident `agent-gateway-client` present the live delete was refused as
protected and the resource server confirmed still there; with it removed the same call deleted the
real orphan; a second call reported "already gone"; and a foreign pool's resource server was refused
before any write and confirmed intact.

### Security — a caller-supplied `litellm_api_key_ref` was dereferenced without proving ownership

`litellm_api_key_ref` is documented as "the ARN this platform minted on a previous deploy of this
canvas", and on redeploy the control plane read whatever ARN the request named. A prefix check is
not ownership: every tenant's connector secrets share that prefix. It is now validated with the
owner-scoped `is_own_connector_secret` before either the control plane or — now that the key
travels by reference — the deployed agent reads it, and it fails **closed** with an actionable
message telling the caller to supply the key itself. Falling back to reading it anyway would have
meant either another tenant's secret or a bring-your-own secret the runtime role could not read
at invoke time regardless: a green deploy whose gateway is dead on its first tool call.

Both model-provider and gateway keys now travel as `PROVIDER_API_KEY_SECRET_ARN` /
`GATEWAY_API_KEY_SECRET_ARN` and are resolved by the agent at the moment of use, never held in a
runtime environment variable — `GetAgentRuntime` returns those in plaintext to any caller who can
describe the runtime, and every Step Functions Task re-emits them into the execution history for
90 days. ARCC `cnt_n8LpZcqYi2t3I2`. `runtime_key_grant_targets()` is the single source of truth
for the two ARNs a runtime role may read, for the same reason its sibling
`client_secret_grant_targets()` is: three deploy paths need them and a drift between them is
invisible.

### Fixed — the CloudFormation export accepted ten configuration fields and discarded all ten

Measured, not read. Generate a bundle, vary exactly **one** `DeployRequest` field, compare
the seven text artifacts byte for byte: `identity_config`, `connectors`,
`external_mcp_servers`, `guardrails_config`, `observability_config`, `resource_tags`,
`tag_profile`, `target_account_id`, `target_region`, `version_description` — every one
byte-identical to an export that never set it. All ten are declared fields on a model whose
`model_config` is `extra="forbid"`, so they were not stray keys being ignored; they were
part of the accepted API contract. An export aimed at one account was byte-identical to one
aimed at another, and a caller who configured guardrails got a template with none.

Two controls, because the first version of this measurement reached the **opposite**
conclusion and was wrong. `cfn_provider_code` is a ZIP with an embedded mtime, so it differs
between two generates of identical input and made all ten look honoured — only the text
artifacts are compared now, with a determinism test pinning that they are stable run to run.
And `data_retention_policy="Delete"` is the positive control: it demonstrably *does* change
the template, so "byte-identical" means dropped rather than "the generator ignores
everything".

Resolved as **eight refused, two implemented**, and that split is a measurement rather than
a preference. `ResourceTagFields.tsx` resolves each tag from user input → selected profile →
the tag policy's own `default_value`, so in any organisation that sets a policy default,
`resourceTags` is non-empty on every export *with no user input at all*. Refusing it would
have turned every CloudFormation export in that organisation into an HTTP 400 — breaking the
feature for exactly the regulated customer who needs it. (On a default install all three
seeded policies have `default_value: null` and no profiles exist, which is why the trap was
invisible from here.) So governance tags are now emitted, and the other eight fields fail
the export with a message naming the field and what to do about it.

Refusing is the honest half and it had to land before faithfully emitting ten more config
dimensions. An export that quietly discards part of your configuration is worse than an
error, because the customer deploys it believing they got what they configured.

**The exception type was the whole fix on the refusal side.** The route passes exactly one
type through with its message intact — `CfnExportUnsupportedError` becomes a 400 whose
detail is the message — and collapses everything else into
`HTTPException(500, "Internal server error")`. The guard as first written raised
`ValueError`, so every carefully worded sentence it produced would have reached the browser
as an opaque server error with no way to learn which setting to remove. Found by reading the
route: the unit test caught `Exception` broadly and passed either way.

**Tag shapes are not guessable, and a wrong one does not fail the export** — it fails at
`CreateChangeSet` in the customer's account, on a property they never typed. Taken from
cfn-lint's bundled registry schemas, the same ones CloudFormation validates against:

| Resource type | Property | Shape |
|---|---|---|
| `AWS::BedrockAgentCore::{Gateway,Memory,Runtime,RuntimeEndpoint}` | `Tags` | map |
| `AWS::BedrockAgentCore::{PolicyEngine,OnlineEvaluationConfig}` | `Tags` | **list** ← same service |
| `AWS::Bedrock::KnowledgeBase` | `Tags` | map |
| `AWS::Bedrock::Guardrail` | `Tags` | list of `{Key,Value}` |
| `AWS::Cognito::UserPool` | **`UserPoolTags`** | map |
| `AWS::IAM::Role`, `AWS::Lambda::Function`, `AWS::Logs::LogGroup` | `Tags` | list of `{Key,Value}` |
| `AWS::S3Vectors::{VectorBucket,Index}` | `Tags` | list of `{Key,Value}` |

There is no rule to derive, because **AgentCore is not internally consistent with itself**:
four of its types take a map and two — same service, same namespace — take a list. Every row
above was read out of that type's own schema.

A resource type the generator gains later is either tagged or recorded as untaggable with a
reason — it cannot quietly become a resource that escapes the organisation's tagging policy.
Tags the generator sets itself win over a caller tag of the same key, and a tag the service
would reject (`aws:` prefix, illegal characters, over-length, more than 50) is refused with
the reason rather than emitted into a stack that fails partway through creation.

One deliberate asymmetry with `/api/deploy`: there, a tag-store failure is caught and logged
as non-fatal, because blocking a deploy over the tagging sidecar is worse than deploying
untagged. On the export it is fatal (503). The artifact is a file the customer keeps and
deploys later, possibly long after we are out of the loop, so handing them a stack silently
missing the governance tags they asked for — with the warning only in *our* log — is the
exact failure this whole guard exists to remove.

Verified with cfn-lint over the emitted template: zero E-level errors, **plus a positive
control** proving that claim means something — swapping the map and list shapes produces two
`E3012` errors. Scoped to one region on purpose; `lint_all` checks every region cfn-lint
knows and AgentCore does not exist in about twenty of them.

**The table has to be complete, not merely correct**, and the first version was not. The
tagging pass fails *closed* on a type it does not recognise — right, because the alternative
is a resource silently escaping the tagging policy — so an unclassified type does not yield an
untagged resource, it yields **HTTP 400 on the whole export**. The first version classified 9
of the 26 types the generator can emit and every test was green, because
`AWS::BedrockAgentCore::GatewayTarget` is emitted only when a gateway *and* an MCP server are
both enabled and the tests exercised one component at a time. All 26 are now classified (14
taggable, 8 recorded untaggable with a reason, 4 `Custom::`), and the guard that does not
depend on reaching a branch parses the generator's own source for every `"Type": "AWS::…"`
literal and asserts each is classified.

* `backend/src/app/services/cfn_template_generator.py`,
  `backend/src/app/deployment_handler.py`
* **new** `backend/tests/test_the_export_refuses_what_it_cannot_express.py`,
  **new** `backend/tests/test_the_export_tags_every_resource_it_can.py`

### Fixed — the streaming Function URL returned HTTP 200 with a body no client can parse

`TestRuntimeStreamUrl` had never been driven. Signed once, it answered HTTP 200 with
`Content-Type: text/event-stream` — and a body that was the handler's own
`{"statusCode": 200, "headers": {…}, "body": "data: …"}` envelope, so an SSE parser found **zero
frames**. Three reasonable facts compose into it: the function is a managed **python3.12**
runtime, where Lambda response streaming (a Node.js managed-runtime feature) never passes a
writable stream, so `lambda_handler` always takes its buffered branch; that branch returns the
API-Gateway envelope, unit-tested as correct since June; and the URL was
`InvokeMode=RESPONSE_STREAM`, which does *not* unwrap that envelope — it reads the leading JSON
for `headers`, which is exactly why the response looked healthy.

Reproduced in a 20-line throwaway Lambda before touching the product, then measured under both
modes with identical code: `RESPONSE_STREAM` leaks the envelope; `BUFFERED` unwraps it into clean
`data:` bytes and still returns HTTP 200 after 45.4s and after 240.6s — so it keeps the entire
reason this Lambda exists, outliving API Gateway's hard 30s integration cap. Incremental
token-by-token delivery needs a Node.js handler or a custom runtime and was never happening
under `RESPONSE_STREAM` either.

Nothing reached a user: the output and its SSM parameter have **zero consumers** — the comment
claiming `deploy.sh` read the output into `VITE_STREAM_URL` was false, and that absence of a
consumer is why no one ever saw the body. Two other stale claims corrected against measurement:
the handler's docstring said `auth_type=NONE` (the call says `AWS_IAM`), and a "two gates, not
one" claim over-credited an in-handler Cognito verify that a SigV4 caller can never reach —
measured, a request bearing a *valid* Cognito access token and no signature gets HTTP 403 from
AWS before any Python runs, because SigV4 occupies the `Authorization` header.

The guard is a **pairing**, because neither half is wrong alone and the mismatch spans two files:
the new suite reads the deployed `InvokeMode` out of the CDK source with `ast` and asserts it
against the shape the handler actually returns. All five mutations caught, including the vacuity
one where `add_function_url` is renamed and every `InvokeMode` assertion would otherwise pass on
an empty search. Live after deploy: the probe went from 4/8 to **8/8**, and 23 pre-existing
`stream_handler` tests plus 162 infra tests stayed green — they had all been green throughout the
outage, which is the point.

* `infra/stacks/platform/lambdas.py` (`invoke_mode=BUFFERED`),
  `backend/src/app/stream_handler.py` (docstrings only)
* **new** `backend/tests/test_stream_url_delivers_parseable_sse.py`;
  `backend/tests/test_stream_handler_auth.py` (marks the dormant streaming-branch test as
  dormant, so its coverage is not read as evidence the product streams)

### Fixed — the invoke route the UI actually calls had no tenant isolation

`POST /api/test-runtime-stream` is what `DeployPanel` and `services/api/chat.ts` call for every
chat turn, so it is the primary invoke path in the product. It enforced nothing: driving it live
against the deployment, a second Cognito user POSTed another tenant's `runtimeId` and received
that agent's real answer in SSE token frames. The sync route `POST /api/test-runtime` refused the
identical request, and `stream_handler._stream_invoke` — the Lambda Function URL twin, which is
`AWS_IAM`-authed and not yet wired to the browser — enforces the rule too, with a comment claiming
it is "identical to handle_test_runtime / delete". The enforced copies were the ones nothing calls.

The omission was in the signature, not the body: the route took only `request: TestRequest`, so
there was no caller identity in scope to compare an owner against. Reading the route never looked
like a check was missing. Runtime ids are not secrets — they appear in the canvas, in exports and
in logs — so this was "invoke any agent in the account, on its owner's bill, with its tools and
its gateway credentials".

Two more defects fell out of the same probe:

* **A refusal was reported as a crash.** The sync route's tenant-isolation 404 arrived as HTTP 200
  with `{"success": false, "error": "An internal error occurred…"}`, because `HTTPException` is an
  `Exception` and the route's own broad `except Exception` caught the 404 it had raised — while
  `logger.exception` filed every routine authorization denial as an unexpected error with a
  traceback. Now re-raised ahead of the broad handler, the idiom the rest of the file already uses.
* **`GET /api/deploy/{id}` served any deployment record to any authenticated caller** — 32 fields
  including the owner's Cognito `sub`, the handle every other ownership check compares against.
  Found by the new structural guard rather than by reading, then confirmed live.

All three are the same shape, so the guard is structural rather than per-route: every route in
`deployment_handler` whose source resolves a deployment record from a caller-supplied id must be
able to identify its caller. The covered route list is pinned by name, so a renamed helper fails
the test instead of silently checking nothing, and a new such route fails until someone classifies
it. 18 tests, six mutations, all caught — including two vacuity mutations, because a route that
refuses *everybody* satisfies every "refuses a non-owner" assertion.

* `backend/src/app/deployment_handler.py`
* **new** `backend/tests/test_stream_route_tenant_isolation.py`
* `backend/tests/test_internal_state_fields_are_not_served.py` (status route signature)

### Fixed — the secrets baseline covered none of this branch's 29 new files

A third failure mode in the same hook, and silent like the second one. `detect-secrets scan
<directory>` enumerates through git, so an **untracked** file is invisible to it. Regenerating
`.secrets.baseline` from the git root therefore walked the tree and skipped every new file on this
branch, producing a baseline that looked complete — while `detect-secrets-hook`, which scans
whatever paths pre-commit hands it, flagged 19 findings across 10 of them. Committing would have
gone red with a "correct" baseline, which is the same shape as the bug fixed on 2026-09-19: a
crash is loud, a baseline that matches nothing looks exactly like a real finding.

The tell is a contradiction you have to go looking for: `detect-secrets scan <the one file>`
reports the finding and `detect-secrets scan <its directory>` reports nothing. `git ls-files
--error-unmatch <file>` then says "did not match any file(s) known to git", which is the whole
explanation. Passing the untracked paths explicitly alongside the directory does **not** help —
detect-secrets still resolves them git-first.

Regenerated behind `git add -N` (intent-to-add: tracked in the index, no content staged), then
`git reset --` to restore the exact prior index state, because leaving intent-to-add marks in
place makes a plain `git commit` a footgun. All 19 additions were read individually before being
recorded, per the standing rule that a detect-secrets failure is investigated as a real finding
rather than waved through: AWS's own published example key ids (`AKIAIOSFODNN7EXAMPLE`), moto's
`"testing"`, deliberately-fake markers (`notarealclientsecret-…`, `sk-litellm-v1-abc123`), secret
*references* rather than values (`agentcore-connector/acme/abc123`, `ext-idp/secret`), three W3C
and X-Ray trace ids, and a CloudWatch log-group name. No real credential. Set-diffed
`(file, type, hashed_secret)` before and after: 19 added, **0 removed**.

* `.secrets.baseline` (88 → 107 entries)

### Fixed — the deployment API returned the platform's own Step Functions execution ARN

Found by driving the error sanitizer against a real failed deployment instead of trusting its
unit tests, which were green and mutation-verified. The defect was one field away from anything
they looked at.

`POST /api/deploy` with `knowledgeBaseConfig: {kbMode: "existing", knowledgeBaseId: "ZZZZZZZZZZ"}`
fails on a read before creating anything, which makes it the cheapest real failure probe there
is. Polling `GET /api/deploy/{id}` confirmed the sanitizer fix live — `error_details` came back as
exactly `Knowledge Base ZZZZZZZZZZ not found`, with the Lambda failure envelope, `stackTrace`,
`requestId`, the `/var/task/` path, the line number and the module name all gone and the one
actionable sentence intact. Scanning the **whole** response, though — the browser is handed the
whole document, not one field — turned up:

```
"execution_arn": "arn:aws:states:us-east-1:<account>:execution:<stack>-deployment:deploy-…"
```

The platform's account, its state machine name and its region: "Internal system components" in
ARCC `cnt_94E30Xo4RZHtSJ`'s list of what a response must not contain. The same argument the
sanitizer already makes about principal ARNs applies verbatim — the execution is the platform's,
never the caller's, and no caller holds IAM permission on it, so disclosing it cannot help them
fix anything.

**No test could have caught it, and that is the interesting part.** `execution_arn` was never
added to a response. It was added to `DeploymentState` for the platform's own bookkeeping, and
because **the API response *is* the storage model** — three routes do
`state.model_dump(mode="json")` — it was published the moment it existed, with nothing to object.
Three surfaces: `GET /api/deploy/{id}`, `GET /api/deployments`, and the `POST /api/deploy` 202.
Nothing consumed it: zero frontend references, nothing in the backend outside the two lines that
write it.

Fixed at the serialization boundary rather than by deleting the field, which would destroy the
handle an operator needs to find the execution in the console. `INTERNAL_ONLY_STATE_FIELDS`
excludes it on both read routes; on the 202 it had to come off `DeployResponse` entirely, because
a declared field cannot be excluded at a call site.

The guard is deliberately **not** "`execution_arn` is absent" — that only re-tests the field
already found. It is "the public allow-list plus the internal list are exactly the model's
fields", so the next bookkeeping field fails the suite until someone classifies it. Two oracles,
because either alone is insufficient: the pin catches a new model field but would pass if a route
quietly dropped its `exclude=`; driving the real route functions catches that but only knows about
one field. Plus a vacuity guard — every other assertion still passes if `INTERNAL_ONLY_STATE_FIELDS`
is emptied and everything declared public, which is precisely the defect.

Five mutations, all caught: `exclude=` dropped on the status route; dropped on the list route; a
new field added to `DeploymentState`; `INTERNAL_ONLY_STATE_FIELDS` emptied; `execution_arn`
reinstated on the 202.

* `backend/src/app/models/deployment_models.py`, `backend/src/app/deployment_handler.py`
* **new** `backend/tests/test_internal_state_fields_are_not_served.py`

### Fixed — account-id redaction rewrote the caller's own deployment id

Self-inflicted, and found by the same live probe. A 12-digit run is both an AWS account id and a
uuid4's final group, so `\b\d{12}\b` did this:

```
Deployment 'a3f19c22-7b41-4de8-9c02-48192037461f' not found
  ->         …-9c02-[redacted-account]' not found
```

The caller cannot match the response against the id they just requested — in the one message
whose entire purpose is to name which id was not found. Over-redaction rather than disclosure, but
measured over 200 000 uuid4s it hits **710, or 0.355% (~1 in 281)**: often enough to matter, far
too rare to notice by hand.

The narrow fix and the safe fix point opposite ways. Excluding every hyphen-adjacent 12-digit run
is simpler, and would stop redacting the account id where it most often appears — the tail of a
generated resource name, `…-frontend-us-east-1-<account>`. A fixed five-character hex lookbehind
threads both, because `st-1-` is not four hex characters:

```python
(re.compile(r"(?<![0-9a-fA-F]{4}-)\b\d{12}\b"), "[redacted-account]")
```

Two tests, one per direction, because the UUID test alone is satisfied by deleting the rule
outright. Verified against all six shapes: both UUID cases preserved; a bare account id in prose,
an account inside a resource ARN, a bucket name ending in the account, and the measured principal
ARN all still redacted. Reverting the lookbehind fails exactly the one new test.

* `backend/src/app/services/error_sanitizer.py`,
  `backend/tests/test_error_details_never_leak_internals.py` (79 tests, was 77)

### Fixed — an MCP-server canvas exported a template CloudFormation rejects outright

The strongest form of the failure above, and it was live. A canvas with an MCP Server node and
no gateway returned HTTP 200 and a downloadable bundle whose `template.yaml` could not deploy
at all. Measured on a real export, followed through the pre-signed download and opened as the
customer's browser would:

| cfn-lint | Site | Dangling reference |
|---|---|---|
| `E6101` | `Outputs.McpServerRuntimeId` | `GetAtt McpServerRuntime` |
| `E1010` | an IAM policy statement | `GetAtt McpCognitoUserPool` |
| `E3005` | `AgentCoreRuntime.DependsOn` | `McpServerGatewayTarget` |

**Two different conditions for one feature.** The three MCP resources are emitted under
`has_mcp_server and has_gateway`; four reference sites are gated on `has_mcp_server` alone. So
the template referred to three resources it never created, and CloudFormation rejects that at
validate/`CreateChangeSet` time — the customer downloaded a bundle that was dead on arrival.

Refused rather than fixed by emitting the resources anyway, for the reason the adjacent
LiteLLM refusal already gives: the MCP runtime is published to callers *as* a `GatewayTarget`,
so with no gateway to attach to it is a server nothing can call. And refused rather than
silently dropping the node, because a silent drop is the failure mode the refusal guard exists
to remove. The error names the MCP node and says what to do.

**No test in the repo had ever linted the artifact we ship** — that is how a whole component
combination shipped an undeployable template. There are now two independent oracles over eight
component combinations: a tool-free resolver that checks every `Ref`, `GetAtt`, `DependsOn`
and `Fn::Sub` `${…}` against the template's own Resources, Parameters and pseudo-parameters,
and cfn-lint, which knows property types and tag shapes the first cannot. `Fn::Sub` counts,
because `${McpCognitoUserPool}` in a Sub string is a reference exactly as much as a `Ref` is —
and the shipped defect had one. Each oracle has a positive control: four injected ghost
references, one per reference kind, must each be caught; a `Sub` that declares its own local
variable must *not* be flagged; and cfn-lint must object to a `GetAtt` on a resource that does
not exist.

* `backend/src/app/services/cfn_template_generator.py`
* **new** `backend/tests/test_the_exported_template_can_actually_deploy.py`

### Security — a custom resource's UPDATE could take over another deployment's OAuth provider

`Custom::OAuth2CredentialProvider`'s Create already refused to adopt a same-named credential
provider bound to a different OAuth client. Update had no such check, so the weaker path was
reachable from inside the stronger one's own resource: point `ProviderName` at a name another
deployment is using, CloudFormation sends an Update, and the handler overwrote that
provider's client id, discovery URL and secret with ours. The victim's gateway target then
minted tokens against *our* Cognito pool — and because the returned `PhysicalResourceId`
became the victim's ARN, our eventual stack Delete deleted **their** provider.

`PhysicalResourceId` is now treated as the authority on what the resource owns, because
CloudFormation assigned it from our own Create. A requested name that does not match the
name inside that ARN is not an in-place update at all — it is a rename, which CloudFormation
already models as a replacement — so it routes to Create, which performs the ownership check
and refuses a foreign provider.

The check is on the **name**, deliberately not on the client id: a legitimate redeploy
recreates the Cognito app client, so the client id *does* change on an honest rotation, and
requiring it to match would reject the very case this handler exists to serve. Ownership
comes from the physical id; the client id is only the discriminator Create falls back on,
because Create has no physical id yet.

* `backend/src/app/services/cfn_provider/handler.py`
* `backend/tests/test_cfn_provider_handler.py::TestAnUpdateCannotTakeOverAnotherDeploymentsProvider`
  — the refusal plus a vacuity guard that a same-name update is still an ordinary in-place
  update, so "refuse a takeover" cannot degrade into "refuse every update".

### BREAKING, Security — an agent could read every co-resident deployment's gateway credential

A deployed agent re-reads its gateway's OAuth client secret at the moment of use rather
than carrying it in an environment variable (`GetAgentRuntime` returns a runtime's
environment in plaintext). For a Cognito gateway it did that with
`cognito-idp:DescribeUserPoolClient`. **That action has exactly one IAM resource type,
`userpool`, with no granularity below it** — so a grant that lets an agent read its own
app client's secret also reads every other app client's secret in the same pool.

In the default `shared` identity mode that is one pool holding every deployed gateway's
client. Measured live, from the shared runtime role: it holds `ListGateways`/`GetGateway`
on `*`, `get-gateway` returns the target's `allowedClients` and its pool id, and
`describe-user-pool-client` then returns that client's `ClientSecret` — a complete
cross-tenant path with no step requiring anything the role did not already have.

The dedicated-pool mode was **not** safe either, and this is the part that looked fine on
review. The grant was `userpool/*` conditioned on `aws:ResourceTag/AgentCoreStack`, which
reads as "only pools this deployment created". The tag's *value* is
`{project}-{env}-{region}` — it names the **stack**, and every deployment in the stack
stamps the identical value. A condition that looked narrow authorized exactly the same
cross-tenant read as an exact-ARN grant would have.

Cognito offers nothing narrower, so the credential moved instead of the grant:

| before | after |
|---|---|
| `create_user_pool_client(GenerateSecret=True)`, secret left in Cognito | secret read out of the **create response** and written to its own Secrets Manager secret under `agentcore-connector/` |
| runtime env carries `COGNITO_USER_POOL_ID` | runtime env carries `OAUTH_CLIENT_SECRET_REF` (and *not* the pool id — the two are mutually exclusive by construction) |
| tenant-facing role holds `DescribeUserPoolClient` on `userpool/*` + stack tag | tenant-facing role holds **no Cognito read at all**; `GetSecretValue` on `agentcore-connector/*`, which it already had |
| `per_agent` mode also handed over the pool ARN | `client_secret_grant_targets` returns the secret ARN **or** the pool ARN, never both |

Reading the secret from the create response rather than a follow-up `DescribeUserPoolClient`
is deliberate: the value is already in hand, and a second call is a second chance for IAM
or throttling to leave a client whose secret is unreachable. `_mint_client_secret_ref`
**raises** when Cognito returns no `ClientSecret` rather than falling back to the pool id —
a fallback would be a green deploy with a dead tool plane, which is precisely the failure
this area already produced once, when the unsatisfiable tag condition meant nothing failed
and no tools appeared.

The minted secret also carries `OwnerSubHash` (sha256, truncated — the binding only ever
needs to be *compared*) and `DeploymentId`. The `agentcore-connector/` prefix marks the
*product* and `AgentCoreStack` marks the *stack*; neither tells two co-resident tenants'
secrets apart, and a prefix is not an owner. It is recorded in the teardown manifest as a
`secret` row, which matters most in shared mode: the pool is deliberately not recorded
there (it is platform-owned and holds every other gateway's client), so the secret is the
one thing that deploy creates which nothing else would clean up.

`infra/tests/test_runtime_role_can_resolve_the_client_secret.py::TestTheCognitoGrant` and
`test_both_targets_can_be_present_at_once` now assert the **opposite** of what they used to.
They were inverted rather than deleted: a deleted test cannot fail when someone re-adds
the grant to fix a "legacy agent can't get a token" bug report. The inversion is paired
with a vacuity guard, because removing a grant is only correct if the path replacing it
works — otherwise the file passes against a role that cannot resolve a client secret by
any means, which is the original green-deploy-dead-tool-plane failure with a new cause.

**MIGRATION.** An agent deployed in `per_agent` identity mode *before* this change has
`COGNITO_USER_POOL_ID` in its environment and no reference, so it resolves its secret
through `DescribeUserPoolClient` and **stops being able to mint a gateway token** once its
role loses the grant. Redeploy the agent. Agents in the default `shared` mode are
unaffected, because for them the tag condition was already unsatisfiable and the tool
plane was already dead.

ARCC `cnt_n8LpZcqYi2t3I2` (never hold a secret in an environment variable),
`cnt_LuG2TKuO0errRp` (grant access only to the specific secret the principal needs),
`cnt_AGx9pUNpmdOVZB` (scope an identity to its business function),
`cnt_PQjUx2msVXY1wU` (unique credentials per entity).

### Fixed — a returning user's canvas came up stuck on "Validation Pending"
`FlowSidebar` auto-opens the most recent flow on every sign-in, so for anyone with
a saved flow this was the state the app started in. `loadFlow` restores the canvas
with the raw `setNodes`/`setEdges` setters, which do not validate, and the
debounced `useValidation` hook is not mounted on that path — so `validationState`
stayed `null` and the indicator read "○ Validation Pending" indefinitely, with
every node holding a stale `validationStatus`, until the user happened to open a
node's config modal or load a template. `loadFlow` now runs validation explicitly.

Measured in a real browser against the deployed stack, same flow and same restored
node each time, with only that one line differing:

| build | indicator after 25s on the restored canvas |
|---|---|
| fix present | **✓ Ready to Deploy** |
| line removed, rebuilt, re-synced, CloudFront invalidated | **○ Validation Pending** |
| restored, rebuilt, re-synced, invalidated | **✓ Ready to Deploy** |

The middle row is the point: without it, the first row is equally consistent with
something else on the page having validated. The probe deliberately does nothing
after sign-in — no template load, no node drop, no modal — and it fails if the
auto-opened flow restores zero nodes, so it cannot pass against an empty canvas.

### Fixed — four API calls shipped without the caller's token, all failing silently
Four call sites used bare `fetch` instead of `authFetch` against routes that
require a scope. None of them surfaced an error, which is why they survived:

| call site | consequence of the 401 |
|---|---|
| `DeployPanel.tsx` → `/api/test-runtime-stream` | `!response.ok` returns false, falling through to the non-streaming path. Chat kept working, so **streaming was unreachable in every deployed UI**. |
| `ObservabilityConfigurationModal.tsx` → `platform-defaults` | Reported `enabled: false`, so the endpoint/credentials/sample fields render editable even when the platform has locked them — and what the user types is dropped server-side at deploy time with no feedback. |
| `RuntimeConfigurationModal.tsx` → `platform-defaults` | Same, for the per-runtime "Enable OTEL" checkbox. |
| `ObservabilityConfigurationModal.tsx` → `observability/credentials` | Storing an OTLP auth header was impossible; the route names the secret after the caller's Cognito sub, so it cannot work without the token. |

Measured live: `POST /api/test-runtime-stream` → 401 on every message. `authFetch`
was already imported in `DeployPanel.tsx` and used for the non-streaming fallback
on the next code path, so the streaming call was one word away from correct.

A per-component test cannot catch the next one of these, because the defect is the
*absence* of a header nothing asserts on. `src/services/no-unauthenticated-api-call.test.ts`
is a source-level invariant instead: it walks the tree and fails on a bare `fetch`
to an `/api/` path, naming the file and line. It is self-checking — one case pins
the regex against a known-bad line, another fails if the walk stops finding files,
because a green assertion over an empty set is exactly the failure mode it exists
to prevent. It reads sources through Vite's `import.meta.glob` rather than
`node:fs`: `tsconfig.app.json` pins `"types": ["vite/client"]` and includes all
of `src`, so a `node:fs` import passes `tsc --noEmit -p tsconfig.app.json` and
still breaks `npm run build`, which runs `tsc -b` over every referenced project.

Verified live in a real browser against the deployed stack, not inferred. The
unauthenticated column is the request the old bare `fetch` actually sent:

| route | no header (old) | Bearer, from the browser (new) |
|---|---|---|
| `GET /api/observability/platform-defaults` | **401** `{"message":"Unauthorized"}` | **200** (×2 — the Observability and Runtime modals each make their own call) |
| `POST /api/observability/credentials` | **401** | **200**, and the returned secret ARN renders in the UI |
| `POST /api/test-runtime-stream` | **401** | pending — needs a deployed runtime to drive |

The control matters: a 200 on its own is equally consistent with the route not
requiring a token at all. Every `/api/` request the page made was checked for the
header, so the assertion covers the whole session rather than the three routes
that were changed.

### Fixed — the Evaluation tab span "Loading…" forever for most agents
`isNotReadyError` correctly swallows the 404 that `/evaluation-config` returns
when a runtime has no eval config — which is every agent deployed without an
Evaluation node, i.e. the common case. But that left `cfg` and `cfgError` both
`null`, which is indistinguishable from the initial pre-fetch state, so the panel
fell through to its loading text and stayed there. The amber "No evaluation config
registered" empty state was unreachable for the one condition it was written for,
as was the equivalent branch in the results block.

The panel now tracks whether the fetch has settled, so absence renders as absence.
The four sibling panels (`Cost`, `Observability`, `Versions`, `Triggers`) already
guarded with `loading && !x`; this was the sole outlier. The explicit flag is
kept in preference to that pattern because it also avoids a flash of the empty
state on first render. Both regression tests fail when the fix is reverted.

### Changed — the runtime role can now read the client secret it no longer receives
Because no deploy path injects `COGNITO_CLIENT_SECRET` any more, every runtime role
needs exactly one new read: `cognito-idp:DescribeUserPoolClient` for a
platform-minted Cognito gateway, or `secretsmanager:GetSecretValue` for an external
IDP. Getting this wrong fails **closed and silent** — the deploy still reports
SUCCESS and the agent then cannot mint a gateway token at all, so every tool call
sees an empty tool list, and nothing goes red until a human invokes the agent.

`runtime_deployer.client_secret_grant_targets()` is the single source of truth for
both ARNs, because three deploy paths need them (per-agent role, legacy per-deploy
role, direct deploy) and a drift between them is invisible in the same way. A
`client_secret_ref` is a Secrets Manager *name*, and Secrets Manager appends a
random six-character suffix to the ARN, so the trailing `-*` is load-bearing: a
policy naming the secret exactly matches nothing.

The CDK shared role cannot name the pool — a user-pool ARN contains the pool ID,
minted per-deploy and unknowable at synth time — so the grant is scoped by ABAC on
the owner tag the gateway step already stamps (`AgentCoreStack={project}-{env}-{region}`).
**Verified live** rather than assumed, with a throwaway tagged pool and two probe
roles differing only in the condition value:

| measurement | result |
|---|---|
| correct condition → correctly-tagged pool | ALLOW |
| correct condition → differently-tagged pool | DENY |
| wrong condition value → tagged pool | DENY |
| the ALLOW response carries `ClientSecret` | yes |

The third measurement is what makes the first meaningful: without it, an ALLOW is
equally consistent with the condition being ignored.

`secretsmanager:GetSecretValue` on the shared role is confined to the platform's
own `agentcore-connector/` namespace. A customer pointing an Identity node at a
long-lived secret of their own, outside it, is deliberately not covered: granting
this role every secret in the account would let any agent read the platform's git
PATs and every other tenant's connector credentials. That case must use
`identity_mode: 'per_agent'`, which scopes to the one secret ARN at deploy time.
ARCC `cnt_pXauQr9E6bKwke` is directly on point — "avoid resource wildcards for APIs
that read the contents of objects… scope down resources where possible, or use
condition keys including tag based conditions" — alongside `cnt_LuG2TKuO0errRp`
and `cnt_SFJJhkOueCPRkd`.

The CDK assertions were mutation-tested: removing the condition, widening the
secrets grant to `secret:*`, and dropping the action each fail a named test.

### Fixed — a typo in the gateway provider silently deployed the other backend
`resolve_gateway_provider()` fell back to `"agentcore"` for any unrecognized
non-empty provider value. Its docstring justified this with "the model layer
already rejects bad values at the API boundary", which is not true on the
CloudFormation export path: `DeployRequest.gateway_config` is declared
`dict | None` (deployment_models.py:556), so `GatewayConfiguration` is never
constructed there and nothing validates the provider before it reaches this
function.

Measured against the real generator — a canvas carrying the single-character typo
`gatewayProvider: "lite-llm"`:

| canvas | before | after |
|---|---|---|
| typo `"lite-llm"` | emitted `AWS::BedrockAgentCore::Gateway` + 4 Cognito resources, **silently discarded `litellmBaseUrl`**, no error | refused with an actionable `ValueError` naming the bad value and the valid set |
| valid `"litellm"` | correct | unchanged: no Gateway, no Cognito pool, proxy URL retained |
| valid `"agentcore"` | correct | unchanged: Gateway + Cognito emitted |

Setting `DEFAULT_GATEWAY_PROVIDER=litellm` did not help, because the fallback
returned before the platform default was ever consulted. A silent fallback is only
defensible when the misroute is harmless, and it is not: one typed character
changed which backend the agent talked to and dropped the proxy URL and key
reference with it. A legacy stored canvas is unaffected — it carries no provider
key at all and still follows the platform default, which is pinned by its own
test so that failing closed cannot be confused with breaking existing agents.
ARCC returned nothing directly on point for enum fail-closed defaults; the nearest
guidance (`cnt_fImfV93NdsOrCd`) warns against designing around which callers
*should* reach a backend rather than which ones *can*.

### Removed — a dead deploy UI that owned the only 3 fake test ids
`DeployButton.tsx`, `DeploymentModal.tsx` and `ErrorSummaryModal.tsx` (plus
`DeployButton.test.tsx`, 1,183 lines together) were imported only by each other and
by `components/deploy/index.ts`, which nothing imported. The app renders
`AppHeader`'s Deploy button → `DeployPanel`. Deleting all four left `tsc --noEmit`
clean and the suite at 29 files / 295 tests passing, which is the evidence they
were genuinely unreachable.

They were not harmless. They held the only copies of the `deploy-button`,
`deployment-modal` and `error-summary-modal` test ids — 3 of the 44 in the
frontend, and the only 3 that match nothing in a real browser. A UI test reaching
for `deploy-button` waits 60s and times out while a working Deploy button sits in
the header, and a persona test asserting "the end-user view has no
`deploy-button`" passes vacuously because *no* view has one. Both happened here.
`DeployButton.test.tsx` was 393 lines of passing tests over a component the
product never rendered. The shipped control in `AppHeader.tsx` now carries
`data-testid="deploy-button"` so the id points at the real thing.

Two capabilities existed only in the dead code and are noted rather than
reimplemented: a deploy-region `<select>` (the shipped path takes the platform's
own region from workflow metadata via `getDeploymentRegion()`; `useDeployment.ts`
has no region handling at all) and a standalone error-summary modal.

### Fixed — enforcing RBAC only hardened 15 of the 66 API operations
`RBAC_ENFORCE` was set on the workflow Lambda and **not** on the deployment Lambda,
which serves 51 of the 66 API operations — every scope-guarded `/api/admin`,
`/api/cost`, `/api/registry`, `/api/permissions`, `/api/prompts`, `/api/tags`,
`/api/triggers`, `/api/connectors`, `/api/hitl`, `/api/approvals`,
`/api/evaluations`, `/api/versions`, `/api/identity`, `/api/models`,
`/api/mcp-servers` and `/api/vpc-profiles` route. `rbac_enforcing()` reads
`os.environ.get("RBAC_ENFORCE", "")`, so an **absent** variable is
indistinguishable from `"false"`: the routes stayed advisory and nothing logged
that they differed from the ones that flipped. An operator following
docs/RBAC_ROLLOUT.md step 5 (`RBAC_ENFORCE=true ./scripts/deploy.sh`) therefore
believed the whole control plane was fail-closed while 51 operations still allowed
every authenticated caller. The doc itself was already right — steps 1 and 5 name
"the workflow + deployment Lambdas"; only the stack was wrong.

Measured live on throwaway stack `acfe2e-p0920` (us-east-1) with a caller in
`t-user` and no `g-*` group, so holding zero scopes:

| configuration | `GET /api/workflows` (workflow) | `GET /api/admin/audit` (deployment) | `GET /api/cost/budgets` (deployment) |
|---|---|---|---|
| advisory (shipped default) | 200 | 200 | 200 |
| flag on the workflow Lambda only (what deploy.sh did) | **403** | 200 | 200 |
| flag on both (after this fix) | **403** | **403** | **403** |

A caller in `g-admins-super` kept 200 on all three in every configuration, so
enforcing is not simply "deny everything" — the grant table works, it just was not
consulted on three quarters of the surface.

`infra/tests/test_rbac_enforce_reaches_every_api_lambda.py` pins the invariant
against the HTTP API's integration targets in the synthesized template rather than
a hard-coded pair of function names, so adding a third API Lambda without the
variable fails the suite instead of shipping a hole. All three assertions fail
against the pre-fix stack and pass after it.

Unchanged: the default is still advisory (`"false"`) on both Lambdas, so an
operator who sets nothing changes nothing.

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
