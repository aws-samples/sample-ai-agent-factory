"""Tests for registry integration gating (Loom-study 1.4).

Only APPROVED external MCP/A2A integrations may be used in a deployment when
federation is enabled. unapproved_integrations() returns the blocked identifiers.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from app.services import aws_agent_registry as reg  # noqa: E402


class _FakeRegistry:
    """Stands in for AwsAgentRegistry, recording the filters it was asked for.

    Exposes list_records_strict() because that is what gating must call: a
    fail-closed check may not accept an empty list as a verdict. The fake ignores
    `filters` when returning records so the caller's own APPROVED re-check stays
    under test (a control plane that ignored `filters` must not widen the gate).
    """

    def __init__(self, records):
        self._records = records
        self.filters_seen: list | None = None

    def list_records_strict(self, filters=None):
        self.filters_seen = filters
        return self._records


class _BrokenRegistry:
    """A registry that cannot answer — AccessDenied, throttle, bad filter shape."""

    def __init__(self, message="AccessDenied: agent-registry:ListRegistryRecords"):
        self._message = message

    def list_records_strict(self, filters=None):
        raise reg.RegistryQueryFailed(self._message)


def _patch(monkeypatch, registry):
    monkeypatch.setattr(reg, "get_registry", lambda: registry)


def test_disabled_federation_is_noop(monkeypatch):
    _patch(monkeypatch, None)
    assert reg.unapproved_integrations(["https://mcp.notion.com/mcp"]) == []


def test_approved_by_name_passes(monkeypatch):
    _patch(
        monkeypatch,
        _FakeRegistry(
            [
                {"name": "notion-mcp", "status": "APPROVED"},
            ]
        ),
    )
    assert reg.unapproved_integrations(["notion-mcp"]) == []


def test_approved_by_url_substring_passes(monkeypatch):
    _patch(
        monkeypatch,
        _FakeRegistry(
            [
                {"name": "notion", "status": "APPROVED", "descriptors": {"mcp": {"url": "https://mcp.notion.com/mcp"}}},
            ]
        ),
    )
    assert reg.unapproved_integrations(["https://mcp.notion.com/mcp"]) == []


def test_unapproved_status_is_blocked(monkeypatch):
    _patch(
        monkeypatch,
        _FakeRegistry(
            [
                {"name": "notion-mcp", "status": "PENDING_APPROVAL"},
            ]
        ),
    )
    assert reg.unapproved_integrations(["notion-mcp"]) == ["notion-mcp"]


def test_unknown_integration_is_blocked_fail_closed(monkeypatch):
    _patch(
        monkeypatch,
        _FakeRegistry(
            [
                {"name": "something-else", "status": "APPROVED"},
            ]
        ),
    )
    # No record names/points-at this one → fail-closed (blocked).
    assert reg.unapproved_integrations(["https://evil.example/mcp"]) == ["https://evil.example/mcp"]


def test_mixed(monkeypatch):
    _patch(
        monkeypatch,
        _FakeRegistry(
            [
                {"name": "ok-mcp", "status": "APPROVED"},
                {"name": "pending-mcp", "status": "DRAFT"},
            ]
        ),
    )
    blocked = reg.unapproved_integrations(["ok-mcp", "pending-mcp", "ghost-mcp"])
    assert set(blocked) == {"pending-mcp", "ghost-mcp"}


# -- GA control-plane filter shape -------------------------------------------


def test_gating_asks_the_control_plane_to_filter_approved(monkeypatch):
    """The APPROVED narrowing is pushed server-side using the GA filters shape.

    GA's ListRegistryRecords takes filters=[{"name": ..., "values": [...]}] where
    name ∈ {name, status, recordType}. Sending the old flat kwargs (or nothing)
    means paging the whole catalog client-side.
    """
    fake = _FakeRegistry([{"name": "ok-mcp", "status": "APPROVED"}])
    _patch(monkeypatch, fake)
    reg.unapproved_integrations(["ok-mcp"])
    assert fake.filters_seen == [{"name": "status", "values": ["APPROVED"]}]


def test_approved_by_display_name_passes(monkeypatch):
    """GA records carry displayName alongside name; either may match."""
    _patch(
        monkeypatch,
        _FakeRegistry([{"name": "notion_mcp", "displayName": "notion-mcp", "status": "APPROVED"}]),
    )
    assert reg.unapproved_integrations(["notion-mcp"]) == []


# -- absent data is not negative data ----------------------------------------


def test_registry_failure_raises_instead_of_reporting_everything_unapproved(monkeypatch):
    """The defect this guards: a wrong IAM action name made list_records() return
    [], which made every integration look UNAPPROVED, which produced a 403 telling
    the operator their integrations were rejected. The real cause was AccessDenied.
    Gating must surface "unknown", never silently convert it into "denied"."""
    _patch(monkeypatch, _BrokenRegistry())
    try:
        reg.unapproved_integrations(["https://mcp.notion.com/mcp"])
    except reg.RegistryQueryFailed as e:
        assert "ListRegistryRecords" in str(e)
    else:
        raise AssertionError("a failed registry query must not yield a silent verdict")


def test_genuinely_empty_registry_still_fails_closed(monkeypatch):
    """The counterpart: a registry that answers and holds nothing IS a verdict.
    Fail-closed must survive the fix — this is not a licence to fail open."""
    _patch(monkeypatch, _FakeRegistry([]))
    assert reg.unapproved_integrations(["https://mcp.notion.com/mcp"]) == ["https://mcp.notion.com/mcp"]


def test_lenient_list_records_still_degrades_for_display_surfaces():
    """Read-only inventory views keep the forgiving behavior: a short list there is
    cosmetic, not a policy decision."""
    a = reg.AwsAgentRegistry.__new__(reg.AwsAgentRegistry)
    a.registry_id = "reg1"
    a.region = "us-east-1"
    a.control = None
    a.data = None
    assert a.list_records() == []


def test_strict_list_records_raises_when_client_is_missing():
    """An old boto3 bundle has no agent-registry models. For display that's [];
    for gating it must be an exception, or federation would silently stop gating."""
    a = reg.AwsAgentRegistry.__new__(reg.AwsAgentRegistry)
    a.registry_id = "reg1"
    a.region = "us-east-1"
    a.control = None
    a.data = None
    try:
        a.list_records_strict()
    except reg.RegistryQueryFailed as e:
        assert "boto3" in str(e)
    else:
        raise AssertionError("missing client must raise, not return []")


def test_strict_list_records_carries_partial_pages_on_failure():
    """Pages already read are preserved on the exception for diagnostics, without
    being mistaken for a complete answer."""

    class _HalfBroken:
        def __init__(self):
            self.n = 0

        def list_registry_records(self, **kw):
            self.n += 1
            if self.n == 1:
                return {"registryRecords": [{"name": "one"}], "nextToken": "t1"}
            raise RuntimeError("Throttling")

    a = reg.AwsAgentRegistry.__new__(reg.AwsAgentRegistry)
    a.registry_id = "reg1"
    a.region = "us-east-1"
    a.control = _HalfBroken()
    a.data = None
    try:
        a.list_records_strict()
    except reg.RegistryQueryFailed as e:
        assert [r["name"] for r in e.partial] == ["one"]
        assert "Throttling" in str(e)
    else:
        raise AssertionError("a mid-pagination failure must raise")
