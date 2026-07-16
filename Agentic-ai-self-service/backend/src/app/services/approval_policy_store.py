"""Config-driven HITL approval policies (Loom-study 2.2).

Turns HITL from all-or-nothing (today: a `human_approval` tool the LLM MAY call)
into governed, per-tool policy: which tools require approval (glob match), whether
to block ("require") or just record ("notify"), and an optional timeout.

Stored in the shared org-config table (reused from tag_policy: PK ``org_id``, SK
``APPROVAL#<name>``) — no new table. The deploy hook serializes matching policies
into the ``LOOM_APPROVAL_POLICIES`` env var, which the generated agent's
BeforeToolInvocation hook (services/code_generator) reads to gate tools.
"""

from __future__ import annotations

import json
from typing import Optional

import boto3
from pydantic import BaseModel, Field

_APPROVAL_PREFIX = "APPROVAL#"


class ApprovalPolicy(BaseModel):
    """A HITL approval policy.

    ``tool_match``: glob patterns matched against tool names (fnmatch), e.g.
    ["delete_*", "*___send_email"]. ``mode``: "require" (block until approved) or
    "notify" (record + allow). ``timeout_seconds``: advisory pending lifetime.
    ``enabled``: toggle without deleting.
    """

    name: str = Field(min_length=1, max_length=128)
    tool_match: list[str] = Field(default_factory=list, max_length=50)
    mode: str = Field(default="require")  # require | notify
    timeout_seconds: int = Field(default=3600, ge=0, le=86400)
    enabled: bool = True


class ApprovalPolicyStore:
    def __init__(self, table_name: str, region: str) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put(self, org_id: str, policy: ApprovalPolicy) -> ApprovalPolicy:
        item = policy.model_dump()
        item["org_id"] = org_id
        item["sk"] = _APPROVAL_PREFIX + policy.name
        self._table.put_item(Item=item)
        return policy

    def get(self, org_id: str, name: str) -> Optional[ApprovalPolicy]:
        resp = self._table.get_item(Key={"org_id": org_id, "sk": _APPROVAL_PREFIX + name})
        item = resp.get("Item")
        return _to_policy(item) if item else None

    def delete(self, org_id: str, name: str) -> bool:
        self._table.delete_item(Key={"org_id": org_id, "sk": _APPROVAL_PREFIX + name})
        return True

    def list(self, org_id: str) -> list[ApprovalPolicy]:
        resp = self._table.query(
            KeyConditionExpression="org_id = :o AND begins_with(sk, :p)",
            ExpressionAttributeValues={":o": org_id, ":p": _APPROVAL_PREFIX},
        )
        return [_to_policy(i) for i in resp.get("Items", [])]


def _to_policy(item: dict) -> ApprovalPolicy:
    return ApprovalPolicy(
        name=item.get("name", (item.get("sk", "") or "").replace(_APPROVAL_PREFIX, "")),
        tool_match=list(item.get("tool_match", [])),
        mode=item.get("mode", "require"),
        timeout_seconds=int(item.get("timeout_seconds", 3600)),
        enabled=bool(item.get("enabled", True)),
    )


def serialize_for_agent(policies: list[ApprovalPolicy]) -> str:
    """Compact JSON of ENABLED policies for the LOOM_APPROVAL_POLICIES env var.

    Only the fields the in-agent hook needs (name, tool_match, mode). Empty
    string when there are no enabled policies (hook then no-ops).
    """
    payload = [
        {"name": p.name, "tool_match": p.tool_match, "mode": p.mode}
        for p in policies if p.enabled and p.tool_match
    ]
    return json.dumps(payload) if payload else ""
