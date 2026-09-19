# AgentCore Gateway inference compatibility spike

This focused live test proves the two August 2026 contracts that the host's older
AWS CLI cannot model:

1. an AgentCore Gateway inference target using the built-in `bedrock-mantle`
   connector; and
2. a customer-defined Gateway rate limit using `CreateGatewayRateLimit`.

It is deliberately **not** the final platform deployment. Phase A uses AWS IAM
inbound authorization to prove service behavior with the smallest credential
surface. Phase B adds Cognito M2M and the Strands `LiteLLMModel` client only after
Phase A passes.

## Safety properties

- Requires the expected 12-digit account ID and refuses a different STS identity.
- Creates only resources beginning with the supplied `--prefix`.
- Applies `application-id`, `agent-id`, `tenant-id`, `cost-centre`, and
  `environment` tags to every taggable resource.
- Uses the exact Gateway service-role permissions from the official AgentCore
  sample: `bedrock-mantle:ListModels` and `bedrock-mantle:CreateInference`.
- Stores state and evidence under `$KIROCREW_SCRATCH`, never in `/tmp` or a
  committable directory.
- `all` runs cleanup in `finally`; cleanup verifies ownership before deletion.
- Never reads or handles access keys. Boto3 uses the credential provider already
  selected by the execution environment.

## Pinned tooling

The August 2026 API first appears after the repository's old `boto3==1.35.90`
pins. This spike uses exactly:

```text
boto3==1.43.97
botocore==1.43.97
```

Create an isolated environment:

```bash
SPIKE_DIR="$PWD/scripts/live-agentcore-gateway-spike"
VENV="$KIROCREW_SCRATCH/aiaf-gateway-spike-venv"
uv venv "$VENV" --python 3.13
uv pip install --python "$VENV/bin/python" -r "$SPIKE_DIR/requirements.txt"
```

## Run the complete IAM-authenticated spike

Use brokered credentials for the Platform account before running this command.
Do not set credential values in environment variables.

```bash
"$VENV/bin/python" "$SPIKE_DIR/gateway_spike.py" all \
  --account-id "$PLATFORM_ACCOUNT_ID" \
  --region us-west-2 \
  --prefix aiaf-live-20260918
```

The command must prove, in order:

1. STS identity matches `--account-id`.
2. Gateway service role is created with scoped trust and permissions.
3. Gateway reaches `READY`.
4. `bedrock-mantle` inference target reaches `READY`.
5. `/inference/v1/models` returns a qualified model.
6. Chat Completions succeeds without streaming.
7. Chat Completions succeeds with OpenAI-compatible SSE streaming.
8. A zero-rate explicit model entry returns HTTP 429 for that same known-good
   model, while the earlier call is its positive twin.
9. Rate limit, target, Gateway, inline role policy, and role are removed.
10. A second cleanup pass reports no residue and exits successfully.

Evidence contains request IDs, status codes, resource identifiers, manifest SHA,
and cleanup results. A discovery mismatch records at most ten related public model
IDs plus five sorted samples for diagnosis. Evidence excludes credentials,
authorization headers, model prompts, and model response text.

## Manual recovery

If the process is interrupted, run cleanup with the same state file printed by
the command:

```bash
"$VENV/bin/python" "$SPIKE_DIR/gateway_spike.py" cleanup \
  --account-id "$PLATFORM_ACCOUNT_ID" \
  --region us-west-2 \
  --prefix aiaf-live-20260918
```

Cleanup refuses to delete a Gateway or IAM role unless both its exact name and
ownership tags match the spike configuration.

## Definition of done

A local unit test, successful synth, or HTTP error is not proof. Phase A passes
only when a real model call succeeds before the rate limit and the same model
then returns an exact 429 after the zero-rate rule, followed by a zero-residue
audit. Phase B separately proves JWT refresh and `LiteLLMModel` behavior.

## Phase B: Cognito M2M and Strands `LiteLLMModel`

After Phase A passes, run the binding generated-agent client test:

```bash
"$VENV/bin/python" "$SPIKE_DIR/cognito_litellm_spike.py" all \
  --account-id "$PLATFORM_ACCOUNT_ID" \
  --region us-west-2 \
  --prefix aiaf-live-20260918
```

