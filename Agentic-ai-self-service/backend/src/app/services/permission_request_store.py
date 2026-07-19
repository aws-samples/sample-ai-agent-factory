"""JIT IAM permission-request store (Loom-study 1.6).

A builder requests specific IAM actions + resources on a managed role with a
justification; a security approver approves (which widens the role's inline
policy) or rejects. This store persists the request lifecycle:

    PENDING → APPROVED | REJECTED

DynamoDB: PK ``org_id``, SK ``request_id`` (sortable). GSI
``status-request_id-index`` powers the admin pending-review queue. No TTL — the
history is an auditable escalation trail. Mirrors the hitl_store shape/idioms.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

import boto3


def _new_request_id() -> str:
    # Sortable-ish: epoch-hex prefix + random suffix (matches versions_store style).
    return f"pr-{int(time.time()):08x}-{uuid.uuid4().hex[:12]}"


@dataclass
class PermissionRequest:
    org_id: str
    request_id: str
    requester_sub: str
    role_name: str
    actions: list[str]
    resources: list[str]
    justification: str
    status: str = "PENDING"  # PENDING | APPROVED | REJECTED
    created_at: str = ""
    decided_by: str | None = None
    decided_at: str | None = None
    decision_reason: str | None = None
    extra: dict = field(default_factory=dict)

    def to_item(self) -> dict:
        return {
            "org_id": self.org_id,
            "request_id": self.request_id,
            "requester_sub": self.requester_sub,
            "role_name": self.role_name,
            "actions": self.actions,
            "resources": self.resources,
            "justification": self.justification,
            "status": self.status,
            "created_at": self.created_at,
            "decided_by": self.decided_by or "",
            "decided_at": self.decided_at or "",
            "decision_reason": self.decision_reason or "",
        }

    @classmethod
    def from_item(cls, item: dict) -> PermissionRequest:
        return cls(
            org_id=item.get("org_id", ""),
            request_id=item.get("request_id", ""),
            requester_sub=item.get("requester_sub", ""),
            role_name=item.get("role_name", ""),
            actions=list(item.get("actions", [])),
            resources=list(item.get("resources", [])),
            justification=item.get("justification", ""),
            status=item.get("status", "PENDING"),
            created_at=item.get("created_at", ""),
            decided_by=item.get("decided_by") or None,
            decided_at=item.get("decided_at") or None,
            decision_reason=item.get("decision_reason") or None,
        )


class PermissionRequestNotPending(Exception):
    """Raised when approve/reject targets a request not in PENDING."""


class PermissionRequestStore:
    GSI_NAME = "status-request_id-index"

    def __init__(self, table_name: str, region: str) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def create(
        self,
        *,
        org_id: str,
        requester_sub: str,
        role_name: str,
        actions: list[str],
        resources: list[str],
        justification: str,
    ) -> PermissionRequest:
        from datetime import datetime, timezone

        req = PermissionRequest(
            org_id=org_id,
            request_id=_new_request_id(),
            requester_sub=requester_sub,
            role_name=role_name,
            actions=actions,
            resources=resources,
            justification=justification,
            status="PENDING",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._table.put_item(Item=req.to_item())
        return req

    def get(self, org_id: str, request_id: str) -> PermissionRequest | None:
        item = self._table.get_item(Key={"org_id": org_id, "request_id": request_id}).get("Item")
        return PermissionRequest.from_item(item) if item else None

    def list_pending(self) -> list[PermissionRequest]:
        """All PENDING requests across the org (admin queue) via the status GSI."""
        items: list[dict] = []
        kwargs: dict = {
            "IndexName": self.GSI_NAME,
            "KeyConditionExpression": "#s = :p",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":p": "PENDING"},
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [PermissionRequest.from_item(i) for i in items]

    def decide(
        self, org_id: str, request_id: str, *, status: str, decided_by: str, reason: str = ""
    ) -> PermissionRequest:
        """Transition PENDING → APPROVED|REJECTED. Raises if not pending."""
        from datetime import datetime, timezone

        current = self.get(org_id, request_id)
        if current is None:
            raise KeyError(request_id)
        if current.status != "PENDING":
            raise PermissionRequestNotPending(current.status)
        current.status = status
        current.decided_by = decided_by
        current.decided_at = datetime.now(timezone.utc).isoformat()
        current.decision_reason = reason
        self._table.put_item(Item=current.to_item())
        return current
