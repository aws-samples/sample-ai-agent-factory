"""Tag policies + tag profiles store (Phase 2 — governance tagging).

Loom-inspired: enforce consistent resource tagging at deploy time so every AWS
resource the platform creates carries owner/application/cost-center tags. This
enables cost attribution (Phase 4) and ABAC filtering.

Two record kinds share one DynamoDB table (single-table design):

  * **TagPolicy** — a tag KEY the org governs. ``required`` policies must be
    satisfied on every deploy (user value or ``default_value``); ``show_on_card``
    surfaces the tag as a badge in the UI. Keys prefixed ``platform:`` (e.g.
    ``platform:application``) are platform-required and read-only in the UI.
  * **TagProfile** — a named bundle of tag VALUES that satisfies the required
    policies, so a user picks a profile at deploy instead of typing every tag.

Table layout (mirrors prompt_library_store):
  PK ``org_id``, SK ``POLICY#<key>`` | ``PROFILE#<name>``.
  Low-volume org-wide config — no GSI needed.

Resolution (resolve_tags) is the deploy-time contract: for each required
policy, take the user/profile value → else default_value → else raise (the
caller returns HTTP 400). Optional policies contribute only if supplied.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_ORG_ID = "default"
_POLICY_PREFIX = "POLICY#"
_PROFILE_PREFIX = "PROFILE#"

# Platform-required tag keys. Seeded on first access; read-only in the UI.
PLATFORM_REQUIRED_KEYS = ("platform:application", "platform:owner", "platform:group")


class TagPolicy(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    default_value: Optional[str] = None
    required: bool = False
    show_on_card: bool = False
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_platform(self) -> bool:
        # Designation is computed from the key, never stored (matches Loom).
        return self.key.startswith("platform:")


class TagProfile(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    values: dict[str, str] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class TagResolutionError(ValueError):
    """Raised when a required tag has no value and no default (→ HTTP 400)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TagPolicyStore:
    """CRUD for tag policies + profiles, plus deploy-time tag resolution."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    # -- policies --------------------------------------------------------

    def put_policy(self, org_id: str, policy: TagPolicy) -> TagPolicy:
        if not policy.created_at:
            policy.created_at = _now()
        policy.updated_at = _now()
        item = policy.model_dump()
        item["org_id"] = org_id
        item["sk"] = _POLICY_PREFIX + policy.key
        self._table.put_item(Item=item)
        return policy

    def get_policy(self, org_id: str, key: str) -> Optional[TagPolicy]:
        resp = self._table.get_item(Key={"org_id": org_id, "sk": _POLICY_PREFIX + key})
        item = resp.get("Item")
        return TagPolicy(**_strip_keys(item)) if item else None

    def delete_policy(self, org_id: str, key: str) -> bool:
        self._table.delete_item(Key={"org_id": org_id, "sk": _POLICY_PREFIX + key})
        return True

    def list_policies(self, org_id: str) -> list[TagPolicy]:
        items = self._query_prefix(org_id, _POLICY_PREFIX)
        return [TagPolicy(**_strip_keys(i)) for i in items]

    # -- profiles --------------------------------------------------------

    def put_profile(self, org_id: str, profile: TagProfile) -> TagProfile:
        if not profile.created_at:
            profile.created_at = _now()
        profile.updated_at = _now()
        item = profile.model_dump()
        item["org_id"] = org_id
        item["sk"] = _PROFILE_PREFIX + profile.name
        self._table.put_item(Item=item)
        return profile

    def get_profile(self, org_id: str, name: str) -> Optional[TagProfile]:
        resp = self._table.get_item(Key={"org_id": org_id, "sk": _PROFILE_PREFIX + name})
        item = resp.get("Item")
        return TagProfile(**_strip_keys(item)) if item else None

    def delete_profile(self, org_id: str, name: str) -> bool:
        self._table.delete_item(Key={"org_id": org_id, "sk": _PROFILE_PREFIX + name})
        return True

    def list_profiles(self, org_id: str) -> list[TagProfile]:
        items = self._query_prefix(org_id, _PROFILE_PREFIX)
        return [TagProfile(**_strip_keys(i)) for i in items]

    # -- seeding + resolution -------------------------------------------

    def ensure_platform_policies(self, org_id: str) -> None:
        """Idempotently seed the platform tag policies as RECOMMENDED (not required).

        Governance must be OPT-IN: seeding these as ``required=True`` would make
        EVERY deploy without the tag fail at HTTP 400 the moment anyone views the
        settings page — breaking normal agent deploys by default. So they seed as
        ``required=False`` (shown on cards, encouraged); an admin explicitly flips
        ``required`` via POST /api/settings/tags when the org wants enforcement.
        Mirrors the advisory-by-default posture of RBAC + deploy-targets.
        """
        existing = {p.key for p in self.list_policies(org_id)}
        for key in PLATFORM_REQUIRED_KEYS:
            if key not in existing:
                self.put_policy(
                    org_id,
                    TagPolicy(key=key, required=False, show_on_card=True),
                )

    def resolve_tags(
        self, org_id: str, supplied: Optional[dict[str, str]] = None,
        profile_name: Optional[str] = None,
    ) -> dict[str, str]:
        """Resolve the final tag set to apply to deployed AWS resources.

        Precedence per policy: supplied value → profile value → default_value.
        Missing REQUIRED tag with no default → TagResolutionError (HTTP 400).
        Optional policies + ad-hoc supplied keys pass through when present.
        """
        supplied = dict(supplied or {})
        profile_values: dict[str, str] = {}
        if profile_name:
            profile = self.get_profile(org_id, profile_name)
            if profile is None:
                raise TagResolutionError(f"Unknown tag profile '{profile_name}'")
            profile_values = dict(profile.values)

        resolved: dict[str, str] = {}
        policies = self.list_policies(org_id)
        policy_keys = {p.key for p in policies}

        for policy in policies:
            value = supplied.get(policy.key) or profile_values.get(policy.key) or policy.default_value
            if value:
                resolved[policy.key] = value
            elif policy.required:
                raise TagResolutionError(
                    f"Required tag '{policy.key}' has no value (supply it or a default_value / profile)"
                )

        # Ad-hoc custom tags the caller supplied that aren't governed policies.
        for k, v in {**profile_values, **supplied}.items():
            if k not in policy_keys and v:
                resolved[k] = v
        return resolved

    # -- internals -------------------------------------------------------

    def _query_prefix(self, org_id: str, prefix: str) -> list[dict]:
        items: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": Key("org_id").eq(org_id)
            & Key("sk").begins_with(prefix)
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return items


def _strip_keys(item: dict) -> dict:
    """Drop the DDB PK/SK before hydrating a pydantic model."""
    return {k: v for k, v in item.items() if k not in ("org_id", "sk")}


_store: Optional[TagPolicyStore] = None


def get_tag_policy_store() -> TagPolicyStore:
    global _store
    if _store is None:
        _store = TagPolicyStore(
            table_name=os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy"),
            region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
        )
    return _store
