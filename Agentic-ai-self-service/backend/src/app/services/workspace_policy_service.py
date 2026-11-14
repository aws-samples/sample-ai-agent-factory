"""Workspace policy store and resolution (plan §3.B, implementation step 1).

A ``WorkspacePolicy`` (``schemas/workspace_policy.py``) says what a workspace may use and which
approver groups each risk tier routes to. This module persists those policies and answers "which
policy governs this agent" for the admission hook (``services/workspace_admission.py``).

Storage. Policies live in the shared org-config table that ``tag_policy_store`` and
``approval_policy_store`` already use (PK ``org_id``, SK ``WORKSPACE#<id>#POLICY``) -- no new
table, no new IAM grant: the deployment Lambda already reads and writes that table for tag
governance. The ``WORKSPACE#`` prefix and ``#POLICY`` suffix keep the key space open for later
per-workspace items (overrides, usage) without a second scan.

Resolution. Every agent is governed by exactly one policy: the policy of the workspace named on
its spec (``workspaceId``), or the org's ``default`` workspace policy when the spec names none or
names a workspace with no stored policy. With the admission flag on there is therefore no
unguarded agent; when the ``default`` policy itself is absent, resolution raises
``WorkspacePolicyUnavailableError`` and admission fails closed (503), the same posture the tag
governance read takes. The default workspace is never auto-created: an admin writes it, so an
environment that has not been configured refuses rather than guesses.

Only ``boto3`` DynamoDB Table calls are wrapped here; every other function is pure and testable
without AWS.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import boto3
from pydantic import ValidationError

from app.schemas.workspace_policy import WorkspacePolicy

logger = logging.getLogger(__name__)

#: Workspace id every agent falls back to when its spec names no (configured) workspace.
DEFAULT_WORKSPACE_ID = "default"

#: Org partition used by the whole org-config table today (tag + approval policies).
DEFAULT_ORG_ID = "default"

_SK_PREFIX = "WORKSPACE#"
_SK_SUFFIX = "#POLICY"

#: Same id grammar as spec ids: the id is embedded in a sort key, so it must never contain ``#``.
_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class WorkspacePolicyError(ValueError):
    """Base class for workspace-policy failures."""


class InvalidWorkspaceIdError(WorkspacePolicyError):
    """The workspace id does not match the id grammar (so it could not form a sort key)."""


class WorkspacePolicyUnavailableError(WorkspacePolicyError):
    """No policy can be resolved for an agent: the store failed or the default policy is absent."""


def validate_workspace_id(workspace_id: str) -> str:
    """Return ``workspace_id`` when it matches the id grammar, else raise ``InvalidWorkspaceIdError``."""
    if not isinstance(workspace_id, str) or not _WORKSPACE_ID_RE.match(workspace_id):
        raise InvalidWorkspaceIdError("workspace id must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    return workspace_id


def policy_sort_key(workspace_id: str) -> str:
    """The DynamoDB sort key of a workspace's policy item."""
    return f"{_SK_PREFIX}{validate_workspace_id(workspace_id)}{_SK_SUFFIX}"


def workspace_id_from_sort_key(sort_key: str) -> str | None:
    """Inverse of ``policy_sort_key``; ``None`` for a key that is not a workspace policy item."""
    if not sort_key.startswith(_SK_PREFIX) or not sort_key.endswith(_SK_SUFFIX):
        return None
    middle = sort_key[len(_SK_PREFIX) : len(sort_key) - len(_SK_SUFFIX)]
    return middle if _WORKSPACE_ID_RE.match(middle) else None


def policy_to_item(org_id: str, policy: WorkspacePolicy) -> dict[str, Any]:
    """The DynamoDB item for a policy: the camelCase document plus the table keys."""
    item: dict[str, Any] = {
        "org_id": org_id,
        "sk": policy_sort_key(policy.workspace_id),
        "policy": policy.model_dump(by_alias=True, mode="json"),
    }
    return item


def policy_from_item(item: dict[str, Any]) -> WorkspacePolicy:
    """Rebuild a policy from its item. Strict: a corrupt or hand-edited item raises rather than
    yielding a policy that silently allows (or denies) more than the admin wrote."""
    document = item.get("policy")
    if not isinstance(document, dict):
        raise WorkspacePolicyError("workspace policy item has no policy document")
    try:
        return WorkspacePolicy.model_validate(document)
    except ValidationError as exc:
        raise WorkspacePolicyError(f"stored workspace policy is invalid: {exc.error_count()} error(s)") from exc


