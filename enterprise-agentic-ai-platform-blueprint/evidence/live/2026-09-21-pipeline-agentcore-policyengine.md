# Live evidence — pipeline-owned AgentCore Gateway PolicyEngine

- **Date:** 2026-09-21
- **Status:** PASS for the bounded pipeline-owned PolicyEngine envelope below
- **Deployed Git HEAD:** `f45a12c4aadb5f4002e533398339ef2d2e32de8d`
- **Region:** `us-west-2`
- **Topology:** one Platform account and one Workstream account, with environment-isolated nonproduction and production resources
- **Retained rollback control:** `@agenticai/tool-cedar-wrapper`

This is a sanitized summary. It contains no AWS account IDs, credentials,
Gateway, PolicyEngine, target, policy, KMS-key, pipeline-execution, approval-token,
or other account-scoped resource identifiers. Raw evidence remained in session
scratch and was not committed.

## Preconditions and local gates

Before live deployment:

- the opt-in `OFF | LOG_ONLY | ENFORCE` implementation passed the TypeScript
  build, scoped lint and formatting, leakage scrub, and diff checks;
- 34 Workstream Gateway, 46 Workload pipeline, and 15 fail-closed teardown tests
  passed serially;
- strict two-environment synthesis passed both `AwsSolutionsChecks` and
  `NIST80053R5Checks`;
- the exact `OFF` template remained equal to the R2 rollback template and
  retained semantic search;
- both enabled modes emitted MCP `2025-06-18` without `searchType`;
- two independent security/API and CloudFormation/lifecycle reviews found no
  Critical or High issue; and
- exact-head JavaScript/TypeScript and Python CodeQL passed before deployment.

The Lambda Cedar wrapper remained active throughout the campaign. No test or
cleanup step removed its code, functions, aliases, Registry records, or DynamoDB
rollback tables.

## Live-only defects closed before promotion

Three live-only contracts were discovered and fixed before production:

1. AgentCore's `GenesisPolicyEngineCheck` assumed the new Gateway role but its
   `GetPolicyEngine` call reached KMS without an applicable identity-based
   decrypt allow. Exact-key `kms:Decrypt` was retained while the inapplicable
   FAS-oriented identity condition was removed. The CMK key policy and both
   service-created grants retained service, source, operation, and exact
   PolicyEngine encryption-context constraints.
2. AgentCore validates native Cedar action names against the Gateway's live
   target schema. Creation was reordered to `LOG_ONLY` association → targets →
   exact target `READY` barrier → strict policies → requested mode. The waiter
   pins the service-minted target identity and expected name, signs the modeled
   trailing-slash URI, and fails immediately on identity drift, terminal state,
   or `*_PENDING_AUTH`.
3. Under `ENFORCE`, an unpermitted principal received an empty `tools/list` and
   direct policy denials, but optional semantic search still returned both
   unauthorized tool schemas. Commit `f45a12c` retained
   `searchType: SEMANTIC` only in exact `OFF` rollback mode and omitted it from
   `LOG_ONLY` and `ENFORCE` create and update payloads.

Each repair received independent review, targeted tests, strict synthesis,
exact-head CI, a fresh pipeline deployment, live positive/adversarial checks,
and rollback or cleanup before the next readiness claim.

## Nonproduction deployment and mode rollback

The pipeline first deployed nonproduction in `LOG_ONLY`. The Gateway reached
`READY`, both targets reached `READY`, the PolicyEngine reached `ACTIVE`, and two
`FAIL_ON_ANY_FINDINGS` / `ACTIVE` policies reached `ACTIVE`. CloudFormation
observed the intended association-before-target and target-before-policy order.

The same physical resources completed:

`LOG_ONLY → ENFORCE → LOG_ONLY → ENFORCE`

Every transition changed only the PolicyEngine mode mutation and readiness
resources. Gateway, PolicyEngine, target, policy, and CMK identities remained
stable.

Positive behavior passed MCP initialization, an exact two-tool `tools/list`,
echo and ping calls, required-protocol handling, and authentication. A missing
protocol header returned HTTP 400; an unsigned request returned HTTP 401; an
unknown tool was denied; and direct cross-account Lambda bypass calls were
denied.

A temporary pipeline-owned principal mismatch then proved the native PolicyEngine
boundary. The current caller received an empty `tools/list`, and direct echo and
ping calls returned explicit `policy enforcement` denials before the Lambda
wrapper or tool code ran. The intended caller was restored immediately and the
positive matrix passed again.

## Semantic-search repair

