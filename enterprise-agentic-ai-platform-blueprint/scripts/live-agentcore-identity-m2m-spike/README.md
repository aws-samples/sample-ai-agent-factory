# AgentCore Identity M2M compatibility spike

A cleanup-first, isolated, single-purpose probe of **Amazon Bedrock AgentCore
Identity's machine-to-machine (M2M) path**. It proves that a caller-owned
Cognito app client can be wired to an AgentCore **OAuth2 credential provider**,
that a **workload identity** can mint a workload access token, that the token
exchanges for an **M2M resource token** for an exact scope, and that the
resulting bearer token drives the existing platform inference Gateway through a
real Strands `LiteLLMModel` (non-streaming and streaming). Then it deletes
everything it created.

This spike follows the same discipline as the sibling
`scripts/live-agentcore-gateway-spike` and
`scripts/live-agentcore-runtime-memory-spike`: a pure, AWS-free model
(`identity_m2m_model.py`) holding all validation/ownership/secret-safety logic,
a thin runner (`identity_m2m_spike.py`) whose every AWS call is a named,
scope-guarded wrapper, and focused offline tests.

> **This is a compatibility spike, not a readiness claim.** It exercises exact
> AWS API contracts in one account/region against caller-supplied prerequisites.
> Passing it does **not** establish production readiness, a region matrix, load
> or soak behaviour, quota headroom, or that any generated agent uses this path.
> `MCPClient` is explicitly **out of scope** here and is a separate following
> spike. See [Scope and non-goals](#scope-and-non-goals).

---

## What it owns vs. what you must provide

The probe owns **exactly two** AWS resources, both run-named:

| Owned by the probe                  | Service (SDK op)                                                                    |
| ----------------------------------- | ----------------------------------------------------------------------------------- |
| One AgentCore **workload identity** | `bedrock-agentcore-control:CreateWorkloadIdentity`                                  |
| One **OAuth2 credential provider**  | `bedrock-agentcore-control:CreateOauth2CredentialProvider` (vendor `CognitoOauth2`) |

You must provide (the probe **never** creates, mutates, or deletes these):

- An **existing Cognito user pool** and an **M2M app client** in it (client
  credentials grant, with a **client secret**), plus a resource server + scope.
- The OAuth2 `issuer`, `authorizationEndpoint`, and `tokenEndpoint` URLs.
- An **existing platform inference Gateway** base URL that accepts the resource
  token as a bearer and serves an OpenAI-compatible `/inference/v1`.
- The **target-qualified model id** you expect discovery to return.

Before either create or token exchange, these values are compared with the
named inference stack's `CognitoUserPoolId`, `CognitoClientId`, `TokenEndpoint`,
`GatewayUrl`, `OAuthScope`, and `InferenceTargetName` outputs. Any mismatch is a
hard stop; cleanup deliberately remains independent of that retained stack.
The Cognito app-client **secret** is read in-process via
`DescribeUserPoolClient` and passed straight into
`CreateOauth2CredentialProvider`. It is **never** written to state, evidence,
logs, or stdout.

---

## Exact AWS API contracts implemented

Every call is a narrow wrapper (`IdentityM2mApi`). Members below are transcribed
from the pinned `boto3==1.43.98` / `botocore==1.43.98` service models.

Control plane — `bedrock-agentcore-control`:

- `CreateWorkloadIdentity(name, tags)` → `name`, `workloadIdentityArn`
- `GetWorkloadIdentity(name)` / `DeleteWorkloadIdentity(name)`
- `ListWorkloadIdentities()` (paginated; summaries carry only `name` + arn)
- `ListTagsForResource(resourceArn)` → the five exact ownership tags
- `CreateOauth2CredentialProvider(name, credentialProviderVendor='CognitoOauth2',
oauth2ProviderConfigInput, tags)` where `oauth2ProviderConfigInput` uses the
  `includedOauth2ProviderConfig` member with `clientId`, `clientSecret`,
  `issuer`, `authorizationEndpoint`, `tokenEndpoint`
- `GetOauth2CredentialProvider(name)` / `DeleteOauth2CredentialProvider(name)`
- `ListOauth2CredentialProviders()` (paginated)

Provider `status` enum handled: `CREATING`, `CREATE_FAILED`, `UPDATING`,
`UPDATE_FAILED`, `READY`, `DELETING`, `DELETE_FAILED`. Any other value fails
closed.

Data plane — `bedrock-agentcore`:

- `GetWorkloadAccessToken(workloadName)` → `workloadAccessToken`
- `GetResourceOauth2Token(workloadIdentityToken, resourceCredentialProviderName,
scopes, oauth2Flow='M2M')` → `accessToken`

> **Ownership note.** Get/List responses do not inline tags, so every ownership
> proof calls `ListTagsForResource` on the exact ARN and requires all five exact
> run tags in addition to the run-derived name and account/region ARN. Although
> the generic API page says Identity is unsupported, a read-only `us-west-2`
> call against the retained inference workload identity returned HTTP 200; this
> spike still requires the live provider tag readback to pass before cleanup.

---

## Secret and token handling (non-negotiable)

- The Cognito client secret is read in-process and used exactly once; the local
  reference is dropped immediately after `CreateOauth2CredentialProvider`.
- Workload and resource tokens live only in local variables while an exchange or
  inference call is in flight, then are dropped.
- The Gateway URL must be the regional AgentCore `...amazonaws.com/mcp` host,
  and issuer/authorization/token URLs must bind to the exact regional Cognito
  pool and one managed hosted domain; arbitrary bearer-token destinations are
  refused before any AWS or HTTP call.
- Evidence records **booleans, safe lengths, and one-way fingerprints of
  non-secret identifiers only** — never a token, token prefix, ARN, account id,
  or client secret.
- Every evidence write is scanned **recursively** against credential/JWT/ARN/
  account-id/UUID patterns and refused if anything credential-shaped appears.
- State/evidence writes are **atomic** (temp file + `chmod 600` + `replace`) and
  persist only non-secret resource names and the run marker.

---

## Prerequisites

- Python with `boto3==1.43.98`, `botocore==1.43.98`, plus `strands-agents==1.44.0`
  and `litellm==1.89.1` for the live inference leg. Pins are in
  `requirements.txt` and asserted at runtime.
- `KIROCREW_SCRATCH` set to a private scratch directory. State and evidence
  files must live under it (refused otherwise).
- Credentials for the **exact target account** with permission for the AgentCore
  identity operations above (including `ListTagsForResource`),
  `cloudformation:DescribeStacks` on the named inference stack,
  `cognito-idp:DescribeUserPool`, `cognito-idp:DescribeUserPoolClient` on the
  existing pool/client, and `sts:GetCallerIdentity`.

---

## How to run

> **Target account / region / credentials.** Every command below must run
> against **AWS account `<ACCOUNT_ID>` in region `<REGION>` (default
> `us-west-2`)**, using credentials already brokered for that exact account in
> your current shell. The runner calls `sts:GetCallerIdentity` first and
> **refuses** to proceed if the live account does not equal `--account-id`.

Install the pins into an isolated environment first (example):

```bash
python3 -m venv "$KIROCREW_SCRATCH/identity-m2m-venv"
"$KIROCREW_SCRATCH/identity-m2m-venv/bin/pip" install \
  --disable-pip-version-check \
  -r scripts/live-agentcore-identity-m2m-spike/requirements.txt
```

Phases (run against account `<ACCOUNT_ID>`, region `<REGION>`):

```bash
PY="$KIROCREW_SCRATCH/identity-m2m-venv/bin/python"
SPIKE=scripts/live-agentcore-identity-m2m-spike/identity_m2m_spike.py
COMMON=(
  --account-id <ACCOUNT_ID> --region <REGION>
  --prefix aiaf-idm2m-spike
  --source-revision <EXACT_40_CHARACTER_GIT_SHA>
  --stack-name Prod-InferenceGateway
  --user-pool-id <COGNITO_USER_POOL_ID>
  --client-id <COGNITO_M2M_APP_CLIENT_ID>
  --issuer <OAUTH2_ISSUER_URL>
  --authorization-endpoint <OAUTH2_AUTHORIZE_URL>
  --token-endpoint <OAUTH2_TOKEN_URL>
  --resource-scope <RESOURCE_SERVER/SCOPE>
  --gateway-url <INFERENCE_GATEWAY_BASE_URL>
  --model-id <TARGET_QUALIFIED_MODEL_ID>
)

# 1. Read-only SDK/account/stack-output preflight.
"$PY" "$SPIKE" preflight "${COMMON[@]}"

# 2. Create the workload identity + credential provider; wait for READY.
"$PY" "$SPIKE" deploy "${COMMON[@]}"

# 3. Mint workload token -> M2M resource token -> model discovery + LiteLLMModel.
"$PY" "$SPIKE" verify "${COMMON[@]}"

# 4. Delete provider then workload identity; verify absence.
"$PY" "$SPIKE" cleanup "${COMMON[@]}"

# Or the whole sequence with guaranteed teardown even on failure:
"$PY" "$SPIKE" run-all "${COMMON[@]}"
```

`run-all` runs `cleanup` in a `finally`, so a failure anywhere still tears down.
State and evidence default to
`$KIROCREW_SCRATCH/<prefix>-identity-m2m-{state,evidence}.json`; override with
`--state-file` / `--evidence-file` (both must stay under `KIROCREW_SCRATCH`).

---

## Phases

| Phase       | Scope (only these side-effecting calls are reachable)                                        |
| ----------- | -------------------------------------------------------------------------------------------- |
| `preflight` | read-only STS identity + exact inference-stack outputs + pinned SDK shapes                   |
| `deploy`    | `CreateWorkloadIdentity`, `CreateOauth2CredentialProvider`                                   |
| `verify`    | Positive token exchange, wrong-scope/provider/workload-token denials, Gateway HTTP + LiteLLM |
| `cleanup`   | `DeleteOauth2CredentialProvider`, `DeleteWorkloadIdentity`                                   |

A per-phase scope guard refuses any call outside the current phase's allowed
set, so `verify` can never mutate a resource and `deploy` can never mint a token.

---

## Cleanup and ordering

Cleanup deletes the **credential provider first, then the workload identity**.
Rationale: the provider is the resource exercised through the workload
identity's token exchange, so it is removed first; the workload identity is
removed second. The two steps are **independent** — if the provider delete
fails, the workload delete is still attempted — and both require a live
ownership proof before deleting. After both, a discovery-based residual sweep
runs; **any** uncertainty counts as residue and fails the cleanup, preserving
the primary failure evidence. A completed run marks its state `completed` and
refuses to redeploy against the same state/evidence files (AgentCore
idempotency must not be reused after deletion — use fresh files).

Partial creates are recovered by exact run-owned name before creating a
duplicate. An exact-name resource that is **not** owned by this run (wrong
vendor or wrong account/region scope) is refused, never deleted.

---

## Scope and non-goals

In scope: the exact identity/token API contracts above, provider `READY`
gating, one M2M token exchange for one scope, target-qualified model discovery,
and a real `LiteLLMModel` non-streaming + streaming reply.

Explicitly **out of scope / not proven** here:

- **`MCPClient` / Tools Gateway** — a separate following spike.
- Any **readiness, region-matrix (incl. EMEA), quota, concurrency, load, soak,
  chaos, upgrade, or cost** claim.
- **Predeclared error-code expectations** — wrong-scope, wrong-provider, and
  wrong-workload-token calls must each return a non-timeout/non-throttle 4xx,
  but the runner records the exact service code only after AWS returns it.
- Any claim that a **generated agent** uses this path.

---

## Offline tests

The offline suite is AWS-free (all AWS access is faked) and covers the model and
the runner control logic, including the guarantee that an injected sentinel
secret and sentinel tokens never reach the state file, evidence file, stdout, or
an error message.

```bash
cd scripts/live-agentcore-identity-m2m-spike
python -m pytest -q          # uses the pins in requirements.txt
python -m py_compile identity_m2m_model.py identity_m2m_spike.py conftest.py
```

`conftest.py` puts this directory and the sibling gateway-spike directory on
`sys.path` (for the shared `SpikeError`/`Evidence`/`JsonStore` helpers) and makes
no AWS calls at import or collection time.

---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
