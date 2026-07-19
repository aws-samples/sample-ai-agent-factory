# Data Retention & PII Notes

[← Back to README](../README.md)

Covers the governance data stores added in the enterprise/governance layer.

## Audit trail (`services/audit_store.py`, table `*-audit`)

- **What's stored:** one row per auditable control-plane WRITE action —
  `actor_sub`, `action` (fixed vocabulary), `method`, `path`, `status_code`,
  timestamp. No request bodies, no secrets.
- **PII assessment:** `actor_sub` is the Cognito **`sub`** — an opaque,
  non-reversible user identifier, not name/email/PII. It is the same identifier
  already used for tenant isolation (`owner_sub`) throughout the platform.
- **Retention:** 90-day TTL (`ttl` attribute, `time_to_live_attribute="ttl"` on
  the table) — rows auto-expire. Tune in `audit_store._TTL_SECONDS`.
- **Access:** `GET /api/admin/audit` requires the `admin` scope (super-admins
  only). No end-user access.

## Cost / usage (`services/cost_tracking.py`, table `*-usage-events`)

- Token counts + priced cost per invocation; `owner_sub` for attribution.
- 90-day TTL. Read via `GET /api/runtimes/{name}/cost` (owner-scoped, 404 cross
  tenant). Budgets (`*-budget` table) hold only limit config, no usage data.

## Tag policies / profiles (`*-tag-policy`)

- Org-wide governance config (tag keys/values). No user data. No TTL (config).

## Deletion / right-to-erasure

- A user's rows are keyed by `owner_sub` / `actor_sub`. To erase a user, delete
  their rows by that key across `*-usage-events` (GSI `owner_sub-*`), `*-audit`
  (scan by `actor_sub`), and their owned workflows/registry/prompt entries. All
  are TTL-bounded regardless.

## Encryption

- All tables use AWS-managed SSE (DynamoDB default). The SNS alarm topic uses
  SSE (`alias/aws/sns`) + enforced TLS. Artifacts/logging S3 buckets are
  `BlockPublicAccess.BLOCK_ALL` + enforce-SSL.
