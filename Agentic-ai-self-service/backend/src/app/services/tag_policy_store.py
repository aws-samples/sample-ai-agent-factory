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

import hashlib
import json
import logging
import os
from collections.abc import Iterable
from datetime import datetime, timezone

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
    default_value: str | None = None
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


class TagGovernanceStaleError(ValueError):
    """The caller's captured policy revision or profile timestamp is not the current one.

    P0-B: the UI resolves the effective tags in the browser against the policy set it
    loaded, and a deploy started minutes later would otherwise apply THOSE values under
    policies an admin has since changed (a new required tag, a changed default, a deleted
    policy, an edited profile). The values the caller saw and approved are no longer the
    values the org governs, so the deploy is refused and the caller re-reads. Distinct from
    ``TagResolutionError`` because the remedy is different: reload, do not supply a value.
    """


class ResolvedGovernance(BaseModel):
    """The effective tags plus the exact state they were resolved against.

    One read produces all three, which is the point: computing the revision from a second
    read would let a policy change slip between the check and the values that get applied.
    """

    tags: dict[str, str] = Field(default_factory=dict)
    policy_revision: str = ""
    profile_updated_at: str | None = None


def compute_policy_revision(policies: Iterable[TagPolicy]) -> str:
    """Hash the governed policy set into a stable ``sha256:<hex>`` revision.

    Byte-for-byte identical to the browser's ``computeTagPolicyRevision``
    (frontend/src/components/deploy/resourceTagState.ts): the same six fields in the same
    order, records sorted by key with a CODE-POINT comparison (not ``localeCompare``, which
    is locale- and ICU-dependent and orders mixed-case keys differently), compact JSON
    separators, and non-ASCII emitted raw -- ``json.dumps(..., separators=(",", ":"),
    ensure_ascii=False)`` is what ``JSON.stringify`` produces. ``created_at`` participates
    because deleting and recreating an otherwise identical policy is a distinct governance
    decision. Both sides pin the same digest for one adversarial fixture
    (``tests/test_tag_policy_revision_parity.py`` and ``resourceTagState.test.ts``), because
    a drift here is not a crash: the two runtimes simply never agree again, and every deploy
    carrying tags is refused as stale.
    """
    canonical = [
        {
            "key": policy.key,
            "default_value": policy.default_value,
            "required": policy.required,
            "show_on_card": policy.show_on_card,
            "created_at": policy.created_at or "",
            "updated_at": policy.updated_at,
        }
        for policy in sorted(policies, key=lambda p: p.key)
    ]
    payload = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def same_instant(a: str | None, b: str | None) -> bool:
    """True when two ISO-8601 timestamps name the same instant.

    The catalog spells its timestamps with ``+00:00`` (``_now``); a workflow document round-trips
    the captured profile timestamp through a pydantic ``datetime`` and comes back spelled with
    ``Z``. Measured live (2026-09-27): the same instant in the two spellings was refused as "the
    profile changed" by every governed export of a reloaded workflow. Unparseable values fall back
    to exact string equality so a malformed token is still a mismatch, never a pass.
    """
    if a == b:
        return True
    if not a or not b:
        return False
    try:
        return datetime.fromisoformat(a) == datetime.fromisoformat(b)
    except ValueError:
        return False


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

    def get_policy(self, org_id: str, key: str) -> TagPolicy | None:
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

    def get_profile(self, org_id: str, name: str) -> TagProfile | None:
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
        self,
        org_id: str,
        supplied: dict[str, str] | None = None,
        profile_name: str | None = None,
    ) -> dict[str, str]:
        """Resolve the final tag set to apply to deployed AWS resources.

        Precedence per policy: supplied value → profile value → default_value.
        Missing REQUIRED tag with no default → TagResolutionError (HTTP 400).
        Optional policies + ad-hoc supplied keys pass through when present.
        """
        return self.resolve_governance(org_id, supplied=supplied, profile_name=profile_name).tags

    def resolve_governance(
        self,
        org_id: str,
        supplied: dict[str, str] | None = None,
        profile_name: str | None = None,
        *,
        expected_policy_revision: str | None = None,
        expected_profile_updated_at: str | None = None,
    ) -> ResolvedGovernance:
        """Resolve the tags AND report the policy state they were resolved against.

        P0-B: the caller (browser or API client) may pass the ``policy_revision`` and the
        profile ``updated_at`` it resolved its own values against. They are checked against
        THIS read, before any AWS side effect, and a mismatch raises
        ``TagGovernanceStaleError``. Checking them here rather than in the route is what
        keeps the check and the applied values on one read of the table.
        """
        supplied = dict(supplied or {})
        policies = self.list_policies(org_id)
        revision = compute_policy_revision(policies)
        if expected_policy_revision and expected_policy_revision != revision:
            raise TagGovernanceStaleError(
                "The tag policies changed after this deployment's tags were resolved "
                f"(captured {expected_policy_revision}, current {revision}). Reload the "
                "deploy panel so the values you approve are the ones the org governs now; "
                "nothing was deployed."
            )

        profile_values: dict[str, str] = {}
        profile_updated_at: str | None = None
        if profile_name:
            profile = self.get_profile(org_id, profile_name)
            if profile is None:
                if expected_profile_updated_at:
                    # A profile the caller DID capture and that is now gone is stale
                    # governance, not a bad request: an admin deleted it after the panel
                    # loaded. Raising TagResolutionError here would answer 400 ("supply a
                    # value") for a state whose only remedy is to reload -- and would do it
                    # before any staleness check ran, so a deletion was the one profile
                    # change that could not be reported as such.
                    raise TagGovernanceStaleError(
                        f"Tag profile '{profile_name}' no longer exists; it was deleted after "
                        "this deployment's tags were resolved. Reload the deploy panel and "
                        "choose a profile that still exists; nothing was deployed."
                    )
                raise TagResolutionError(f"Unknown tag profile '{profile_name}'")
            profile_values = dict(profile.values)
            profile_updated_at = profile.updated_at
            if expected_profile_updated_at and not same_instant(expected_profile_updated_at, profile_updated_at):
                raise TagGovernanceStaleError(
                    f"Tag profile '{profile_name}' changed after this deployment's tags were "
                    f"resolved (captured {expected_profile_updated_at}, current "
                    f"{profile_updated_at}). Reload the deploy panel; nothing was deployed."
                )
        elif expected_profile_updated_at:
            raise TagGovernanceStaleError(
                "A tag profile timestamp was supplied without a profile name, so there is "
                "nothing to check it against. Reload the deploy panel; nothing was deployed."
            )

        resolved: dict[str, str] = {}
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
        return ResolvedGovernance(
            tags=resolved,
            policy_revision=revision,
            profile_updated_at=profile_updated_at,
        )

    # -- internals -------------------------------------------------------

    def _query_prefix(self, org_id: str, prefix: str) -> list[dict]:
        items: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("org_id").eq(org_id) & Key("sk").begins_with(prefix)}
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


_store: TagPolicyStore | None = None


def get_tag_policy_store() -> TagPolicyStore:
    global _store
    if _store is None:
        _store = TagPolicyStore(
            table_name=os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy"),
            region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
        )
    return _store
