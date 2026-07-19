"""Tests for tag policy/profile store + deploy-time tag resolution (Phase 2).

moto-backed DDB. Focus on resolve_tags (the deploy contract): required-tag
enforcement, precedence (supplied > profile > default), and platform seeding.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest

moto = pytest.importorskip("moto")
from app.services import tag_policy_store as tps_mod  # noqa: E402
from app.services.tag_policy_store import (  # noqa: E402
    DEFAULT_ORG_ID,
    TagPolicy,
    TagPolicyStore,
    TagProfile,
    TagResolutionError,
)
from moto import mock_aws  # noqa: E402

TABLE = "TagPolicy"
ORG = DEFAULT_ORG_ID


def _create_table() -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "org_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def store() -> Iterator[TagPolicyStore]:
    with mock_aws():
        _create_table()
        s = TagPolicyStore(table_name=TABLE, region="us-east-1")
        tps_mod._store = s
        yield s


# -- policies + profiles CRUD -----------------------------------------------


def test_put_and_list_policy(store: TagPolicyStore):
    store.put_policy(ORG, TagPolicy(key="team", default_value="core", show_on_card=True))
    policies = store.list_policies(ORG)
    assert len(policies) == 1
    assert policies[0].key == "team"
    assert policies[0].default_value == "core"
    assert policies[0].show_on_card is True


def test_platform_policy_is_computed_not_stored(store: TagPolicyStore):
    store.put_policy(ORG, TagPolicy(key="platform:owner", required=True))
    p = store.get_policy(ORG, "platform:owner")
    assert p.is_platform is True
    store.put_policy(ORG, TagPolicy(key="cost-center"))
    assert store.get_policy(ORG, "cost-center").is_platform is False


def test_profile_crud(store: TagPolicyStore):
    store.put_profile(ORG, TagProfile(name="prod", values={"team": "core", "env": "prod"}))
    profiles = store.list_profiles(ORG)
    assert len(profiles) == 1 and profiles[0].name == "prod"
    assert store.delete_profile(ORG, "prod")
    assert store.list_profiles(ORG) == []


def test_ensure_platform_policies_idempotent(store: TagPolicyStore):
    store.ensure_platform_policies(ORG)
    store.ensure_platform_policies(ORG)  # second call must not duplicate
    keys = {p.key for p in store.list_policies(ORG)}
    assert {"platform:application", "platform:owner", "platform:group"} <= keys
    assert len(store.list_policies(ORG)) == 3


# -- resolve_tags: the deploy-time contract ---------------------------------


def test_resolve_uses_default_for_required(store: TagPolicyStore):
    store.put_policy(ORG, TagPolicy(key="team", required=True, default_value="core"))
    resolved = store.resolve_tags(ORG)
    assert resolved == {"team": "core"}


def test_resolve_missing_required_raises(store: TagPolicyStore):
    store.put_policy(ORG, TagPolicy(key="team", required=True))  # no default
    with pytest.raises(TagResolutionError):
        store.resolve_tags(ORG)


def test_resolve_supplied_beats_default(store: TagPolicyStore):
    store.put_policy(ORG, TagPolicy(key="team", required=True, default_value="core"))
    resolved = store.resolve_tags(ORG, supplied={"team": "payments"})
    assert resolved["team"] == "payments"


def test_resolve_profile_values(store: TagPolicyStore):
    store.put_policy(ORG, TagPolicy(key="env", required=True))
    store.put_profile(ORG, TagProfile(name="prod", values={"env": "production"}))
    resolved = store.resolve_tags(ORG, profile_name="prod")
    assert resolved["env"] == "production"


def test_resolve_supplied_beats_profile(store: TagPolicyStore):
    store.put_policy(ORG, TagPolicy(key="env", required=True))
    store.put_profile(ORG, TagProfile(name="prod", values={"env": "production"}))
    resolved = store.resolve_tags(ORG, supplied={"env": "staging"}, profile_name="prod")
    assert resolved["env"] == "staging"


def test_resolve_unknown_profile_raises(store: TagPolicyStore):
    with pytest.raises(TagResolutionError):
        store.resolve_tags(ORG, profile_name="nope")


def test_resolve_optional_only_when_supplied(store: TagPolicyStore):
    store.put_policy(ORG, TagPolicy(key="team", required=False))
    assert store.resolve_tags(ORG) == {}
    assert store.resolve_tags(ORG, supplied={"team": "x"}) == {"team": "x"}


def test_resolve_adhoc_custom_tag_passes_through(store: TagPolicyStore):
    resolved = store.resolve_tags(ORG, supplied={"adhoc": "v"})
    assert resolved == {"adhoc": "v"}
