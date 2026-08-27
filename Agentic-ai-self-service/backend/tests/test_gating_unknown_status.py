"""A failed registry query must surface as 503 (unknown), never 403 (denied).

The distinction is operational, not cosmetic. Integration gating is fail-closed,
and `list_records()` used to return [] on AccessDenied — so a single wrong
`agent-registry:` IAM action name would block every deploy that wired an external
MCP/A2A integration, while telling the operator their integrations were "not
APPROVED". They would go audit approval records; the actual fault was an IAM
policy. These tests pin the honest failure mode.
"""

from __future__ import annotations

import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, "src")

from app import deployment_handler as dh  # noqa: E402
from app.services import aws_agent_registry as ar  # noqa: E402


class _Req:
    """Minimal stand-in for DeployRequest's gating-relevant surface."""

    def __init__(self, mcp=None, a2a=None):
        self.mcp_server_config = mcp or {}
        self.a2a_config = a2a or {}
        self.config = None


def _gate(monkeypatch, raiser):
    """Exercise only the gating block, with the registry lookup replaced."""
    monkeypatch.setattr(ar, "unapproved_integrations", raiser)


def test_registry_query_failure_is_503_not_403(monkeypatch):
    def _boom(_idents):
        raise ar.RegistryQueryFailed("AccessDenied: agent-registry:ListRegistryRecords")

    _gate(monkeypatch, _boom)

    with pytest.raises(HTTPException) as ei:
        _run_gating(_Req(mcp={"endpoint": "https://mcp.example/mcp"}))

    assert ei.value.status_code == 503, "unknown approval status must not read as denied"
    detail = str(ei.value.detail)
    assert "unknown" in detail.lower()
    assert "ListRegistryRecords" in detail, "the error must name the permission to fix"
    assert "not APPROVED" not in detail, "must not blame the customer's integrations"


def test_genuine_denial_is_still_403(monkeypatch):
    _gate(monkeypatch, lambda idents: list(idents))

    with pytest.raises(HTTPException) as ei:
        _run_gating(_Req(mcp={"endpoint": "https://mcp.example/mcp"}))

    assert ei.value.status_code == 403
    assert "not APPROVED" in str(ei.value.detail)


def test_approved_integration_passes_gating(monkeypatch):
    _gate(monkeypatch, lambda _idents: [])
    _run_gating(_Req(mcp={"endpoint": "https://mcp.example/mcp"}))  # no raise


def test_federation_off_is_a_noop(monkeypatch):
    """No identifiers collected → gating never consults the registry at all."""

    def _must_not_be_called(_idents):
        raise AssertionError("gating queried the registry with no integrations")

    _gate(monkeypatch, _must_not_be_called)
    _run_gating(_Req())  # no mcp/a2a config → nothing to gate


def _run_gating(request):
    """Replicates handle_deploy's gating block against the live module symbols.

    handle_deploy() itself needs DynamoDB, Step Functions and auth; this exercises
    the branch under test using the same imports and control flow, so the 503/403
    mapping is verified rather than assumed.
    """
    from app.services.aws_agent_registry import (
        RegistryQueryFailed,
        unapproved_integrations,
    )

    idents: list[str] = []
    mcp = request.mcp_server_config or {}
    if isinstance(mcp, dict):
        for k in ("endpoint", "url", "name", "server_url", "serverUrl"):
            if mcp.get(k):
                idents.append(str(mcp[k]))
    a2a = request.a2a_config or {}
    if isinstance(a2a, dict):
        for u in a2a.get("peer_allowlist") or a2a.get("peerAllowlist") or []:
            idents.append(str(u))

    if not idents:
        return

    try:
        blocked = unapproved_integrations(idents)
    except RegistryQueryFailed as rqe:
        raise HTTPException(
            status_code=503,
            detail=(
                "Integration gating is enabled but the Agent Registry could not be "
                f"queried, so approval status is unknown ({rqe}). Refusing the deploy "
                "rather than let an unreviewed integration through. Check that the "
                "deployment role holds agent-registry:ListRegistryRecords and that the "
                "configured registry id is correct."
            ),
        ) from rqe
    if blocked:
        raise HTTPException(
            status_code=403,
            detail=(
                "These integrations are not APPROVED in the Agent Registry and "
                f"cannot be used in a deployment: {blocked}"
            ),
        )


def test_handler_source_matches_this_replica():
    """Guard against the replica above drifting from handle_deploy().

    A copy of production control flow is only worth testing if it stays a copy.
    """
    import inspect

    src = inspect.getsource(dh.handle_deploy)
    assert "except RegistryQueryFailed" in src
    assert "status_code=503" in src
    assert "agent-registry:ListRegistryRecords" in src
    assert "approval status is unknown" in src
    # the 403 denial branch must still exist alongside it
    assert "not APPROVED in the Agent Registry" in src
