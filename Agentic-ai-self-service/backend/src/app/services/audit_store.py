"""Audit event store (Phase 5 — Loom-inspired admin analytics).

Append-only record of who did what on the control plane: an admin dashboard
reads it to see action counts, per-user activity, and a session timeline.

Table (single-table):
  PK ``org_id``, SK ``<ts_iso>#<event_id>`` (sortable, newest last).
  GSI ``actor-ts-index`` (actor_sub → ts_sk) for per-user queries.
  TTL on ``ttl`` (90-day) bounds growth.

Emitted from a lightweight FastAPI middleware (deployment_handler) on the
enterprise write routes — a small FIXED action-type enum keeps the signal clean
(vs logging every request). Best-effort: an audit-write failure NEVER breaks the
underlying request.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

DEFAULT_ORG_ID = "default"
_TTL_SECONDS = 90 * 24 * 3600

# Fixed action-type vocabulary. Map (METHOD, path-prefix) → action so the audit
# log is a small, queryable set rather than raw request lines.
ACTION_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("POST", "/api/deploy", "agent.deploy"),
    ("DELETE", "/api/runtime", "agent.delete"),
    ("POST", "/api/registry", "registry.publish"),
    ("POST", "/api/registry", "registry.action"),  # approve/reject/clone (sub-path)
    ("PUT", "/api/registry", "registry.update"),
    ("DELETE", "/api/registry", "registry.delete"),
    ("POST", "/api/settings/tags", "tag.policy.write"),
    ("POST", "/api/settings/tag-profiles", "tag.profile.write"),
    ("POST", "/api/cost/budgets", "budget.write"),
    ("DELETE", "/api/cost/budgets", "budget.delete"),
    ("POST", "/api/prompts", "prompt.write"),
    ("POST", "/api/hitl", "hitl.decision"),
)


def classify_action(method: str, path: str) -> str | None:
    """Return a fixed action label for an auditable (method, path), else None.

    Longest-prefix match so /api/settings/tag-profiles beats /api/settings.
    Only WRITE-ish routes are audited (reads are noise).
    """
    method = (method or "").upper()
    best: str | None = None
    best_len = -1
    for m, prefix, action in ACTION_ROUTES:
        if m == method and path.startswith(prefix) and len(prefix) > best_len:
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

    @property
    def sk(self) -> str:
        return f"{self.ts}#{self.event_id}"

    def to_item(self) -> dict:
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
            "ttl": int(time.time()) + _TTL_SECONDS,
        }

    @classmethod
    def from_item(cls, item: dict) -> AuditEvent:
        return cls(
            org_id=item["org_id"],
            actor_sub=item.get("actor_sub", ""),
            action=item.get("action", ""),
            method=item.get("method", ""),
            path=item.get("path", ""),
            status_code=int(item.get("status_code", 0)),
            ts=item.get("ts", ""),
            event_id=item.get("event_id", ""),
            session_uuid=item.get("session_uuid") or None,
        )


class AuditStore:
    def __init__(self, table_name: str, region: str) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def record(self, event: AuditEvent) -> None:
        if not event.ts:
            event.ts = datetime.now(timezone.utc).isoformat()
        self._table.put_item(Item=event.to_item())

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
        by_action: dict[str, int] = {}
        by_actor: dict[str, int] = {}
        by_day: dict[str, int] = {}
        sessions: set[str] = set()
        for e in events:
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
            "total": len(events),
            "distinct_actors": len(by_actor),
            "distinct_sessions": len(sessions),
            "by_action": by_action,
            "by_actor": by_actor,
            "by_day": by_day_series,
            "events": [e.to_item() for e in events[:100]],
        }


_store: AuditStore | None = None


def get_audit_store() -> AuditStore:
    global _store
    if _store is None:
        _store = AuditStore(
            table_name=os.environ.get("AUDIT_TABLE_NAME", "Audit"),
            region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
        )
    return _store