class WorkspacePolicyStore:
    """CRUD over workspace policy items in the shared org-config table."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    # -- boto3 wrappers (the only AWS calls in this module) -------------------------------------

    def _put_item(self, item: dict[str, Any]) -> None:
        self._table.put_item(Item=item)

    def _get_item(self, key: dict[str, str]) -> dict[str, Any] | None:
        response = self._table.get_item(Key=key, ConsistentRead=True)
        return response.get("Item")

    def _delete_item(self, key: dict[str, str]) -> None:
        self._table.delete_item(Key=key)

    def _query_prefix(self, org_id: str, prefix: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "org_id = :o AND begins_with(sk, :p)",
            "ExpressionAttributeValues": {":o": org_id, ":p": prefix},
        }
        while True:
            response = self._table.query(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs["ExclusiveStartKey"] = last

    # -- public API ------------------------------------------------------------------------------

    def put(self, org_id: str, policy: WorkspacePolicy) -> WorkspacePolicy:
        self._put_item(policy_to_item(org_id, policy))
        return policy

    def get(self, org_id: str, workspace_id: str) -> WorkspacePolicy | None:
        item = self._get_item({"org_id": org_id, "sk": policy_sort_key(workspace_id)})
        return policy_from_item(item) if item else None

    def delete(self, org_id: str, workspace_id: str) -> None:
        self._delete_item({"org_id": org_id, "sk": policy_sort_key(workspace_id)})

    def list(self, org_id: str) -> list[WorkspacePolicy]:
        policies = []
        for item in self._query_prefix(org_id, _SK_PREFIX):
            if workspace_id_from_sort_key(str(item.get("sk", ""))) is None:
                continue
            policies.append(policy_from_item(item))
        return sorted(policies, key=lambda policy: policy.workspace_id)


def resolve_policy(store: WorkspacePolicyStore, org_id: str, workspace_id: str | None) -> tuple[WorkspacePolicy, bool]:
    """The policy that governs an agent in ``workspace_id`` and whether it fell back to ``default``.

    Lookup order: the named workspace's policy, then the ``default`` workspace's policy. A named
    workspace with no policy falls back (an admin has not configured it yet, so the org default
    governs); a missing default is ``WorkspacePolicyUnavailableError`` so the caller fails closed.
    An unparseable stored policy is also unavailable: never admit on a policy we could not read.
    """
    try:
        if workspace_id and workspace_id != DEFAULT_WORKSPACE_ID:
            named = store.get(org_id, workspace_id)
            if named is not None:
                return named, False
        default = store.get(org_id, DEFAULT_WORKSPACE_ID)
    except WorkspacePolicyUnavailableError:
        raise
    except WorkspacePolicyError as exc:
        # A stored policy that no longer parses: the admin's intent is unknown, so it is unavailable.
        logger.error("Workspace policy for %r is unreadable (%s)", workspace_id or DEFAULT_WORKSPACE_ID, exc)
        raise WorkspacePolicyUnavailableError("stored workspace policy is invalid") from exc
    except Exception as exc:  # noqa: BLE001 - any store failure is "unavailable"; type only in logs
        logger.error("Workspace policy could not be read (%s)", type(exc).__name__)
        raise WorkspacePolicyUnavailableError("workspace policy store unavailable") from exc
    if default is None:
        raise WorkspacePolicyUnavailableError(
            f"no policy is stored for workspace {workspace_id or DEFAULT_WORKSPACE_ID!r} "
            f"and the {DEFAULT_WORKSPACE_ID!r} workspace has no policy either"
        )
    return default, True


_store: WorkspacePolicyStore | None = None


def get_workspace_policy_store() -> WorkspacePolicyStore:
    """Process-wide store on the org-config table, resolved the way the tag policy store is."""
    global _store
    if _store is None:
        _store = WorkspacePolicyStore(
            table_name=os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy"),
            region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
        )
    return _store


def reset_workspace_policy_store() -> None:
    """Drop the cached store (tests and table re-targeting)."""
    global _store
    _store = None
