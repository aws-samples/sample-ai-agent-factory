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

Versions. Every item carries a ``PolicyVersion``: an ``incarnation`` (a random id minted when the policy
is created and kept across replacements) and a ``revision`` counting the writes within it. A write names
the version it was based on (``expected``; ``None`` for "must not exist yet") as a DynamoDB condition on
BOTH attributes. Two admins editing the same policy therefore cannot silently overwrite each other: the
second save fails (``WorkspacePolicyRevisionConflict``) and has to be redone against the current
document, so a tightened approver routing or allow-list is never undone by a stale form. Because a
delete followed by a create mints a new incarnation, a token read before the deletion can never name
the recreated policy either (a plain counter would restart at 1 and could). Items written before
versions existed read as the legacy version ``0`` and can be claimed exactly once as such.

Only ``boto3`` DynamoDB Table calls are wrapped here; every other function is pure and testable
without AWS.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

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


class WorkspacePolicyRevisionConflict(WorkspacePolicyError):
    """A write named a version (or "none yet") that is not what the table holds now.

    ``expected`` is the version the writer based its document on (``None``: it meant to create);
    ``current`` is what is stored now (``None``: nothing is). Nothing was written.
    """

    def __init__(self, workspace_id: str, expected: PolicyVersion | None, current: PolicyVersion | None) -> None:
        self.workspace_id = workspace_id
        self.expected = expected
        self.current = current
        super().__init__(
            f"workspace policy {workspace_id!r}: expected version "
            f"{expected.token if expected else None!r}, found {current.token if current else None!r}"
        )

    @property
    def recreated(self) -> bool:
        """Both versions exist but belong to different incarnations: the policy the writer read was
        deleted and another was created under the same id since."""
        return (
            self.expected is not None
            and self.current is not None
            and self.expected.incarnation is not None
            and self.expected.incarnation != self.current.incarnation
        )


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


#: Item attributes holding the version. ``revision`` counts the writes within an ``incarnation``, a random
#: id minted when the policy is created and kept across replacements; a delete followed by a create mints
#: a new one, so no token ever names two different policies. Both are absent on items written before
#: versions existed.
REVISION_ATTRIBUTE = "revision"
INCARNATION_ATTRIBUTE = "incarnation"

_INCARNATION_RE = re.compile(r"^[0-9a-f]{16}$")
_TOKEN_RE = re.compile(r"^([0-9a-f]{16})\.([1-9][0-9]*)$")


