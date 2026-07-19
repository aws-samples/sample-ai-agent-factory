"""Tests for 3rd-party IdP group-claim mapping (Loom-study 1.1).

A federated user's groups arrive under the IdP's own claim (OIDC_GROUPS_CLAIM),
not cognito:groups. extract_cognito_groups maps those to our internal g-*/t-*
vocabulary via OIDC_GROUP_MAP; unmapped external groups are dropped (fail-closed).
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from app.services.auth import _parse_group_claim, extract_cognito_groups  # noqa: E402


class _Req:
    """Minimal Request stand-in carrying an authorizer-claims scope."""

    def __init__(self, claims: dict):
        self.scope = {"aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}}


def test_parse_group_claim_shapes():
    assert _parse_group_claim(["a", "b"]) == ["a", "b"]
    assert _parse_group_claim('["x", "y"]') == ["x", "y"]
    assert _parse_group_claim("[p, q]") == ["p", "q"]
    assert _parse_group_claim("m n") == ["m", "n"]
    assert _parse_group_claim(None) == []


def test_cognito_groups_still_work_without_federation(monkeypatch):
    monkeypatch.delenv("OIDC_GROUPS_CLAIM", raising=False)
    req = _Req({"cognito:groups": ["g-admins-super", "t-admin"]})
    assert extract_cognito_groups(req) == ["g-admins-super", "t-admin"]


def test_federated_groups_are_mapped_to_internal(monkeypatch):
    monkeypatch.setenv("OIDC_GROUPS_CLAIM", "groups")
    monkeypatch.setenv("OIDC_GROUP_MAP", '{"FinanceAdmins":"g-admins-cost","Staff":"g-users-default"}')
    req = _Req({"groups": ["FinanceAdmins", "Staff", "UnknownTeam"]})
    out = extract_cognito_groups(req)
    assert "g-admins-cost" in out
    assert "g-users-default" in out
    # unmapped external group grants nothing (fail-closed)
    assert "UnknownTeam" not in out


def test_cognito_and_federated_merge_and_dedup(monkeypatch):
    monkeypatch.setenv("OIDC_GROUPS_CLAIM", "groups")
    monkeypatch.setenv("OIDC_GROUP_MAP", '{"Devs":"g-users-default"}')
    # Both a native cognito group AND a mapped federated one that resolves to the
    # same internal group → deduped.
    req = _Req({"cognito:groups": ["g-users-default"], "groups": ["Devs"]})
    assert extract_cognito_groups(req) == ["g-users-default"]
