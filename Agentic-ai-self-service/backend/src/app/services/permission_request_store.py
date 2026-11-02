"""JIT IAM permission-request store (Loom-study 1.6).

A builder requests specific IAM actions + resources on a managed role with a
justification; a security approver approves (which widens the role's inline
policy) or rejects. This store persists the request lifecycle:

    PENDING → APPLYING → APPROVED
           ↘ REJECTED

``APPLYING`` is a conditional lease held across the idempotent IAM write. Rejection
can transition only from ``PENDING``; therefore a permission that may have landed can
never be recorded as rejected. An expired lease may be reclaimed to apply the same
deterministic inline policy and finalize evidence.

DynamoDB: PK ``org_id``, SK ``request_id`` (sortable). GSI
``status-request_id-index`` powers the admin pending-review queue. No TTL — the
history is an auditable escalation trail. Mirrors the hitl_store shape/idioms.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError


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
    status: str = "PENDING"  # PENDING | APPLYING | APPROVED | REJECTED
    created_at: str = ""
    decided_by: str | None = None
    decided_at: str | None = None
    decision_reason: str | None = None
    approval_token: str | None = None
    approval_started_at: str | None = None
    approval_expires_at: int | None = None
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
            approval_token=item.get("approval_token") or None,
            approval_started_at=item.get("approval_started_at") or None,
            approval_expires_at=(int(item.get("approval_expires_at") or 0) or None),
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

    def get(
        self,
        org_id: str,
        request_id: str,
        *,
        consistent: bool = False,
    ) -> PermissionRequest | None:
        item = self._table.get_item(
            Key={"org_id": org_id, "request_id": request_id},
            ConsistentRead=consistent,
        ).get("Item")
        return PermissionRequest.from_item(item) if item else None

    def list_pending(self) -> list[PermissionRequest]:
        """All PENDING/APPLYING requests across the org via the status GSI."""

        items: list[dict] = []
        for status in ("PENDING", "APPLYING"):
            kwargs: dict = {
                "IndexName": self.GSI_NAME,
                "KeyConditionExpression": "#s = :status",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {":status": status},
            }
            while True:
                resp = self._table.query(**kwargs)
                items.extend(resp.get("Items", []))
                if "LastEvaluatedKey" not in resp:
                    break
                kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        requests = [PermissionRequest.from_item(item) for item in items]
        return sorted(requests, key=lambda request: request.request_id)

    def _raise_transition_failure(self, org_id: str, request_id: str) -> None:
        current = self.get(org_id, request_id, consistent=True)
        if current is None:
            raise KeyError(request_id)
        raise PermissionRequestNotPending(current.status)

    def claim_approval(
        self,
        org_id: str,
        request_id: str,
        *,
        decided_by: str,
        reason: str = "",
        lease_seconds: int = 300,
        now: int | None = None,
    ) -> PermissionRequest:
        """Conditionally transition PENDING/expired APPLYING to a held lease."""

        if lease_seconds <= 0:
            raise ValueError("approval lease must be positive")
        now_epoch = int(time.time()) if now is None else int(now)
        token = uuid.uuid4().hex
        started_at = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
        try:
            response = self._table.update_item(
                Key={"org_id": org_id, "request_id": request_id},
                UpdateExpression=(
                    "SET #status = :applying, decided_by = :by, decision_reason = :reason, "
                    "approval_token = :token, approval_started_at = :started, "
                    "approval_expires_at = :expires"
                ),
                ConditionExpression=(
                    "attribute_exists(request_id) AND "
                    "(#status = :pending OR "
                    "(#status = :applying AND approval_expires_at <= :now))"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":pending": "PENDING",
                    ":applying": "APPLYING",
                    ":by": decided_by,
                    ":reason": reason,
                    ":token": token,
                    ":started": started_at,
                    ":expires": now_epoch + lease_seconds,
                    ":now": now_epoch,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                self._raise_transition_failure(org_id, request_id)
            raise
        except Exception:
            try:
                current = self.get(org_id, request_id, consistent=True)
            except Exception:
                current = None
            if current and current.status == "APPLYING" and current.approval_token == token:
                return current
            raise
        return PermissionRequest.from_item(response["Attributes"])

    def complete_approval(
        self,
        org_id: str,
        request_id: str,
        *,
        approval_token: str,
        status: str,
        decided_by: str,
        reason: str = "",
        now: int | None = None,
    ) -> PermissionRequest:
        """Finalize a held APPLYING lease as APPROVED or REJECTED."""

        if status not in {"APPROVED", "REJECTED"}:
            raise ValueError("approval completion status must be APPROVED or REJECTED")
        decided_at = datetime.fromtimestamp(
            int(time.time()) if now is None else int(now),
            tz=timezone.utc,
        ).isoformat()
        try:
            response = self._table.update_item(
                Key={"org_id": org_id, "request_id": request_id},
                UpdateExpression=(
                    "SET #status = :status, decided_by = :by, decided_at = :at, "
                    "decision_reason = :reason "
                    "REMOVE approval_token, approval_started_at, approval_expires_at"
                ),
                ConditionExpression="#status = :applying AND approval_token = :token",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": status,
                    ":applying": "APPLYING",
                    ":token": approval_token,
                    ":by": decided_by,
                    ":at": decided_at,
                    ":reason": reason,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                current = self.get(org_id, request_id, consistent=True)
                if current is not None and current.status == status:
                    return current
                self._raise_transition_failure(org_id, request_id)
            raise
        except Exception:
            try:
                current = self.get(org_id, request_id, consistent=True)
            except Exception:
                current = None
            if current is not None and current.status == status:
                return current
            raise
        return PermissionRequest.from_item(response["Attributes"])

    def release_approval(
        self,
        org_id: str,
        request_id: str,
        *,
        approval_token: str,
    ) -> PermissionRequest:
        """Return a held approval to PENDING after a definitive IAM refusal."""

        try:
            response = self._table.update_item(
                Key={"org_id": org_id, "request_id": request_id},
                UpdateExpression=(
                    "SET #status = :pending "
                    "REMOVE approval_token, approval_started_at, approval_expires_at, "
                    "decided_by, decision_reason"
                ),
                ConditionExpression="#status = :applying AND approval_token = :token",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":pending": "PENDING",
                    ":applying": "APPLYING",
                    ":token": approval_token,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                current = self.get(org_id, request_id, consistent=True)
                if current is not None and current.status == "PENDING":
                    return current
                self._raise_transition_failure(org_id, request_id)
            raise
        except Exception:
            try:
                current = self.get(org_id, request_id, consistent=True)
            except Exception:
                current = None
            if current is not None and current.status == "PENDING":
                return current
            raise
        return PermissionRequest.from_item(response["Attributes"])

    def decide(
        self,
        org_id: str,
        request_id: str,
        *,
        status: str,
        decided_by: str,
        reason: str = "",
    ) -> PermissionRequest:
        """Conditionally reject a PENDING request; approval requires a lease."""

        if status != "REJECTED":
            raise ValueError("approvals must use claim_approval and complete_approval")
        decided_at = datetime.now(timezone.utc).isoformat()
        try:
            response = self._table.update_item(
                Key={"org_id": org_id, "request_id": request_id},
                UpdateExpression=(
                    "SET #status = :rejected, decided_by = :by, decided_at = :at, decision_reason = :reason"
                ),
                ConditionExpression="attribute_exists(request_id) AND #status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":pending": "PENDING",
                    ":rejected": "REJECTED",
                    ":by": decided_by,
                    ":at": decided_at,
                    ":reason": reason,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                self._raise_transition_failure(org_id, request_id)
            raise
        return PermissionRequest.from_item(response["Attributes"])
