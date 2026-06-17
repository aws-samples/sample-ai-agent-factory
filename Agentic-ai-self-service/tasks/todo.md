# Recover from SpringClean table reaping + make deploy self-healing (2026-06-10)

Context: `cdk deploy` of `agentcore-workflow-dev` failed with
`Unable to retrieve Arn attribute for AWS::DynamoDB::Table ... agentcore-workflow-dev-agent-versions does not exist`.
Stack is `UPDATE_ROLLBACK_COMPLETE`. ALL 10 DDB tables were reaped out-of-band
(SpringClean reaper in platform-test account — repeat of the 2026-05-29 incident,
this time every table, not just 3).

## Plan

- [x] 1. Write `scripts/preflight-ddb-restore.py`: reads the **deployed** CFN
      template (`get_template`), finds every `AWS::DynamoDB::Table`, and for any
      table CFN believes exists but is physically missing, recreates it empty
      with the exact schema (keys, GSIs, SSE, tags), then re-applies TTL + PITR.
- [x] 2. Wire the preflight into `scripts/deploy.sh` before `cdk deploy`
      (no-op on fresh deploys / healthy stacks).
- [x] 3. Run the preflight against platform-test → restored all 10 tables
      (PITR raced ContinuousBackupsUnavailableException; added retry, all enabled).
- [x] 4. Spot-check other resources — drift detection: only DDB tables DELETED
      (+ benign HttpApi MODIFIED); Lambdas/buckets/pool intact.
- [x] 5. Run full `./scripts/deploy.sh`. FIRST RUN went to us-west-2 (leaked
      AWS_REGION env var) and attempted a fresh stack create — failed on
      account-global name collisions + CLOUDFRONT WAF scope; deleted that
      ROLLBACK_COMPLETE stack, added a us-east-1 fail-fast guard to deploy.sh,
      re-ran pinned to us-east-1 → UPDATE_COMPLETE, frontend uploaded,
      CloudFront invalidated.
- [x] 6. E2E verification (main loop): SRP auth OK; /api/workflows /flows
      /prompts /registry /hitl/pending all 200; no-auth → 401; flow CRUD
      round-trip OK; POST /api/deploy → SFN SUCCEEDED → /api/test-runtime
      returned real model output ("Paris.") → runtime deleted; test user
      password scrambled after.
- [x] 7. lessons.md + memory updated.

## Review

Root cause: account reaper (SpringClean) deleted all 10 stack DDB tables
out-of-band; CFN state went stale so the next UPDATE failed resolving
`GetAtt .Arn`. Fixes shipped: (1) self-healing DDB preflight in deploy.sh,
(2) region fail-fast guard in deploy.sh. Deployment verified working
end-to-end through the live CloudFront entry point, including a real
AgentCore runtime deploy + invoke + cleanup.
