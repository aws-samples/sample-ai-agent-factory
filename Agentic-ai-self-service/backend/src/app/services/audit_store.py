"""Audit event store (Phase 5 — Loom-inspired admin analytics).

Append-only record of who did what on the control plane: an admin dashboard
reads it to see action counts, per-user activity, and a session timeline.

Table (single-table):
  PK ``org_id``, SK ``<ts_iso>#<event_id>`` (sortable, newest last).
  Development rows carry a 90-day TTL; production rows do not expire.

Every write is a conditional create and carries a canonical SHA-256 integrity
digest. Production middleware appends an ``attempt`` before route execution and
refuses the request if that evidence cannot be written, then appends a correlated
``outcome``. DynamoDB is an append-only, tamper-evident query projection within
the platform IAM boundary; it is not claimed as S3 Object Lock/WORM evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

from app.services.production_safety import is_production_like_environment

logger = logging.getLogger(__name__)

DEFAULT_ORG_ID = "default"
_TTL_SECONDS = 90 * 24 * 3600


class AuditIntegrityError(RuntimeError):
    """A persisted audit record no longer matches its canonical digest."""


# Fixed action-type vocabulary. ``WRITE`` matches every mutating HTTP method;
# method-specific entries can still override it via longest-prefix ordering.
ACTION_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("POST", "/api/deploy", "agent.deploy"),
    ("DELETE", "/api/runtime", "agent.delete"),
    ("POST", "/api/registry", "registry.publish"),
    ("PUT", "/api/registry", "registry.update"),
    ("DELETE", "/api/registry", "registry.delete"),
    ("DELETE", "/api/cost/budgets", "budget.delete"),
    ("WRITE", "/api/runtimes", "agent.lifecycle.write"),
    ("WRITE", "/api/runtime", "agent.runtime.write"),
    ("WRITE", "/api/registry", "registry.write"),
    ("WRITE", "/api/settings/tag-profiles", "tag.profile.write"),
    ("WRITE", "/api/settings/tags", "tag.policy.write"),
    ("WRITE", "/api/settings/approval-policies", "approval.policy.write"),
    ("WRITE", "/api/settings/vpc-profiles", "vpc.profile.write"),
    ("WRITE", "/api/cost/budgets", "budget.write"),
    ("WRITE", "/api/prompts", "prompt.write"),
    ("WRITE", "/api/hitl", "hitl.decision"),
    ("WRITE", "/api/permissions", "permission.write"),
    ("WRITE", "/api/admin/deploy-targets", "deploy.target.write"),
    ("WRITE", "/api/workflows", "workflow.write"),
    ("WRITE", "/api/flows", "flow.write"),
    ("WRITE", "/api/provider-credentials", "provider.credential.write"),
    ("WRITE", "/api/observability", "observability.write"),
    ("WRITE", "/api/generate-tool", "tool.generate"),
    ("WRITE", "/api/test-tool", "tool.test"),
    ("WRITE", "/api/test-runtime", "agent.invoke"),
    ("WRITE", "/api/test-mcp-runtime", "mcp.invoke"),
    ("WRITE", "/api/generate-canvas", "canvas.generate"),
    ("WRITE", "/api/generate-cfn-template", "export.generate"),
    ("WRITE", "/api/export-python", "export.generate"),
    ("WRITE", "/api/identity", "identity.test"),
    ("WRITE", "/hooks", "trigger.invoke"),
)


def classify_action(method: str, path: str) -> str | None:
    """Return a fixed action label for an auditable (method, path), else None.

    Longest-prefix match so /api/settings/tag-profiles beats /api/settings.
    Only WRITE-ish routes are audited (reads are noise).
    """
    method = (method or "").upper()
    is_write = method in {"POST", "PUT", "PATCH", "DELETE"}
    best: str | None = None
    best_len = -1
    for configured_method, prefix, action in ACTION_ROUTES:
        method_matches = configured_method == method or (configured_method == "WRITE" and is_write)
        if method_matches and path.startswith(prefix) and len(prefix) > best_len:
            best, best_len = action, len(prefix)
    return best


@dataclass
class AuditEvent:
    org_id: str
    actor_sub: str
    action: str
    method: str
    path: str
    status_code: int
    ts: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_uuid: str | None = None
    phase: str = "outcome"
    correlation_id: str | None = None
    integrity_verified: bool = field(default=True, compare=False, repr=False)

    @property
    def sk(self) -> str:
        return f"{self.ts}#{self.event_id}"

    def _integrity_document(self) -> dict:
        return {
            "org_id": self.org_id,
            "sk": self.sk,
            "actor_sub": self.actor_sub or "unknown",
            "action": self.action,
            "method": self.method,
            "path": self.path,
            "status_code": int(self.status_code),
            "ts": self.ts,
            "event_id": self.event_id,
            "session_uuid": self.session_uuid or "",
            "phase": self.phase,
            "correlation_id": self.correlation_id or "",
        }

    def integrity_sha256(self) -> str:
        canonical = json.dumps(
            self._integrity_document(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_item(self, *, ttl_seconds: int | None = _TTL_SECONDS) -> dict:
        item = self._integrity_document()
        item["integrity_sha256"] = self.integrity_sha256()
        if ttl_seconds is not None:
            item["ttl"] = int(time.time()) + ttl_seconds
        return item

    def to_public_item(self) -> dict:
        item = self._integrity_document()
        item["integrity_verified"] = self.integrity_verified
        item["integrity_sha256"] = self.integrity_sha256() if self.integrity_verified else None
        return item

    @classmethod
    def from_item(cls, item: dict) -> AuditEvent:
        event = cls(
            org_id=item["org_id"],
            actor_sub=item.get("actor_sub", ""),
            action=item.get("action", ""),
            method=item.get("method", ""),
            path=item.get("path", ""),
            status_code=int(item.get("status_code", 0)),
            ts=item.get("ts", ""),
            event_id=item.get("event_id", ""),
            session_uuid=item.get("session_uuid") or None,
            phase=item.get("phase") or "outcome",
            correlation_id=item.get("correlation_id") or None,
        )
        stored_digest = item.get("integrity_sha256")
        if stored_digest is None:
            event.integrity_verified = False
        elif stored_digest != event.integrity_sha256():
            raise AuditIntegrityError(f"Audit event {event.event_id} failed integrity verification")
        return event


class AuditStore:
    def __init__(
        self,
        table_name: str,
        region: str,
        *,
        ttl_seconds: int | None = _TTL_SECONDS,
    ) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self._ttl_seconds = ttl_seconds

    def record(self, event: AuditEvent) -> None:
        if not event.ts:
            event.ts = datetime.now(timezone.utc).isoformat()
        self._table.put_item(
            Item=event.to_item(ttl_seconds=self._ttl_seconds),
            ConditionExpression="attribute_not_exists(org_id) AND attribute_not_exists(sk)",
        )

    def list_recent(self, org_id: str, limit: int = 200) -> list[AuditEvent]:
        """Most-recent-first events for the org (bounded)."""
        resp = self._table.query(
            KeyConditionExpression=Key("org_id").eq(org_id),
            ScanIndexForward=False,  # newest first
            Limit=max(1, min(limit, 1000)),
        )
        return [AuditEvent.from_item(i) for i in resp.get("Items", [])]

    def summarize(self, org_id: str, limit: int = 500) -> dict:
        """Return audit analytics over recent events.

        Loom-study 5.2 adds time-series (``by_day``) + session/actor counts on top
        of the original by_action/by_actor rollups, so the admin dashboard can
        chart activity-over-time and distinct-session usage — not just flat lists.
        """
        events = self.list_recent(org_id, limit=limit)
        outcomes = [event for event in events if event.phase != "attempt"]
        outcome_correlations = {event.correlation_id for event in outcomes if event.correlation_id}
        incomplete_attempts = [
            event
            for event in events
            if event.phase == "attempt" and event.correlation_id and event.correlation_id not in outcome_correlations
        ]
        unverified_legacy_events = [event for event in events if not event.integrity_verified]
        by_action: dict[str, int] = {}
        by_actor: dict[str, int] = {}
        by_day: dict[str, int] = {}
        sessions: set[str] = set()
        for e in outcomes:
            by_action[e.action] = by_action.get(e.action, 0) + 1
            by_actor[e.actor_sub] = by_actor.get(e.actor_sub, 0) + 1
            # ts is ISO 8601 (e.g. 2026-07-16T...); the date prefix is the day.
            day = (e.ts or "")[:10]
            if day:
                by_day[day] = by_day.get(day, 0) + 1
            if e.session_uuid:
                sessions.add(e.session_uuid)
        # by_day as a sorted list of {day, count} — chart-ready.
        by_day_series = [{"day": d, "count": c} for d, c in sorted(by_day.items())]
        return {
            "total": len(outcomes),
            "audit_records": len(events),
            "incomplete_attempts": len(incomplete_attempts),
            "unverified_legacy_events": len(unverified_legacy_events),
            "distinct_actors": len(by_actor),
            "distinct_sessions": len(sessions),
            "by_action": by_action,
            "by_actor": by_actor,
            "by_day": by_day_series,
            "events": [e.to_public_item() for e in outcomes[:100]],
            "incomplete_events": [e.to_public_item() for e in incomplete_attempts[:100]],
        }


_store: AuditStore | None = None


def get_audit_store() -> AuditStore:
    global _store
    if _store is None:
        _store = AuditStore(
            table_name=os.environ.get("AUDIT_TABLE_NAME", "Audit"),
            region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
            ttl_seconds=None if is_production_like_environment() else _TTL_SECONDS,
        )
    return _store
