"""Unit tests for services/rbac.py — scope resolution + require_scopes.

Requests are faked by constructing a Starlette Request with an ``aws.event``
in scope carrying a ``cognito:groups`` claim — exactly the shape
services.auth.extract_cognito_groups reads. No real Cognito needed.
"""

from __future__ import annotations

import pytest
from app.services import rbac
from fastapi import HTTPException
from starlette.requests import Request


def _request(groups=None, *, in_lambda: bool = True) -> Request:
    """Build a Request with a cognito:groups claim.

    groups: list|str|None -> serialized into the claim exactly as the HTTP API
    JWT authorizer might. in_lambda=False omits aws.event (local-dev path).
    """
    scope = {"type": "http", "path": "/api/test", "headers": []}
    if in_lambda:
        claims = {}
        if groups is not None:
            claims["cognito:groups"] = groups
        scope["aws.event"] = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
    return Request(scope)


# --------------------------------------------------------------------------
# Scope resolution
# --------------------------------------------------------------------------


def test_local_dev_gets_all_scopes():
    req = _request(in_lambda=False)
    assert rbac.caller_scopes(req) == set(rbac.SCOPES)


def test_no_groups_fail_closed():
    req = _request(groups=None)
    assert rbac.caller_scopes(req) == set()


def test_super_admin_group_expands_to_all():
    req = _request(groups=["g-admins-super"])
    assert rbac.caller_scopes(req) == set(rbac.SCOPES)


def test_admin_super_scope_implies_everything():
    req = _request(groups=["org-admin"])  # legacy group → admin
    assert rbac.has_scopes(req, ("agent:write", "cost:read", "registry:write"))


def test_resource_group_grants_only_its_scopes():
    req = _request(groups=["g-admins-registry"])
    held = rbac.caller_scopes(req)
    assert "registry:read" in held and "registry:write" in held
    assert "agent:write" not in held
    assert "admin" not in held


def test_multiple_groups_union():
    req = _request(groups=["g-admins-registry", "g-admins-cost"])
    held = rbac.caller_scopes(req)
    assert {"registry:read", "registry:write", "cost:read", "cost:write"} <= held


def test_viewer_is_read_only():
    req = _request(groups=["viewer"])
    held = rbac.caller_scopes(req)
    assert "agent:read" in held and "invoke" in held
    assert "agent:write" not in held


@pytest.mark.parametrize(
    "raw",
    [
        ["g-admins-registry"],  # list
        '["g-admins-registry"]',  # JSON-array string
        "g-admins-registry",  # bare string
        "[g-admins-registry]",  # bracketed
    ],
)
def test_group_claim_serialization_shapes(raw):
    req = _request(groups=raw)
    assert "registry:write" in rbac.caller_scopes(req)


# --------------------------------------------------------------------------
# require_scopes dependency — advisory vs enforce
# --------------------------------------------------------------------------


def test_require_scopes_rejects_unknown_scope():
    with pytest.raises(ValueError):
        rbac.require_scopes("bogus:scope")


def test_enforce_denies_missing_scope(monkeypatch):
    monkeypatch.setenv("RBAC_ENFORCE", "true")
    dep = rbac.require_scopes("agent:write")
    req = _request(groups=["viewer"])  # read-only
    with pytest.raises(HTTPException) as exc:
        dep(req)
    assert exc.value.status_code == 403


def test_enforce_allows_held_scope(monkeypatch):
    monkeypatch.setenv("RBAC_ENFORCE", "true")
    dep = rbac.require_scopes("agent:read")
    req = _request(groups=["viewer"])
    assert dep(req) is None  # no raise


def test_advisory_allows_even_when_missing(monkeypatch):
    monkeypatch.setenv("RBAC_ENFORCE", "false")
    dep = rbac.require_scopes("agent:write")
    req = _request(groups=["viewer"])  # lacks agent:write
    assert dep(req) is None  # advisory: allowed


def test_advisory_is_default(monkeypatch):
    monkeypatch.delenv("RBAC_ENFORCE", raising=False)
    assert rbac.rbac_enforcing() is False


def test_enforce_local_dev_always_allowed(monkeypatch):
    monkeypatch.setenv("RBAC_ENFORCE", "true")
    dep = rbac.require_scopes("admin")
    req = _request(in_lambda=False)  # local dev → all scopes
    assert dep(req) is None
