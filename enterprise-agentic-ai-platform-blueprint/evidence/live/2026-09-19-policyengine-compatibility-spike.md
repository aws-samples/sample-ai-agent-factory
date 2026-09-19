# Live evidence — AgentCore Gateway PolicyEngine compatibility

- **Date:** 2026-09-19
- **Status:** PASS for the bounded `us-west-2` compatibility envelope below
- **Final Git HEAD:** `46c3a62a30adc7bf07b49cb8853e370534f995be`
- **SDK contract:** Boto3 and Botocore `1.43.97`
- **Protocol:** MCP `2025-06-18`
- **Validation mode:** `FAIL_ON_ANY_FINDINGS`
- **Topology:** one ephemeral Platform-account Gateway, Lambda target, PolicyEngine, Cognito pool, four users, and three groups

This is a sanitized summary. It contains no AWS account IDs, access keys,
passwords, client secrets, JWTs, authorization headers, tool argument values, or
tool response text. The raw run record stayed in session scratch and was not
committed because it contains account-scoped resource identifiers.

## Preconditions

The final revision passed:

- `npm run build`
- `npm run lint`
- `npm run scrub`
- 158 focused Python tests against pinned Boto3 `1.43.97`
- Python and TypeScript CodeQL on the exact PR head
- independent read-only review of the initial six-file implementation, with no
  Critical/High finding or falsely passing test

A cleanup-only preflight found no resource with the final run prefix before
provisioning.

## Identity matrix

Four Cognito users exercised independent subject and group dimensions:

| User | Allowed `sub` | Group claim | Expected purpose |
|---|---:|---|---|
| `alpha` | yes | exact allowed group | subject + group |
| `beta` | no | exact allowed group | group only |
| `gamma` | yes | suffix-collision group | subject only; group must not match |
| `delta` | no | two collision groups | neither |

The PolicyEngine contained eight strict Cedar policies over five tools:
subject-only, group-only, subject OR group, subject AND group, and an unpermitted
default-deny tool. Every policy named the exact
`AgentCore::Action::"<TargetName>___<ToolName>"` and exact Gateway ARN.

AWS documents JWT claims as OAuth principal tags but does not document the
representation of array-valued claims. The spike therefore tested one bounded
candidate for `cognito:groups`: `hasTag` plus a delimiter-aware quoted-element
match. The exact group positive passed for `alpha` and `beta`; both live
collision-group negatives passed for `gamma` and `delta`. Product integration
must preserve these positive and collision-negative twins.

## Live results

| Assertion | Result |
|---|---|
| Policy creation | 8/8 reached `ACTIVE` under `FAIL_ON_ANY_FINDINGS` |
| Tool decisions | 20/20 matched: 8 allow, 12 deny |
| `tools/list` | 4/4 profiles exactly matched; `delta` correctly saw an empty list |
| Direct `tools/call` | unpermitted qualified tool denied by PolicyEngine |
| Group candidate | exact members allowed; prefix/suffix collision groups denied |
| Missing header | HTTP 401 |
| Malformed compact JWS | HTTP 401 |
| `alg=none` JWS | HTTP 401 |
| Forged `sub` with stale signature | HTTP 403 |
| Forged `cognito:groups` with stale signature | HTTP 403 |
| Valid token from disallowed client | HTTP 403 |
| Expired valid token | HTTP 403 after 360-second wait |
| Mode rollback | `ENFORCE:DENY → LOG_ONLY:ALLOW → ENFORCE:DENY` |
| Evidence unknowns/failures | 0 / 0 |

An allowed call required the deterministic Lambda marker. A Lambda or transport
error that did not name a PolicyEngine denial failed the run instead of being
counted as authorization evidence.

## Propagation behavior

Two real IAM propagation shapes were observed and then passed under narrowly
bounded retries:

1. Lambda `AddPermission` rejected the freshly created Gateway role as an
   invalid principal. The final run succeeded on attempt 2 after 15.9 seconds.
2. Gateway association initially returned `ValidationException` with an exact
   `GetPolicyEngine` access-denied message for the fresh Gateway role policy.
   The final run succeeded on attempt 2 after 15.8 seconds.

Every retry checks an exact resource/statement identity first. Unrelated
`ValidationException` and `InvalidParameterValueException` messages fail
immediately. Gateway and target creation each succeeded on the first final-run
attempt.

## Cleanup and independent inventory

The `all` command deleted the target, Gateway, policies, PolicyEngine, Lambda,
IAM roles, tagged log group, and Cognito pool in `finally`. It polled every
asynchronous policy deletion before deleting the engine.

A second cleanup-only run appended another empty inventory without overwriting
the terminal `passed` verdict. Direct read-only AWS CLI checks, separate from
the runner's inventory helper, then confirmed:

- Gateways: 0
- PolicyEngines: 0
- Cognito user pools: 0
- Lambda functions: 0
- CloudWatch log groups: 0
- IAM roles: 0

## Defects found and closed

1. Fresh IAM roles require a safe, idempotency-aware Lambda permission retry.
2. Fresh `GetPolicyEngine` role grants can surface as a message-qualified
   `ValidationException` during Gateway association.
3. AgentCore returns a mixed, case-specific 401/403 JWT rejection contract;
   arbitrary non-200 responses are not sufficient evidence.
4. A later cleanup-only run must not overwrite an earlier terminal pass/fail.
5. Explicit state/evidence paths must resolve under session scratch, including
   protection against symlink escapes.

Each correction was committed to the feature branch, passed PR CI, and was then
re-exercised live. Every failed attempt also cleaned to zero residue and was
followed by an independent cleanup-only inventory before the next revision.

## Bounded conclusion and remaining gates

This proves the current AgentCore Gateway PolicyEngine API, Cognito `sub` and
`cognito:groups` behavior, exact list/call enforcement, mode rollback, and clean
teardown for the pinned SDK and one `us-west-2` Platform compatibility account.
It does **not** yet prove:

- deployment through the Workload pipeline;
- the real Workstream Gateway, Runtime, Memory, Registry, or generated agent;
- retirement of the Lambda Cedar wrapper after pipeline parity and rollback;
- EMEA regional compatibility;
- cross-account, cross-tenant, load, concurrency, quota, or chaos behavior;
- SCPs 01–12, workload canary recovery, OTEL correlation, or final environment
  teardown.

Those remain release blockers. This evidence is the API-contract prerequisite
for implementing the pipeline-owned Workstream vertical slice; it is not a
wide-adoption readiness claim.
