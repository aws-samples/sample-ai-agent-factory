"""Regressions for defects discovered by the exact-commit live gallery campaign."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import requests
from app import deployment_handler
from app.routers import versions
from app.services.agent_versions_store import RuntimeSlots

from tests.integration import conftest as integration_fixtures
from tests.integration import test_template_deployments as gallery


def _qualified_receipt(invocation: gallery.ToolInvocation, *, name: str) -> dict[str, Any]:
    arguments = gallery._EXPECTED_TOOL_ARGUMENTS[invocation.tool_name][0]
    return {
        "name": name,
        "status": "success",
        "input_sha256": gallery._receipt_digest(arguments),
        "argument_sha256": {key: gallery._receipt_digest(value) for key, value in arguments.items()},
    }


def test_gateway_qualified_receipt_keeps_exact_argument_proof() -> None:
    invocation = gallery._DYNAMIC_WEB_INVOCATIONS[0]
    gallery._require_exact_tool_receipt(
        {
            "toolReceipts": [
                _qualified_receipt(
                    invocation,
                    name="AmazonWebSearch___WebSearch",
                )
            ]
        },
        invocation,
    )


@pytest.mark.parametrize(
    "name",
    (
        "DynamicTools___fetch_webpage",
        "AmazonWebSearch...WebSearch",
        "___WebSearch",
        "AmazonWebSearch___WebSearch___",
        "Amazon.WebSearch___WebSearch",
    ),
)
def test_gateway_receipt_rejects_wrong_or_malformed_published_names(name: str) -> None:
    invocation = gallery._DYNAMIC_WEB_INVOCATIONS[0]
    with pytest.raises(AssertionError, match="lacks a successful exact-argument receipt"):
        gallery._require_exact_tool_receipt(
            {"toolReceipts": [_qualified_receipt(invocation, name=name)]},
            invocation,
        )


def test_qualified_name_branch_removal_mutation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    invocation = gallery._DYNAMIC_WEB_INVOCATIONS[0]
    monkeypatch.setattr(
        gallery,
        "_receipt_tool_name_matches",
        lambda published, requested: published == requested,
    )
    with pytest.raises(AssertionError, match="lacks a successful exact-argument receipt"):
        gallery._require_exact_tool_receipt(
            {
                "toolReceipts": [
                    _qualified_receipt(
                        invocation,
                        name="AmazonWebSearch___WebSearch",
                    )
                ]
            },
            invocation,
        )


def test_tool_wiring_sessions_are_isolated_but_memory_pair_is_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def invoke(_api_session, **kwargs):
        calls.append(dict(kwargs))
        if kwargs["label"] == "memory recall":
            return "The customer prefers text message delivery updates."
        if kwargs["label"] == "memory write":
            return "Preference recorded."
        return "{}"

    monkeypatch.setattr(gallery, "_invoke_http", invoke)
    case = gallery.TemplateCase(
        template_id="customer-support-assistant",
        name_token="support",
        protocol="HTTP",
        invocations=(
            gallery.ToolInvocation("first", "first", "first", lambda _value: True),
            gallery.ToolInvocation("second", "second", "second", lambda _value: True),
        ),
        verify_memory_recall=True,
    )

    gallery._invoke_http_template(object(), case=case, status={})

    tool_calls = [call for call in calls if call.get("invocation") is not None]
    memory_calls = [call for call in calls if call.get("invocation") is None]
    assert len(tool_calls) == 2
    assert tool_calls[0]["session_id"] != tool_calls[1]["session_id"]
    assert len(memory_calls) == 2
    assert memory_calls[0]["session_id"] == memory_calls[1]["session_id"]
    assert memory_calls[0]["session_id"] not in {call["session_id"] for call in tool_calls}
    assert "text message" in memory_calls[0]["prompt"]
    assert "blue orchard" not in memory_calls[0]["prompt"]
    assert "verification phrase" not in memory_calls[0]["prompt"]


@pytest.mark.parametrize(
    ("template_id", "default_name"),
    tuple(deployment_handler._TEMPLATE_DEFAULT_GATEWAY_NAMES.items()),
)
def test_gallery_default_gateway_follows_the_runtime_name(
    template_id: str,
    default_name: str,
) -> None:
    original = {"name": default_name, "targets": []}
    prepared = deployment_handler._pair_template_default_gateway_name(
        original,
        template_id=template_id,
        friendly_runtime_name="tenant_unique_runtime",
    )

    assert prepared == {"name": "tenant_unique_runtime", "targets": []}
    assert original == {"name": default_name, "targets": []}


def test_explicit_custom_gateway_name_is_never_rewritten() -> None:
    prepared = deployment_handler._pair_template_default_gateway_name(
        {"name": "customer-owned-gateway"},
        template_id="mcp-server-gateway-target",
        friendly_runtime_name="tenant_unique_runtime",
    )

    assert prepared == {"name": "customer-owned-gateway"}


def test_unknown_template_cannot_trigger_gallery_name_rewrite() -> None:
    prepared = deployment_handler._pair_template_default_gateway_name(
        {"name": "support_gateway"},
        template_id="customer-authored-template",
        friendly_runtime_name="tenant_unique_runtime",
    )

    assert prepared == {"name": "support_gateway"}


class _StatusResponse:
    def __init__(self, body: dict[str, Any], *, error: requests.HTTPError | None = None) -> None:
        self._body = body
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> dict[str, Any]:
        return self._body


class _StatusSession:
    base_url = "https://agents.example.com"

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def get(self, _url: str, **_kwargs) -> _StatusResponse:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_status_read_recovers_from_bounded_transport_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration_fixtures.time, "sleep", lambda _seconds: None)
    session = _StatusSession(
        [
            requests.ConnectionError("closed"),
            requests.Timeout("slow"),
            _StatusResponse({"status": "succeeded"}),
        ]
    )

    assert integration_fixtures._deployment_status(session, "deployment-1") == {"status": "succeeded"}
    assert session.calls == 3


def test_status_read_exhaustion_and_http_errors_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration_fixtures.time, "sleep", lambda _seconds: None)
    exhausted = _StatusSession([requests.ConnectionError("closed")] * 4)
    with pytest.raises(requests.ConnectionError):
        integration_fixtures._deployment_status(exhausted, "deployment-1")
    assert exhausted.calls == 4

    authoritative = _StatusSession(
        [
            _StatusResponse(
                {"detail": "unavailable"},
                error=requests.HTTPError("HTTP 503"),
            ),
            _StatusResponse({"status": "succeeded"}),
        ]
    )
    with pytest.raises(requests.HTTPError):
        integration_fixtures._deployment_status(authoritative, "deployment-1")
    assert authoritative.calls == 1


def test_slots_route_uses_a_strong_read(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, bool]] = []

    class Store:
        def get(self, runtime_name: str, *, consistent: bool = False) -> RuntimeSlots:
            calls.append((runtime_name, consistent))
            return RuntimeSlots(
                runtime_name=runtime_name,
                owner_sub="owner-1",
                staging_version_id="version-1",
            )

    monkeypatch.setattr(versions, "get_slots_store", lambda: Store())

    response = asyncio.run(versions.get_slots("runtime_1", caller_sub="owner-1"))

    assert response.staging_version_id == "version-1"
    assert calls == [("runtime_1", True)]


def test_generic_gateway_live_contract_excludes_optional_managed_web_search() -> None:
    cases = {case.template_id: case for case in gallery.TEMPLATE_CASES}
    generic_tools = {invocation.tool_name for invocation in cases["strands-gateway-agent"].invocations}
    dedicated_tools = {invocation.tool_name for invocation in cases["web-search-agent"].invocations}

    assert "WebSearch" not in generic_tools
    assert {"wikipedia_search", "get_weather", "fetch_webpage"} <= generic_tools
    assert {"get_order", "get_customer", "list_orders", "process_refund"} <= generic_tools
    assert "WebSearch" in dedicated_tools
