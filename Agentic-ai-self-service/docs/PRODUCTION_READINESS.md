# Production Readiness

Status of the enterprise/governance platform (Loom-inspired 7-phase build +
Phase-7 cross-account wiring + hardening). Honest assessment: what's proven,
what's opt-in, and the known limits.

## Proven & live-verified (dev stack, real AWS)

| Capability | Evidence |
|---|---|
| Scope-based RBAC/ABAC | 403-by-scope proven live (advisory→enforce toggle) |
| Tag policies + deploy-time enforcement | required-tag deploy blocked at 400 |
| OBO identity propagation (RFC 8693) | OBO credential-provider config accepted by AWS (+ negative test) |
| Cost budgets / FinOps | budget CRUD + breach status live |
| Audit trail + OTEL trace waterfall | write actions captured with actor |
| AWS Agent Registry federation | full DRAFT→APPROVED lifecycle on the real service |
| Multi-region / multi-account deploy (opt-in) | **agent deployed into a 2nd account; runtime confirmed present** |
| Same-account deploy + invoke | regression gate green after all changes |

Test depth: ~1009 backend unit tests, full pattern-matrix + baseline agent
deploy/invoke, cross-account deploy across two real accounts.

## Operational hardening (this pass)

- **Alarms → SNS topic** (`*-alarms`, SSE+TLS): Lambda errors/throttles/p99,
  DynamoDB throttles on all governance tables, Step Functions deploy failures.
  Subscribe an ops endpoint to the topic to receive them.
- **RBAC rollout:** advisory-by-default + a `WouldDeny` CloudWatch metric — see
  `RBAC_ROLLOUT.md`.
- **Budget breach metric** (`<proj>/<env>/finops` `BudgetBreach`) emitted on
  cost read; alarm on it to page on overspend (no poller needed).
- **Data retention / PII:** see `DATA_RETENTION.md` (90-day TTLs, `sub`-only,
  admin-gated audit).

## Known limits / prerequisites (not blockers, but plan for them)

- **Cross-account onboarding is manual per target account:** pre-create
  `AgentCoreFlowsDeploymentRole`, `AgentCoreFlowsRuntimeRole`, and the
  `agentcore-flows-artifacts-<acct>-<region>` bucket (see
  `cross-account-deploy-role.json`), add the account id to
  `-c deploy_target_accounts=`, redeploy, then register via the admin API.
  Off by default.
- **OBO end-to-end** needs a token-exchange IdP (Entra/Okta/Auth0); Cognito
  can't do the final user-delegated hop. The platform mechanism is proven.
- **Single region / single deploy state machine** per stack. DDB tables are
  PAY_PER_REQUEST (auto-scale). No load test at production scale yet — size the
  Lambda reserved-concurrency + Bedrock quotas before high-volume launch.
- **Not yet merged/reviewed:** work sits on `feat/loom-enterprise-governance`;
  open a PR for review + CI before any production cutover.

## Pre-launch checklist

- [ ] Subscribe an ops contact to the `*-alarms` SNS topic.
- [ ] Run the RBAC advisory→enforce rollout (`RBAC_ROLLOUT.md`).
- [ ] Set required tag policies + a default tag profile (governance).
- [ ] Configure cost budgets + a `BudgetBreach` alarm.
- [ ] Load/soak test at expected peak; confirm Bedrock + Lambda quotas.
- [ ] PR + security review of `feat/loom-enterprise-governance`.