Phase B creates an ephemeral Cognito User Pool, client-credentials resource
server/client/domain, and a `CUSTOM_JWT` Gateway. It retrieves each access token
in memory, passes it through `LiteLLMModel.client_args.api_key`, and never writes
or logs the token or client secret. The model uses:

```python
LiteLLMModel(
    model_id="openai/bedrock-mantle/openai.gpt-oss-120b",
    client_args={"api_base": "<gateway>/inference/v1", "api_key": token},
    params={"max_tokens": 256, "temperature": 0, "stream": stream},
)
```

The gate passes only when distinct streaming and non-streaming Strands events are
recorded, the exact 429 names the non-streaming event as its positive twin, and
both the AgentCore/IAM and Cognito cleanup audits report zero residue.

## Verify a pipeline-owned Gateway without mutation

After the Platform pipeline succeeds, verify its deployed production Gateway in
place with the same pinned environment:

```bash
"$VENV/bin/python" "$SPIKE_DIR/verify_pipeline_gateway.py" \
  --account-id "$PLATFORM_ACCOUNT_ID" \
  --region us-west-2 \
  --stack-name Prod-InferenceGateway \
  --git-head "$(git rev-parse HEAD)" \
  --model openai.gpt-oss-120b \
  --evidence-file "$KIROCREW_SCRATCH/pipeline-gateway-evidence.json"
```

This verifier reads all resource identifiers from CloudFormation and derives the
exact target-qualified model route as
`<InferenceTargetName>/<provider-qualified-model-id>`. It requires the stack to
be in `CREATE_COMPLETE` or `UPDATE_COMPLETE` (rollback states fail), and requires
the Gateway, target, and native rate limit to be ready. It validates the five
allocation tags and Cognito client-credentials configuration, then runs model
discovery plus streaming and non-streaming Strands `LiteLLMModel` invocations.
It retrieves the Cognito client secret and access tokens only in process memory,
never writes or prints them, and performs no create, update, delete, cleanup, or
rate-limit mutation.

---

## Phase P: AgentCore Gateway PolicyEngine (Cedar) compatibility spike

`policy_engine_spike.py` is a separate, self-contained probe of **Policy in
Amazon Bedrock AgentCore** — the Cedar policy engine that attaches to a Gateway
and authorises every `tools/list` and `tools/call`. It shares this directory's
`Config`/`JsonStore`/`Evidence` primitives and the same pinned
`boto3==1.43.97` / `botocore==1.43.97`, and it needs no extra dependency (no
Strands, no LiteLLM). Its AWS-free logic lives in `policy_engine_model.py` so the
security-critical parts are unit-testable with no SDK at all.

### What it stands up, and what it proves

| Live resource | Purpose |
|---|---|
| Cognito User Pool, 2 app clients, 3 groups, 4 users | `alpha`=subject+group, `beta`=group only, `gamma`=subject+collision group, `delta`=neither; the second client is outside `allowedClients` |
| Tagged one-day log group, deterministic Lambda, 2 least-privilege roles | Five MCP tools expose subject-only, group-only, OR, AND, and default-deny cases; the handler never logs or returns argument values |
| AgentCore PolicyEngine + 8 Cedar policies | Strict `FAIL_ON_ANY_FINDINGS`, `ACTIVE`, exact actions/resources, and direct subject/group semantics |
| `CUSTOM_JWT` MCP Gateway + Lambda target | Target schema exists before the engine is associated in `LOG_ONLY`; policies are created only after association, then mode moves to `ENFORCE` |

Proof obligations, all recorded without credentials or payload text:

1. **Exact policy scope.** Every live definition equals its generated statement
   and names `AgentCore::Action::"<TargetName>___<ToolName>"` plus the exact
   Gateway ARN. Wildcard resources are rejected before any API call.
2. **Direct identity matrix.** Subject-only uses exact JWT `sub`; group-only
   uses the `cognito:groups` candidate; ANY is subject OR group; ALL is subject
   AND group. The 20-case matrix proves all four user profiles.
3. **Array-claim collision safety.** AWS documents JWT claims as Cedar tags but
   not how array claims are serialized. The spike tests the narrow expression
   `hasTag("cognito:groups") && getTag("cognito:groups") like "*\\"group\\"*"`.
   Exact members must be allowed; both prefix/suffix collision groups must be
   denied. A bare `*group*` expression is rejected by local guards.
4. **List and direct-call enforcement.** `tools/list` must exactly match each
   profile (including an empty list for `delta`), and `denied-tool` must still
   fail when called directly by qualified name.
