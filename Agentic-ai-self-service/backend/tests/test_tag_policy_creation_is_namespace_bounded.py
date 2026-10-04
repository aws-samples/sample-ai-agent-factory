"""A tag policy an admin can create must be one a deploy can actually apply.

THE DEFECT, and it is a pair of them that cancelled the feature out.

P0-B lets an admin declare governance tag policies (``POST /api/settings/tags``) which every
subsequent deployment resolves and stamps on the AWS resources it creates. The step roles'
``bedrock-agentcore:TagResource`` grants bound ``aws:TagKeys`` to the governance namespaces
(``infra/stacks/platform/config.py::GOVERNANCE_TAG_KEY_PREFIXES``), because an IAM condition is
fixed at synth time and ARCC ``cnt_L4ZLZgjrCctfxl`` lists create/update tags among the operations
leveraged for privilege escalation -- dropping the bound was not available. So:

  1. the only namespace the bound named was ``platform:``, and
  2. ``upsert_policy`` REFUSES to create a new ``platform:`` key -- deliberately, so an
     admin-created key cannot read as one of the three product-seeded policies.

Every key an admin could create was therefore outside the namespace every deploy role could
stamp. The feature was fully plumbed -- store, resolution, staleness tokens, Step Functions
state, the merged tag set at ~40 call sites -- and could not tag anything. ``org:`` exists to
close that, and this file is what keeps the two halves from drifting apart again: a namespace an
admin cannot write to is not a governance namespace, and a key an admin can write that the deploy
path must refuse is a landmine planted by one person for another to trip over.

The validation deliberately runs at WRITE time and not only at deploy time. A tag policy outlives
the request that created it; the admin who typed the key is not the person who reads the
AccessDenied, and by then there are half-created resources to clean up.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import pytest
from app import deployment_handler as dh
from app.services import resource_tagging as rt
from app.services.tag_policy_store import (
    PLATFORM_REQUIRED_KEYS,
    TagPolicy,
    TagPolicyStore,
    TagProfile,
)
from fastapi.testclient import TestClient

ADMIN_SUB = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"


class _FakeStore(TagPolicyStore):
    """The real store with its DynamoDB reads and writes substituted.

    A subclass rather than a mock for the same reason the fail-closed suite gives: the route's
    behaviour on a missing policy (``get_policy`` -> ``None``) is part of what is under test, and
    a loose mock returning a truthy MagicMock would make the reservation check pass vacuously.
    """

    def __init__(self, existing: dict[str, TagPolicy] | None = None) -> None:  # noqa: D107
        self._policies = dict(existing or {})
        self.written: list[TagPolicy] = []
        self.written_profiles: list[TagProfile] = []

    def get_policy(self, _org_id, key):
        return self._policies.get(key)

    def list_policies(self, _org_id):
        return list(self._policies.values())

    def ensure_platform_policies(self, _org_id):
        return None

    def put_policy(self, _org_id, policy):
        self.written.append(policy)
        self._policies[policy.key] = policy
        return policy

    def put_profile(self, _org_id, profile):
        self.written_profiles.append(profile)
        return profile


@pytest.fixture
def store(monkeypatch):
    import app.routers.tags as tags_router
    import app.services.tag_policy_store as tps

    fake = _FakeStore()
    monkeypatch.setattr(tps, "get_tag_policy_store", lambda: fake)
    monkeypatch.setattr(tags_router, "get_tag_policy_store", lambda: fake)
    return fake


@pytest.fixture
def client():
    """An admin caller. ``g-admins-super`` is the group that carries ``tag:write``; without it
    every assertion below would be measuring a 403 from RBAC instead of the tag validation."""
    claims = {"cognito:groups": ["g-admins-super"], "sub": ADMIN_SUB}
    event = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}

    async def _inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await dh.deployment_app(scope, receive, send)

    return TestClient(_inject, raise_server_exceptions=False)


# ======================================================================================
# The admitting case. Without it every refusal below is satisfied by "refuse everything",
# which is what the code actually did: the one namespace IAM allowed was the one namespace
# this endpoint refused to create in.
# ======================================================================================


def test_an_admin_can_create_a_policy_in_a_namespace_the_deploy_roles_can_stamp(store, client):
    response = client.post("/api/settings/tags", json={"key": "org:cost-center", "default_value": "cc-000"})

    assert response.status_code == 200, response.text[:400]
    assert [p.key for p in store.written] == ["org:cost-center"]
    # The written key must survive the live path's own gate, or the policy is still a landmine.
    assert rt.stampable_governance_tags({"org:cost-center": "cc-000"}) == {"org:cost-center": "cc-000"}


def test_at_least_one_default_namespace_is_one_an_admin_can_create_in(store, client):
    """The invariant behind the test above, asserted against the namespace list itself.

    Narrowing ``GOVERNANCE_TAG_KEY_PREFIXES`` back to the reserved namespace alone would leave
    every admin-created key un-stampable while every test that only checks refusals still passed.
    This is the control that fails when that happens.
    """
    creatable = []
    for prefix in rt.governance_tag_key_prefixes():
        response = client.post("/api/settings/tags", json={"key": f"{prefix}probe-key"})
        if response.status_code == 200:
            creatable.append(prefix)
    assert creatable, (
        f"none of the governance namespaces {rt.governance_tag_key_prefixes()} accepts a "
        "newly created tag policy, so no admin-created tag can ever reach an AWS resource"
    )


# ======================================================================================
# The refusals, each one a key that WOULD have been accepted and then denied by IAM.
# ======================================================================================


@pytest.mark.parametrize(
    ("key", "because"),
    [
        ("CostCenter", "outside the tag namespaces"),
        ("Environment", "outside the tag namespaces"),  # the ABAC key cnt_6gBImtb08AJqCB warns about
        ("platform-owner", "outside the tag namespaces"),  # the namespace needs its colon
        ("aws:cost-center", "reserved 'aws:' prefix"),
        ("org:api_key", "designates credential material"),
    ],
)
def test_a_key_the_deploy_path_must_refuse_cannot_be_saved(store, client, key, because):
    response = client.post("/api/settings/tags", json={"key": key})

    assert response.status_code == 400, response.text[:400]
    assert because in response.text
    assert store.written == [], "the policy was persisted before its key was validated"


def test_a_platform_key_is_still_reserved_from_creation(store, client):
    """Unchanged behaviour, re-pinned here because ``org:`` exists only as its consequence.

    If this reservation is ever dropped, ``org:`` stops being load-bearing -- and the reverse is
    the dangerous direction: dropping ``org:`` while keeping this makes the feature inert again.
    """
    response = client.post("/api/settings/tags", json={"key": "platform:invented"})

    assert response.status_code == 400, response.text[:400]
    assert "reserved" in response.text
    assert store.written == []


def test_an_existing_platform_policy_can_still_be_updated(monkeypatch, client):
    """An admin MUST be able to flip ``required`` on a seeded platform policy -- that is how
    governance is turned on, since they ship ``required=False``. The namespace check must not
    break that path, so the seeded key is exercised rather than an invented one."""
    import app.routers.tags as tags_router
    import app.services.tag_policy_store as tps

    key = PLATFORM_REQUIRED_KEYS[0]
    fake = _FakeStore({key: TagPolicy(key=key, required=False)})
    monkeypatch.setattr(tps, "get_tag_policy_store", lambda: fake)
    monkeypatch.setattr(tags_router, "get_tag_policy_store", lambda: fake)

    response = client.post("/api/settings/tags", json={"key": key, "required": True})

    assert response.status_code == 200, response.text[:400]
    assert fake.written[0].required is True


def test_a_default_value_no_aws_service_would_accept_is_refused(store, client):
    """The default is what gets stamped when a deploy supplies nothing, so it is as much a live
    tag as the key. A NUL byte is outside AgentCore's TagsMap character class, which means
    CreateAgentRuntime rejects it -- mid-deploy, with resources already made. The value is never
    echoed back: a tag value can be pasted credential material."""
    response = client.post("/api/settings/tags", json={"key": "org:app", "default_value": "bad\x00value"})

    assert response.status_code == 400, response.text[:400]
    assert "character AgentCore" in response.text
    assert "badvalue" not in response.text and "bad\\u0000value" not in response.text
    assert store.written == []


# ======================================================================================
# Profiles are the second way in: resolve_governance merges a profile entry that matches no
# policy into the resolved set as an ad-hoc tag, so a profile can carry a key no policy declares.
# ======================================================================================


def test_a_profile_cannot_carry_an_out_of_namespace_key(store, client):
    response = client.post(
        "/api/settings/tag-profiles",
        json={"name": "regulated", "values": {"org:owner": "ops", "CostCenter": "cc-1"}},
    )

    assert response.status_code == 400, response.text[:400]
    assert "outside the tag namespaces" in response.text
    assert "'CostCenter'" in response.text, "the refusal must name which entry to fix"
    assert store.written_profiles == []


def test_a_profile_whose_keys_are_all_in_namespace_is_saved(store, client):
    response = client.post(
        "/api/settings/tag-profiles",
        json={"name": "regulated", "values": {"org:owner": "ops", "platform:application": "payments"}},
    )

    assert response.status_code == 200, response.text[:400]
    assert store.written_profiles[0].values == {"org:owner": "ops", "platform:application": "payments"}
