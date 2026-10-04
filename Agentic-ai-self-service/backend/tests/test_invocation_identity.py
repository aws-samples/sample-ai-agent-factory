"""Tenant isolation for the default AgentCore Memory invocation identity."""

from __future__ import annotations

import re
import uuid

import pytest
from app.services.invocation_identity import (
    InvocationIdentityError,
    resolve_invocation_identity,
)
from app.services.resource_ownership import owner_sub_hash

OWNER = "7478e488-7081-7081-aaaa-bbbbbbbbbbbb"
OTHER = "8489f599-8192-8192-bbbb-cccccccccccc"
TOKEN = uuid.UUID("11111111-2222-4333-8444-555555555555")


def _resolve(
    session_id: str | None = None,
    *,
    owner: str | None = OWNER,
    caller: str | None = None,
    token: uuid.UUID = TOKEN,
):
    return resolve_invocation_identity(
        session_id,
        deployment_owner_sub=owner,
        authenticated_caller_sub=caller,
        token_factory=lambda: token,
    )


def test_an_omitted_session_is_valid_for_runtime_routing_and_memory():
    identity = _resolve()

    assert identity.generated_session is True
    assert 33 <= len(identity.session_id) <= 100
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", identity.session_id)


def test_the_actor_is_tenant_bound_without_exposing_the_raw_subject():
    identity = _resolve()

    assert identity.actor_id == owner_sub_hash(OWNER)
    assert len(identity.actor_id) == 32
    assert OWNER not in identity.actor_id
    assert OWNER not in identity.session_id


def test_new_session_mints_a_new_memory_stream_for_the_same_owner():
    first = _resolve(token=uuid.UUID("11111111-2222-4333-8444-555555555555"))
    second = _resolve(token=uuid.UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"))

    assert first.actor_id == second.actor_id
    assert first.session_id != second.session_id


def test_even_a_repeated_random_token_cannot_merge_two_tenants():
    first = _resolve(owner=OWNER)
    second = _resolve(owner=OTHER)

    assert first.actor_id != second.actor_id
    assert first.session_id != second.session_id


def test_an_explicit_session_remains_exact_while_the_actor_stays_bound():
    explicit = "client-session_" + ("x" * 40)
    identity = _resolve(explicit)

    assert identity.session_id == explicit
    assert identity.actor_id == owner_sub_hash(OWNER)
    assert identity.generated_session is False


@pytest.mark.parametrize("length", [33, 100])
def test_explicit_session_accepts_the_exact_service_boundaries(length):
    explicit = "s" * length

    identity = _resolve(explicit)

    assert identity.session_id == explicit
    assert identity.generated_session is False


@pytest.mark.parametrize("length", [1, 32, 101, 256])
def test_explicit_session_outside_the_runtime_memory_intersection_is_refused(length):
    explicit = "s" * length

    with pytest.raises(InvocationIdentityError, match="33 to 100"):
        _resolve(explicit)


@pytest.mark.parametrize(
    "explicit",
    [
        "-starts-with-a-hyphen" + ("x" * 20),
        "_starts_with_underscore" + ("x" * 20),
        "contains.period" + ("x" * 20),
        "contains space" + ("x" * 20),
        "contains/slash" + ("x" * 20),
    ],
)
def test_explicit_session_outside_memorys_character_contract_is_refused(explicit):
    assert 33 <= len(explicit) <= 100

    with pytest.raises(InvocationIdentityError, match="start with a letter or digit"):
        _resolve(explicit)


def test_explicit_session_is_never_silently_padded_or_truncated():
    too_short = "client-session"

    with pytest.raises(InvocationIdentityError, match="33 to 100"):
        _resolve(too_short)


def test_a_non_string_explicit_session_is_refused_cleanly():
    with pytest.raises(InvocationIdentityError, match="must be a string"):
        _resolve(42)  # type: ignore[arg-type]


def test_an_empty_session_is_treated_as_a_request_for_a_new_session():
    identity = _resolve("")

    assert identity.generated_session is True
    assert len(identity.session_id) == 65


def test_the_stored_deployment_owner_wins_over_an_iam_function_url_caller():
    identity = _resolve(caller="iam:AROATEST:operator")

    assert identity.actor_id == owner_sub_hash(OWNER)
    assert identity.actor_id != owner_sub_hash("iam:AROATEST:operator")


def test_a_pre_tenancy_record_can_fall_back_to_the_authenticated_caller():
    identity = _resolve(owner=None, caller=OTHER)

    assert identity.actor_id == owner_sub_hash(OTHER)


def test_an_ownerless_unauthenticated_invocation_fails_closed():
    with pytest.raises(InvocationIdentityError, match="owner or authenticated caller"):
        _resolve(owner=None, caller=None)


def test_a_whitespace_only_owner_fails_closed():
    with pytest.raises(InvocationIdentityError, match="owner or authenticated caller"):
        _resolve(owner="   ", caller=None)