5. **Authentication negatives.** Missing header, malformed compact JWS, and
   `alg=none` must return 401. Forged `sub`, forged `cognito:groups`, a client
   outside `allowedClients`, and an expired parseable token must return 403.
6. **Mode rollback.** One default-deny request must be denied in `ENFORCE`,
   allowed in `LOG_ONLY`, and denied again after restoring `ENFORCE`.
7. **Zero residue.** Cleanup polls each asynchronous policy deletion before the
   engine delete and inventories Gateway, PolicyEngine, Cognito, Lambda, IAM,
   and the Lambda log group. Any survivor fails the run.

### Safety properties

- Requires the expected 12-digit account ID and refuses a different STS identity.
- Refuses any region outside the documented Policy in AgentCore GA list
  (including the five EMEA regions `eu-west-1/2/3`, `eu-central-1`,
  `eu-north-1`); the region actually exercised is recorded as the only live
  evidence.
- Creates only resources whose names start with `--prefix`. Because
  `CreatePolicy`/`CreatePolicyEngine` names match `[A-Za-z][A-Za-z0-9_]*`, the
  prefix is translated to its underscore form for those two resource classes,
  and a prefix that would overflow the 48-character name limit is refused at
  construction.
- Applies the five allocation tags to every taggable resource and re-checks exact
  identity plus tags before every delete. Targets and policies must match the
  finite generated name set; a prefix-sharing resource is not accepted.
- `all` runs cleanup in `finally`, always includes the expired-token wait, and
  fails if any resource class remains. A later cleanup-only pass appends its
  inventory without overwriting the original `passed` or `failed` verdict.
- State and evidence are written only under `$KIROCREW_SCRATCH`, mode `0600`.
- Never stores or prints a password, a client secret, an access token, an
  `Authorization` header, a tool argument value, or a tool response. App clients
  are created **without** a secret; user passwords are random, single-use, and
  held in process memory only; evidence values are scanned recursively for
  JWT-shaped strings, `AKIA`/`ASIA` keys, `Bearer` headers, and
  credential-named fields, and a match aborts the write.
- Tool decisions are recorded as `ALLOW`/`DENY` plus a reason code and a SHA-256
  fingerprint of the response body — never the body.
- A tool error that does **not** name a policy denial is an error, not a denial,
  so a broken Lambda can never be scored as successful enforcement.

### IAM requirements

The **Gateway execution role** gets exactly three inline policies with concrete
ARNs and no wildcards:

```json
{"Effect":"Allow","Action":"lambda:InvokeFunction","Resource":"<exact function ARN>"}
{"Effect":"Allow","Action":"bedrock-agentcore:GetPolicyEngine","Resource":"<exact engine ARN>"}
{"Effect":"Allow",
 "Action":["bedrock-agentcore:AuthorizeAction","bedrock-agentcore:PartiallyAuthorizeActions"],
 "Resource":["<exact engine ARN>","<exact gateway ARN>"]}
```

The Lambda resource policy names the exact Gateway role ARN and is idempotently
reused only when the existing statement matches. A fresh-role “invalid
principal” response is retried for up to six minutes, with the exact statement
ID checked before every attempt. The authorization policies are attached before
association. Gateway create/target create/mode updates use the same bounded
propagation window. Validation errors fail immediately except the exact
live-proven `GetPolicyEngine` access-denied propagation message.

The **management caller** needs `bedrock-agentcore:CreatePolicyEngine`,
`CreatePolicy`, `UpdatePolicy`, `DeletePolicy`, `DeletePolicyEngine`,
`UpdateGateway`, and `ManageResourceScopedPolicy` (resource-scoped statements
only — `ManageAdminPolicy` is deliberately **not** required, because this spike
never writes a wildcard-scoped policy), plus the usual Cognito, Lambda, and IAM
create/delete rights for the ephemeral resources. `verify` records a read-only
`SimulatePrincipalPolicy` result for those actions and, when simulation is
unavailable, records an explicit unknown instead of guessing.

### Commands

```bash
SPIKE_DIR="$PWD/scripts/live-agentcore-gateway-spike"
VENV="$KIROCREW_SCRATCH/aiaf-gateway-spike-venv"

"$VENV/bin/python" "$SPIKE_DIR/policy_engine_spike.py" all \
  --account-id "$PLATFORM_ACCOUNT_ID" \
  --region us-west-2 \
  --prefix aiaf-pe-spike
```

