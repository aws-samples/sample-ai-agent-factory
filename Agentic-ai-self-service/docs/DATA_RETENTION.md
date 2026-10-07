# Data Retention & PII Notes

[← Back to README](../README.md)

Covers the governance data stores added in the enterprise/governance layer.

## Audit trail (`services/audit_store.py`, table `*-audit`)

- **What's stored:** correlated `attempt` and `outcome` rows for every classified
  control-plane write across both API Lambdas: `actor_sub`, fixed `action`,
  `method`, `path`, `status_code`, timestamp, session/correlation IDs, and a
  canonical SHA-256 integrity digest. Request bodies, tokens, and secrets are
  never stored.
- **Write contract:** each row is a conditional create; duplicate keys cannot
  overwrite evidence. Both Lambda roles can `PutItem` but are explicitly denied
  `UpdateItem`, `DeleteItem`, and `BatchWriteItem`. Production writes must append
  the attempt before route execution or the route returns 503 without running.
  Public webhooks append only after HMAC/replay validation and before enqueue.
  A missing correlated outcome remains visible as an incomplete attempt.
- **Integrity:** reads recompute every stored digest and production returns 503
  on mismatch instead of fabricating an empty trail. Pre-upgrade rows without
  digests remain visible but are explicitly counted as unverified until their
  legacy TTL expires. This is append-only and
  tamper-evident inside the platform IAM boundary; it is **not** an S3 Object
  Lock/WORM compliance claim against account administrators.
- **PII assessment:** `actor_sub` is a Cognito `sub`: opaque and pseudonymous,
  but still a persistent user identifier and therefore personal data in many
  privacy regimes. Treat it accordingly.
- **Retention:** production events omit the `ttl` attribute and the table is
  retained/deletion-protected. DynamoDB TTL processing stays enabled so legacy
  and development rows carrying `ttl` still expire after 90 days.
- **Access:** `GET /api/admin/audit` requires the `admin` scope. Storage or
  integrity failures are surfaced, not rendered as an empty audit history.

## Cost / usage (`services/cost_tracking.py`, table `*-usage-events`)

- Token counts + priced cost per invocation; `owner_sub` for attribution.
- 90-day TTL. Read via `GET /api/runtimes/{name}/cost` (owner-scoped, 404 cross
  tenant). Budgets (`*-budget` table) hold only limit config, no usage data.

## Tag policies / profiles (`*-tag-policy`)

- Org-wide governance config (tag keys/values). No user data. No TTL (config).

## Deletion / right-to-erasure

- User-owned operational rows are keyed by `owner_sub` and can be removed through
  their resource lifecycle.
- New production audit events intentionally have no TTL and are append-only to the
  platform roles. Privacy/legal owners must define a retention and erasure basis
  for pseudonymous `actor_sub` before adoption; changing retained audit evidence
  requires a separately authorized administrative process outside this sample.
- Development audit and usage-event rows remain TTL-bounded.

## Encryption

- All tables use AWS-managed SSE (DynamoDB default). The SNS alarm topic uses
  SSE (`alias/aws/sns`) + enforced TLS. Artifacts/logging S3 buckets are
  `BlockPublicAccess.BLOCK_ALL` + enforce-SSL.
