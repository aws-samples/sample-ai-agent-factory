"""Tests for registry integration gating (Loom-study 1.4).

Only APPROVED external MCP/A2A integrations may be used in a deployment when
federation is enabled. unapproved_integrations() returns the blocked identifiers.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from app.services import aws_agent_registry as reg  # noqa: E402


class _FakeRegistry:
    def __init__(self, records):
        self._records = records

    def list_records(self):
        return self._records


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