class PolicyVersion(BaseModel):
    """The identity of one stored state of a policy, served to clients as the ``ETag`` token.

    ``incarnation`` is minted at creation and kept across replacements; ``revision`` counts the writes
    within it. A policy deleted and recreated gets a new incarnation, so a stale token from before the
    deletion cannot name the recreated policy (a plain counter would restart at 1 and could). The legacy
    token ``0`` names an item written before versions existed; it is claimed exactly once.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    incarnation: str | None
    revision: int

    @model_validator(mode="after")
    def _consistent(self) -> PolicyVersion:
        if self.incarnation is None:
            if self.revision != 0:
                raise ValueError("a version without an incarnation is the legacy version 0")
        elif not _INCARNATION_RE.match(self.incarnation) or self.revision < 1:
            raise ValueError("a version is a 16-hex-digit incarnation and a positive revision")
        return self

    @property
    def token(self) -> str:
        if self.incarnation is None:
            return "0"
        return f"{self.incarnation}.{self.revision}"

    @classmethod
    def parse(cls, token: str) -> PolicyVersion:
        """The version a token names; ``ValueError`` for anything this store never issues."""
        if token == "0":
            return LEGACY_VERSION
        match = _TOKEN_RE.match(token)
        if match is None:
            raise ValueError(f"not a workspace policy version token: {token!r}")
        return cls(incarnation=match.group(1), revision=int(match.group(2)))


#: The version of an item written before versions existed.
LEGACY_VERSION = PolicyVersion(incarnation=None, revision=0)


def new_incarnation() -> str:
    """A fresh incarnation id (64 random bits as hex): unpredictable, so a stale token cannot be forged
    into a current one by guessing, and never reused by a later create."""
    return secrets.token_hex(8)


def policy_to_item(org_id: str, policy: WorkspacePolicy, version: PolicyVersion) -> dict[str, Any]:
    """The DynamoDB item for a policy: the camelCase document plus the table keys and the version this
    write produces. Floats become ``Decimal`` because boto3's DynamoDB serializer refuses Python floats
    (a budget-bearing policy would otherwise fail to write)."""
    if not isinstance(version, PolicyVersion) or version.incarnation is None:
        raise WorkspacePolicyError(f"a stored version has an incarnation and a positive revision, not {version!r}")
    item: dict[str, Any] = {
        "org_id": org_id,
        "sk": policy_sort_key(policy.workspace_id),
        REVISION_ATTRIBUTE: version.revision,
        INCARNATION_ATTRIBUTE: version.incarnation,
        "policy": _floats_to_decimals(policy.model_dump(by_alias=True, mode="json")),
    }
    return item


def version_of(item: dict[str, Any]) -> PolicyVersion:
    """The version an item carries; the legacy version for an item written before versions existed.
    Anything inconsistent (a revision without an incarnation or the reverse, a malformed incarnation, a
    revision that is not a positive integer) is corruption and raises rather than being guessed at."""
    revision = item.get(REVISION_ATTRIBUTE)
    incarnation = item.get(INCARNATION_ATTRIBUTE)
    if revision is None and incarnation is None:
        return LEGACY_VERSION
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int | Decimal)
        or revision != int(revision)
        or revision < 1
    ):
        raise WorkspacePolicyError(f"stored workspace policy revision is not a positive integer: {revision!r}")
    if not isinstance(incarnation, str) or not _INCARNATION_RE.match(incarnation):
        raise WorkspacePolicyError("stored workspace policy incarnation is malformed")
    return PolicyVersion(incarnation=incarnation, revision=int(revision))


class StoredPolicy(BaseModel):
    """A policy as read from the table, with the version a replacement must name."""

    policy: WorkspacePolicy
    version: PolicyVersion


def policy_from_item(item: dict[str, Any]) -> WorkspacePolicy:
    """Rebuild a policy from its item. Strict, three ways: a corrupt or hand-edited item raises rather
    than yielding a policy that silently allows (or denies) more than the admin wrote; the document must
    be COMPLETE (every field the writer produces, at every level), because a truncated document would
    reconstruct unconstrained allow-lists and empty approval routing from defaults; and the document's
    ``workspaceId`` must be the one the sort key was built from (a policy filed under another key governs
    nothing)."""
    document = item.get("policy")
    if not isinstance(document, dict):
        raise WorkspacePolicyError("workspace policy item has no policy document")
    missing = _missing_fields(WorkspacePolicy, document)
    if missing:
        raise WorkspacePolicyError(f"stored workspace policy is incomplete; missing {sorted(missing)}")
    try:
        policy = WorkspacePolicy.model_validate(_decimals_to_numbers(document))
    except ValidationError as exc:
        raise WorkspacePolicyError(f"stored workspace policy is invalid: {exc.error_count()} error(s)") from exc
    keyed = workspace_id_from_sort_key(str(item.get("sk", "")))
    if keyed is not None and keyed != policy.workspace_id:
        raise WorkspacePolicyError(
            f"stored workspace policy for key {keyed!r} names workspace {policy.workspace_id!r}; refusing to use it"
        )
    return policy


def _missing_fields(model: type[BaseModel], document: Any, prefix: str = "") -> list[str]:
    """Dotted paths of every field of ``model`` (and its nested policy models) absent from ``document``.
    The writer always stores the complete camelCase document, so absence means truncation."""
    if not isinstance(document, dict):
        return [prefix.rstrip(".") or "<root>"]
    missing: list[str] = []
    for name, field in model.model_fields.items():
        alias = field.alias or name
        if alias not in document:
            missing.append(f"{prefix}{alias}")
            continue
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            missing.extend(_missing_fields(annotation, document[alias], f"{prefix}{alias}."))
    return missing


def _floats_to_decimals(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _floats_to_decimals(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_floats_to_decimals(item) for item in value]
    return value


def _decimals_to_numbers(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _decimals_to_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decimals_to_numbers(item) for item in value]
    return value


class PolicyFence(BaseModel):
    """The exact stored policy item a judgement was made against, so a later write can be conditioned
    on it. ``document`` is the item's ``policy`` attribute exactly as read (``Decimal`` numbers included),
    so a DynamoDB equality condition holds if and only if the policy has not changed since the read."""

    table_name: str
    key: dict[str, str]
    document: dict[str, Any]

    def condition_check(self) -> dict[str, Any]:
        """A ``TransactWriteItems`` ``ConditionCheck`` action that cancels the transaction unless the
        policy is still exactly this document (a deleted policy fails it too)."""
        return {
            "ConditionCheck": {
                "TableName": self.table_name,
                "Key": dict(self.key),
                "ConditionExpression": "#policy = :policy",
                "ExpressionAttributeNames": {"#policy": "policy"},
                "ExpressionAttributeValues": {":policy": self.document},
            }
        }


class WorkspacePolicyStore:
    """CRUD over workspace policy items in the shared org-config table."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    # -- boto3 wrappers (the only AWS calls in this module) -------------------------------------

    def _put_item(self, item: dict[str, Any], condition: dict[str, Any]) -> None:
        """``condition`` is the ``ConditionExpression`` (plus its attribute names/values) every policy
        write carries; there is no unconditional write."""
        self._table.put_item(Item=item, **condition)

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
            # Strongly consistent: admission resolves the governing policy from this list, and a
            # restrictive policy an admin wrote a moment ago must not be missed in favour of the default.
            "ConsistentRead": True,
        }
        while True:
            response = self._table.query(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs["ExclusiveStartKey"] = last

    # -- public API ------------------------------------------------------------------------------

    def put(self, org_id: str, policy: WorkspacePolicy, *, expected: PolicyVersion | None) -> StoredPolicy:
        """Create (``expected=None``: the item must not exist) or replace (the item must be at exactly
        ``expected``; the legacy version names an item written before versions existed) a policy, as one
        conditional write on both version attributes. Any other state of the table is
        ``WorkspacePolicyRevisionConflict`` and nothing is written. A create, and the first claim of a
        legacy item, mints a new incarnation, so a token read before a delete can never match what a
        later create stores. Returns the policy with the version this write produced."""
        if expected is not None and not isinstance(expected, PolicyVersion):
            raise WorkspacePolicyError(f"expected must be None or a PolicyVersion, not {type(expected).__name__}")
        names = {"#rev": REVISION_ATTRIBUTE, "#inc": INCARNATION_ATTRIBUTE}
        if expected is None:
            condition: dict[str, Any] = {"ConditionExpression": "attribute_not_exists(sk)"}
            version = PolicyVersion(incarnation=new_incarnation(), revision=1)
        elif expected.incarnation is None:
            condition = {
                "ConditionExpression": "attribute_exists(sk) AND attribute_not_exists(#rev) AND attribute_not_exists(#inc)",
                "ExpressionAttributeNames": names,
            }
            version = PolicyVersion(incarnation=new_incarnation(), revision=1)
        else:
            condition = {
                "ConditionExpression": "#inc = :inc AND #rev = :rev",
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": {":inc": expected.incarnation, ":rev": expected.revision},
            }
            version = PolicyVersion(incarnation=expected.incarnation, revision=expected.revision + 1)
        try:
            self._put_item(policy_to_item(org_id, policy, version), condition)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            current = self._read_version(org_id, policy.workspace_id)
            raise WorkspacePolicyRevisionConflict(policy.workspace_id, expected, current) from exc
        return StoredPolicy(policy=policy, version=version)

    def _read_version(self, org_id: str, workspace_id: str) -> PolicyVersion | None:
        item = self._get_item({"org_id": org_id, "sk": policy_sort_key(workspace_id)})
        return version_of(item) if item else None

    def read(self, org_id: str, workspace_id: str) -> StoredPolicy | None:
        """The policy and the version a replacement must name (strongly consistent)."""
        item = self._get_item({"org_id": org_id, "sk": policy_sort_key(workspace_id)})
        if not item:
            return None
        return StoredPolicy(policy=policy_from_item(item), version=version_of(item))

    def get(self, org_id: str, workspace_id: str) -> WorkspacePolicy | None:
        record = self.read(org_id, workspace_id)
        return record.policy if record else None

    def get_snapshot(self, org_id: str, workspace_id: str) -> tuple[WorkspacePolicy, PolicyFence] | None:
        """The policy AND a fence on the exact item it was read from, for a write that must be
        conditioned on the policy not having changed since the judgement (strongly consistent read)."""
        key = {"org_id": org_id, "sk": policy_sort_key(workspace_id)}
        item = self._get_item(key)
        if not item:
            return None
        policy = policy_from_item(item)
        return policy, PolicyFence(table_name=self._table_name, key=key, document=item["policy"])

    def delete(self, org_id: str, workspace_id: str) -> None:
        self._delete_item({"org_id": org_id, "sk": policy_sort_key(workspace_id)})

    def list(self, org_id: str) -> list[WorkspacePolicy]:
        policies = []
        for item in self._query_prefix(org_id, _SK_PREFIX):
            if workspace_id_from_sort_key(str(item.get("sk", ""))) is None:
                continue
            policies.append(policy_from_item(item))
        return sorted(policies, key=lambda policy: policy.workspace_id)


class WorkspaceMembershipError(WorkspacePolicyError):
    """The agent names a workspace its deployer is not a member of."""

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        super().__init__(f"the deployer is not a member of workspace {workspace_id!r}")


class WorkspaceSelectionRequiredError(WorkspacePolicyError):
    """The deployer belongs to several workspaces and the agent names none of them."""

    def __init__(self, choices: list[str]) -> None:
        self.choices = list(choices)
        super().__init__(f"the agent must name one of the deployer's workspaces: {', '.join(choices)}")


def is_member(policy: WorkspacePolicy, caller_groups: list[str] | tuple[str, ...] | set[str]) -> bool:
    """Whether a caller holding ``caller_groups`` (the JWT's ``cognito:groups``) belongs to the
    workspace. The ``default`` workspace has no membership: it governs everyone else."""
    if policy.workspace_id == DEFAULT_WORKSPACE_ID:
        return True
    return bool(set(policy.member_groups) & set(caller_groups))


def resolve_policy(
    store: WorkspacePolicyStore,
    org_id: str,
    workspace_id: str | None,
    caller_groups: list[str] | tuple[str, ...] | set[str] = (),
) -> tuple[WorkspacePolicy, bool]:
    """The policy that governs an agent deployed by a caller holding ``caller_groups``, and whether it
    is the org ``default``.

    The governing workspace is resolved from the CALLER'S MEMBERSHIPS (``member_groups`` against the
    IdP-asserted ``cognito:groups``), not from what the agent's spec says, because the spec is the
    deployer's own document:

    * the caller is a member of no configured workspace -> the org ``default`` policy governs, whatever
      the spec names (an unconfigured name is a canvas saved before its workspace existed);
    * the caller is a member of exactly one -> that workspace governs, named or not, so a member cannot
      escape their workspace by omitting ``flowId`` or clearing ``workspaceId``;
    * the caller is a member of several -> the spec must name one of them
      (``WorkspaceSelectionRequiredError`` otherwise);
    * a spec that names a configured workspace the caller is not a member of is
      ``WorkspaceMembershipError`` in every case. Neither error falls back to the default: the refusal
      is visible, never a re-route.

    A missing ``default`` policy (when it is what governs) or an unreadable store is
    ``WorkspacePolicyUnavailableError`` so the caller fails closed. An unparseable stored policy is also
    unavailable: never admit on a policy we could not read.
    """
    try:
        policies = store.list(org_id)
    except WorkspacePolicyError as exc:
        logger.error("A stored workspace policy is unreadable (%s)", exc)
        raise WorkspacePolicyUnavailableError("stored workspace policy is invalid") from exc
    except Exception as exc:  # noqa: BLE001 - any store failure is "unavailable"; type only in logs
        logger.error("Workspace policies could not be read (%s)", type(exc).__name__)
        raise WorkspacePolicyUnavailableError("workspace policy store unavailable") from exc

    by_id = {policy.workspace_id: policy for policy in policies}
    default = by_id.get(DEFAULT_WORKSPACE_ID)
    memberships = [
        policy
        for policy in policies
        if policy.workspace_id != DEFAULT_WORKSPACE_ID and is_member(policy, caller_groups)
    ]
    named = by_id.get(workspace_id) if workspace_id and workspace_id != DEFAULT_WORKSPACE_ID else None

    if named is not None:
        if named not in memberships:
            raise WorkspaceMembershipError(named.workspace_id)
        return named, False
    if len(memberships) == 1:
        return memberships[0], False
    if len(memberships) > 1:
        raise WorkspaceSelectionRequiredError(sorted(policy.workspace_id for policy in memberships))
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