| Command | Effect |
|---|---|
| `deploy` | Creates the topology and associates the engine `LOG_ONLY` then `ENFORCE` |
| `verify` | **Read-only.** No mutating API is reachable from it, statically or at runtime |
| `rollback` | `ENFORCE → LOG_ONLY → ENFORCE` with a behaviour twin at each step; its only AWS mutation is `UpdateGateway` |
| `cleanup` | Dependency-ordered teardown plus a zero-residual sweep; safe to re-run |
| `all` | `deploy` → `verify` → `rollback`, mandatory expiry wait, cleanup in `finally` |

`all` always proves expired-token denial, adding roughly six minutes because
Cognito's minimum access-token lifetime is five minutes. Standalone `verify`
can opt in with `--include-expiry-wait`.

`verify` and `rollback` mint user tokens, and user passwords are never
persisted — so they can only run in the same process that created the users.
Use `all` for a full pass; standalone `verify`/`rollback` fail closed with that
message. `cleanup` works standalone from the state file.

### Cost

All resources are ephemeral and torn down in the same run. A bounded pass makes
roughly 50 MCP requests and fewer than 100 Lambda invocations, keeping Lambda,
CloudWatch Logs, and Cognito (four users) inside or near the free tier. The
metered lines are AgentCore Gateway requests and Policy evaluations — see the
[Amazon Bedrock AgentCore pricing page](https://aws.amazon.com/bedrock/agentcore/pricing/);
this README deliberately quotes no figures. Expect well under one US dollar for
a complete pass, and note that the wall-clock cost is dominated by IAM/control-
plane propagation waits (roughly 3–6 minutes of deliberate sleeps) rather than
by request volume.

### Stop criteria

Abort the run and do not iterate blindly if any of these occur:

- `CreatePolicy` rejects a statement under `FAIL_ON_ANY_FINDINGS` — record the
  finding; do not retry with `IGNORE_ALL_FINDINGS`.
- Association returns `InternalServerException` — the execution role is missing
  one of the three policy-evaluation actions.
- A tool call returns an error that is not a policy denial — the topology is
  broken, and any "denial" observed afterwards is meaningless.
- An observed decision disagrees with the truth table — stop and re-derive the
  policy set rather than editing the expectation.
- Any auth negative differs from its pinned 401/403 status — do not count an
  arbitrary 4xx/5xx or unrelated failure as authorization evidence.
- Cleanup reports residue twice in a row — remove the remaining resources by
  hand before another run.

### Cleanup and recovery

Cleanup detaches the policy engine, deletes the target and Gateway, then deletes
and polls every policy absent before deleting the engine. It next removes the
function, both roles, tagged log group, and user pool (including clients, groups,
and users). Every resource is identity/tag checked before deletion; cleanup
remains safe to re-run:

```bash
"$VENV/bin/python" "$SPIKE_DIR/policy_engine_spike.py" cleanup \
  --account-id "$PLATFORM_ACCOUNT_ID" --region us-west-2 --prefix aiaf-pe-spike
```

### Tests

```bash
"$VENV/bin/python" -m pytest scripts/live-agentcore-gateway-spike -q
```

`test_policy_engine_model.py` needs no AWS SDK. `test_policy_engine_spike.py`
skips unless the pinned boto3 is installed; it constructs clients offline and
drives a fake API recorder. Between them they pin resource-name ownership,
secret safety, Cedar generation and the truth table, invalid configurations,
cleanup ordering and idempotence, and — by static call-graph analysis plus a
runtime scope guard — that `verify` cannot reach a mutating operation and
`rollback` can reach only `UpdateGateway`. Dedicated regressions pin association
before policy creation, six-minute propagation, Lambda-permission idempotency,
asynchronous policy deletion, and ownership refusal for engine, function, and
log group.

### Definition of done

A synth, unit test, or `CREATE_COMPLETE` is not proof. The spike passes only when
a live `ENFORCE` Gateway reproduces all 20 decisions, `tools/list` matches every
profile, exact and collision group cases behave correctly, all invalid JWT twins
return 401, token expiry is observed, the three-step mode rollback passes, and
cleanup plus a second independent inventory find zero residue. Evidence applies
only to the exact commit, account class, SDK version, and region exercised; other
regions remain separate matrix entries.
