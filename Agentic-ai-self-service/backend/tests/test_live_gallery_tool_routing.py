"""Customer-visible routing contract for the live AgentCore gateway deployer.

The CloudFormation exporter has a separate artifact-level oracle.  This file
pins the other production path: the Lambda family and inline tool schemas that
``deploy_gateway`` sends to the AgentCore control plane.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from app.services import gateway_deployer

from tests.test_gateway_empty_tool_plane_retry import _FakeCtrl, _install

CANONICAL_TOOLS = (
    "get_order",
    "get_customer",
    "list_orders",
    "process_refund",
)
LEGACY_TOOLS = (
    "check_order_status",
    "lookup_customer",
    "search_knowledge_base",
    "get_return_policy",
)
WEB_TOOLS = (
    "duckduckgo_search",
    "wikipedia_search",
    "get_weather",
    "fetch_webpage",
)


def _exercise_live_route(
    monkeypatch: pytest.MonkeyPatch,
    *,
    template_id: str | None,
    gateway_tools: list[str],
    expected_count: int,
) -> tuple[dict, list[str], list[tuple[str, dict]]]:
    ctrl = _FakeCtrl()
    _install(
        monkeypatch,
        ctrl=ctrl,
        served=expected_count,
        expected=expected_count,
    )

    lambda_calls: list[str] = []

    def _lambda_factory(family: str) -> Callable[..., str]:
        def _create(*_args, **_kwargs) -> str:
            lambda_calls.append(family)
            return f"arn:aws:lambda:us-east-1:111122223333:function:{family}-tools"

        return _create

    monkeypatch.setattr(
        gateway_deployer,
        "create_dynamic_gateway_lambda",
        _lambda_factory("dynamic"),
    )
    monkeypatch.setattr(
        gateway_deployer,
        "create_customer_support_lambda",
        _lambda_factory("legacy"),
    )

    targets: list[tuple[str, dict]] = []

    def _capture_target(
        control_client,
        gateway_id: str,
        target_name: str,
        create_params: dict,
        **_kwargs,
    ) -> dict:
        assert control_client is ctrl
        assert gateway_id
        targets.append((target_name, create_params))
        return {"targetId": f"target-{len(targets)}", "status": "READY"}

    monkeypatch.setattr(
        gateway_deployer,
        "_create_gateway_target_with_retry",
        _capture_target,
    )

    result = gateway_deployer.deploy_gateway(
        {"name": "gallery-routing-probe"},
        "us-east-1",
        template_id=template_id,
        gateway_tools=gateway_tools,
        deployment_id="d-gallery-routing",
    )
    return result, lambda_calls, targets


@pytest.mark.parametrize(
    (
        "template_id",
        "gateway_tools",
        "expected_target",
        "expected_schema_names",
        "expected_lambda_family",
    ),
    (
        (
            "strands-gateway-agent",
            [],
            "DynamicTools",
            WEB_TOOLS + CANONICAL_TOOLS,
            "dynamic",
        ),
        (
            "customer-support-assistant",
            [],
            "CustomerSupportTools",
            LEGACY_TOOLS,
            "legacy",
        ),
        (
            "customer-support-blueprint",
            list(CANONICAL_TOOLS),
            "DynamicTools",
            CANONICAL_TOOLS,
            "dynamic",
        ),
        (
            None,
            list(CANONICAL_TOOLS),
            "DynamicTools",
            CANONICAL_TOOLS,
            "dynamic",
        ),
    ),
)
def test_live_gateway_advertises_exact_gallery_contract_and_uses_matching_lambda(
    monkeypatch: pytest.MonkeyPatch,
    template_id: str | None,
    gateway_tools: list[str],
    expected_target: str,
    expected_schema_names: tuple[str, ...],
    expected_lambda_family: str,
) -> None:
    expected_count = len(expected_schema_names)
    result, lambda_calls, targets = _exercise_live_route(
        monkeypatch,
        template_id=template_id,
        gateway_tools=gateway_tools,
        expected_count=expected_count,
    )

    assert result["success"] is True, result
    assert lambda_calls == [expected_lambda_family]
    assert len(targets) == 1

    target_name, create_params = targets[0]
    assert target_name == expected_target
    assert create_params["name"] == expected_target
    schemas = create_params["targetConfiguration"]["mcp"]["lambda"]["toolSchema"]["inlinePayload"]
    assert tuple(schema["name"] for schema in schemas) == expected_schema_names
    assert "knowledge_base_query" not in expected_schema_names


def test_live_gateway_refuses_an_unknown_tool_instead_of_silently_dropping_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, lambda_calls, targets = _exercise_live_route(
        monkeypatch,
        template_id=None,
        gateway_tools=["get_order", "not_a_real_gateway_tool"],
        expected_count=1,
    )

    assert result["success"] is False, (
        "A partial target is not the deployment the caller requested: an unknown "
        "tool must fail the gateway instead of disappearing beside a known tool."
    )
    assert "not_a_real_gateway_tool" in result["error"]
    assert lambda_calls == []
    assert targets == []


def test_live_gateway_never_silently_drops_one_lambda_family_from_a_mixed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, lambda_calls, targets = _exercise_live_route(
        monkeypatch,
        template_id=None,
        gateway_tools=["check_order_status", "get_order"],
        expected_count=2,
    )

    if result["success"] is False:
        error = result["error"].lower()
        assert "legacy" in error
        assert "dynamic" in error or "mixed" in error
        assert lambda_calls == []
        assert targets == []
        return

    # Supporting the combination is also valid, but it must be complete and
    # leave both Lambda functions discoverable for cleanup.
    assert set(lambda_calls) == {"legacy", "dynamic"}
    schemas_by_target = {
        name: tuple(
            schema["name"] for schema in params["targetConfiguration"]["mcp"]["lambda"]["toolSchema"]["inlinePayload"]
        )
        for name, params in targets
    }
    assert schemas_by_target == {
        "CustomerSupportTools": ("check_order_status",),
        "DynamicTools": ("get_order",),
    }
    cleanup_names = result.get("lambda_function_names") or [result.get("lambda_function_name")]
    assert set(cleanup_names) == {"legacy-tools", "dynamic-tools"}
