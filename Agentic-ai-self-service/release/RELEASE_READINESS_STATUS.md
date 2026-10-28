# PR #65 Release-Readiness Status (handoff)

Branch: `wip/scenario-port` (HEAD `41f8468`, 47 commits ahead of
`fix/self-service-production-readiness` @ `9ac7a0d`, 0 behind). **Not pushed, not merged.**
Candidate under test: `acf-production` stack `SourceCommit` = `9ac7a0d…`, deployed us-east-1,
account 048834207240.

This file is the authoritative state. It is NOT a completion claim: the goal (all 59 scenarios
live-proven + seven-Region campaign + review + merge) is **not met**.

## Verified by evidence (live, receipt-backed, zero residue)

- **48 / 59 scenarios live-proven** against deployed `9ac7a0d` in us-east-1.
- **gateway-governance: 11 / 11** — verified cycle 44, `journey.json`
  (`build/release-scenario-journeys/9ac7a0d…/us-east-1/gateway-governance/journey.json`),
  `cleanup_errors: []`. All 11 ids re-asserted PASS against the cached journey.
  Oracles gate on the forgery-resistant **receipt-digest contract** (exact tool leaf +
  per-argument SHA-256 `input_sha256`/`argument_sha256` + status + fixture dispatch count),
  ported from the audited b43627b runner's `_receipt_for`.

## Real defects found + fixed this arc (all committed on wip/scenario-port)

1. `89b7b22` — public (AuthType=NONE) Lambda Function URL needs BOTH `lambda:InvokeFunctionUrl`
   AND `lambda:InvokeFunction` (`InvokedViaFunctionUrl=True`) + `InvokeMode=BUFFERED`; absence
   makes every public call 403 at the function-invoke gate (the "fixture never invoked" blocker).
2. `d4d47f8` — `runtime_role_denial` made statement-authoritative: reads the role's
   `NeverMutateGatewayOrPolicyControlPlane` Deny over all 15 FORBIDDEN_RUNTIME_CONTROL_ACTIONS;
   IAM simulator used only as cross-check (it returns implicitDeny for policy actions simulated
   against a gateway ARN — resource-type mismatch, not a real allow).
3. `9b496b6` — journey must authorize the runtime invocation budget before probes (fixes 429);
   `g-admins-cost` group required.
4. `b4a5856` — drive runtime probes via `POST /api/test-runtime-async` (unredacted response +
   `toolReceipts`); the slot invoke redacts output and surfaces no receipts under a guardrail.
5. `cb5b7a6` — exact audited per-scenario prompts incl. the concatenation trick (blocked literal
   must NOT appear in the prompt text, or the input guardrail blocks at ingestion = wrong stage).
   This is what made the agent actually call tools (the zero-tool-call symptom was a prompt defect).
6. `eecdde9` — receipt-digest oracles (see above); responseText is evidence only, because `9ac7a0d`
   routes block text through Bedrock `blockedOutputsMessaging` (guardrails_step.py L227-228) /
   Strands output redaction, so the final-turn string is LLM/path-dependent, not a stable contract.
7. `41f8468` — same public-Function-URL grant fix applied to OBO's `_create_fixture`.
   (Earlier arc also fixed: connector auth = api_key static Bearer; guardrail
   `contentFilters:{prompt_attack:HIGH}`; deploy_body mirrors b43627b `_deployment_payload`.)

### Candidate behaviour difference (NOT a blocker, flag if you disagree)
`9ac7a0d`'s user-facing guardrail-block message differs from audited b43627b: generic
"The response was blocked by a safety guardrail." / "[Assistant output redacted.]" vs the specific
"Tool input/result blocked…". Security behaviour is intact (tool blocked, receipt status=error, no
fixture dispatch); only the human-readable string changed. Treated as acceptable messaging drift.

## Open / unproven (blocks merge under the evidence bar)

- **OBO (3 ids): localized blocker.** The `41f8468` fixture-grant fix got the journey PAST the
  fixture-never-invoked stage to the whoami assertion, which now returns a gateway `isError`
  "An internal error occurred." The managed gateway invokes the fixture via its own IAM execution
  role (`AgentCoreGateway-itobo…` inline `GatewayLambdaInvoke`), NOT the public URL — so the
  whoami failure is a separate gateway→fixture invocation / OBO-identity-forwarding issue.
  Diagnosis needs the fixture's CloudWatch logs, which teardown deletes: next step is a
  keep-fixture run (skip `_delete_fixture` log-group deletion, or set `keep_log_group=True`) →
  read the fixture log → fix. Unknown whether the root cause is product or harness.
- **ui_cloudfront (1 id):** not yet ported (reuse `browser_widget.py` Playwright harness).
- **clean_clone (2 ids):** not yet run; each is a ~3h detached `--no-hardlinks` full-gate run
  (traceability `timeout_seconds:10800`). Defer to unattended / low-memory window.

## Remaining path to merge (ordered)

1. Resolve OBO whoami (keep-fixture run → logs → fix) → OBO 3/3.
2. Port + prove ui_cloudfront (1).
3. Run clean_clone ×2 (~3h each, unattended).
4. Merge `wip/scenario-port` → `fix/self-service-production-readiness`, run all offline gates
   (ruff all 21 scenario files; `test_release_scenarios_live_binding.py`), add CHANGELOG entries
   for the fixes above, `git push origin fix/self-service-production-readiness` (feature branch
   ONLY — never main), confirm CI green for head.
5. Seven-Region certifying campaign (us-east-1, us-east-2, us-west-2, eu-central-1, eu-west-1,
   eu-west-2, eu-west-3): prepare_assemblies `--all-release-regions`, deploy 7 Regions serially,
   recorder run all suites + live scenarios per Region, finalize, clean-clone cert, teardown.
6. Independent adversarial review (subagents unmounted in this harness → in-session or a fresh
   session with `include_memory=false`).
7. Undraft + merge PR #65. **Then remind Omar to re-enable the three Kiro Crew deny rules:**
   `aws-destructive-cfn-deploy-mutate`, `aws-destructive-cfn-delete-stack`,
   `credential-exfil-s3-sync`.

## Operational lessons (so the next run doesn't repeat failures)

- **Creds vs journey length.** A journey takes ~28–30 min; the broker caps sessions well under the
  requested duration (~46–65 min observed). A near-expiry cached re-vend caused an expired-token
  teardown (cycle 43) that orphaned fixture residue. Re-vend immediately before each live run and
  confirm `aws sts get-caller-identity` = 048834207240.
- **Residue sweep after any failed teardown:** lambda `acfpr-{gg,obo}-fixture-*`, its IAM role +
  inline policy, log group `/aws/lambda/…`, SSM `/acfpr/{gg,obo}/fixture/*`, synthetic cognito
  users (email `acfpr.<label>.*@example.com` in pool `us-east-1_WYQPPnCqg` — username is the UUID
  sub, so match on the email attribute), and product gateway roles `AgentCoreGateway-it{gg,obo}*`.
- **Journey cache:** `rm -rf build/release-scenario-journeys/9ac7a0d*/us-east-1/<family>` before
  each fresh live re-run (a recorded 429 still caches a completed journey).
- Host policy: use the file-editor tool for large edits (shell heredocs trip an argv classifier);
  keep AWS queries simple (`--query … --output text`). `timeout`/subagents not available.