The exact `f45a12c` pipeline updated the existing nonproduction Gateway in place.
The live protocol configuration changed from MCP with semantic search to MCP
`2025-06-18` with no `searchType`. No Gateway, PolicyEngine, target, policy, or
CMK identity changed.

For the permitted caller:

- `tools/list` contained exactly echo and ping;
- echo and ping succeeded;
- the built-in semantic-search action was absent;
- calling that action returned an unknown/error result; and
- the response disclosed no tool name, input schema, description, or other tool
  metadata.

The temporary principal mismatch was repeated after the repair. `tools/list`
was empty, both direct target calls were explicitly policy-denied, semantic
search remained absent, and zero metadata was disclosed. The intended principal
was restored and the complete positive matrix passed again.

## Production deployment and adversarial behavior

Production was entered only after exact-head CI, fresh Registry resolution,
no-op stable-role stages, exact alias-principal verification, successful
nonproduction in-place removal, positive behavior, principal-mismatch denial,
and intended-principal restoration.

The production change set contained 74 additions, zero modifications, and zero
removals. CloudFormation observed the required sequence:

1. `LOG_ONLY` association;
2. both targets created and reported `READY`;
3. exact target-readiness validation;
4. both strict policies became `ACTIVE`; and
5. the Gateway converged to `READY + ENFORCE`.

Production passed the same exact two-tool list, echo, ping, HTTP 400/401,
unknown-tool, absent-search, and zero-metadata assertions. A temporary
pipeline-owned production principal mismatch changed only the two native policy
resources without replacement. The current caller then received an empty list,
explicit policy-enforcement denials for echo and ping, and no semantic-search
surface or metadata. The exact reverse update restored the intended principal,
left every infrastructure identity stable, and passed the final positive matrix.

## Fail-closed teardown and residual inventory

Production and nonproduction ToolGateway stacks were deleted separately. In both
environments, deleted-stack events established this order:

1. the requested `ENFORCE` mode was held while mode-ready/mutation delete hooks
   completed without weakening enforcement;
2. both native policies deleted;
3. target-readiness teardown completed;
4. both targets deleted;
5. the signed zero-target barrier completed;
6. the Gateway rolled to `LOG_ONLY`;
7. the association detached and detach readiness completed; and
8. only then did the Gateway and PolicyEngine delete.

For each PolicyEngine CMK, both operation/context-constrained service grants
retired, its alias disappeared, and the key entered the configured seven-day
`PendingDeletion` window.

The Platform pipeline then removed exactly four cross-account Lambda alias
permissions—two per environment. Hash comparison proved every non-permission
Registry resource unchanged. The four tool aliases, tool functions, native
Registry records, retained DynamoDB rollback tables, and Lambda Cedar wrapper
remain.

After permission retirement, both stable-role stacks and all six Workstream
roles were deleted. The Workload pipeline root then deleted its pipeline, three
CodeBuild projects, IAM roles, generated Lambda, and artifact bucket; its CMK
entered the expected seven-day pending-deletion window.

The exact deleted-stack-history cleanup recovered every generated Lambda and
CodeBuild physical ID, verified each owning resource absent and each surviving
log group empty, and deleted only those exact groups. Independent final
inventories found:

- no scoped Workstream stacks, Gateways, targets, PolicyEngines, policies, IAM
  roles, generated functions, state machines, or log groups;
- no Platform Workload pipeline stack, pipeline, build project, generated
  Lambda, artifact bucket, alias permission, or associated service log group;
- only the expected seven-day pending-deletion keys from the successful and
  failed-closed attempts; and
- the intentionally retained Platform pipeline, Registries, approved records,
  four tool aliases/functions, DynamoDB rollback tables, and Lambda Cedar
  wrapper.

## Bounded conclusion and remaining gates

This proves the pipeline-owned AgentCore Gateway PolicyEngine path in one
`us-west-2` two-account topology: exact IAM principals, strict per-tool Cedar,
CMK controls, `LOG_ONLY` deployment, `ENFORCE` promotion, mode rollback,
in-place semantic-search removal, positive and direct-policy-denial twins in
both environments, intended-principal restoration, fail-closed teardown, grant
retirement, and zero unintended residue.

The Lambda Cedar wrapper is intentionally retained as a rollback and defense-in-
depth control. Retiring it is a separate maintainer decision, not an automatic
consequence of this campaign.

This bounded result does **not** prove AgentCore Runtime or Memory integration,
a live legacy-consumer redeployment, SCPs 01–12 at organization scope, an EMEA
region matrix, load/concurrency/quota/soak/chaos/upgrade campaigns, Gateway OTEL
span correlation, or a measured 24-hour cost baseline.
