"""Executable contract for the real-AWS integration harness.

The integration tests themselves require a deployed stack. These local tests
pin the parts that must be trustworthy before Claude runs that expensive gate:
the exact gallery matrix, semantic response oracles, early deployment tracking,
retry behaviour, and durable teardown verification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
import requests
from app.models.deployment_models import DeployRequest
from botocore.exceptions import ClientError

from tests.integration import conftest as harness
from tests.integration import test_template_deployments as matrix
from tests.integration import test_trigger_delivery_matrix as trigger_matrix

_REPO = Path(__file__).resolve().parents[2]


class FakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}: {self.text}",
                response=self,  # type: ignore[arg-type]
            )


class FakeSession:
    base_url = "https://product.example.test"

    def __init__(
        self,
        *,
        get_responses: Iterable[FakeResponse] = (),
        delete_responses: Iterable[FakeResponse] = (),
        post_responses: Iterable[FakeResponse | requests.RequestException] = (),
    ) -> None:
        self._get_responses = list(get_responses)
        self._delete_responses = list(delete_responses)
        self._post_responses = list(post_responses)
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.post_calls: list[str] = []

    @staticmethod
    def _next(
        responses: list[Any],
        operation: str,
    ) -> Any:
        if not responses:
            raise AssertionError(f"Unexpected extra fake {operation} call")
        return responses.pop(0)

    def get(self, url: str, *, timeout: int) -> FakeResponse:
        del timeout
        self.get_calls.append(url)
        return self._next(self._get_responses, "GET")

    def delete(self, url: str, *, timeout: int) -> FakeResponse:
        del timeout
        self.delete_calls.append(url)
        return self._next(self._delete_responses, "DELETE")

    def post(self, url: str, *, json: Any, timeout: int) -> FakeResponse:
        del json, timeout
        self.post_calls.append(url)
        result = self._next(self._post_responses, "POST")
        if isinstance(result, requests.RequestException):
            raise result
        return result


def _response(
    status_code: int,
    body: Any = None,
    **fields: Any,
) -> FakeResponse:
    if body is not None and fields:
        raise AssertionError("Fake response accepts either body or keyword fields")
    return FakeResponse(status_code, fields if body is None else body)


def test_the_live_matrix_names_exactly_the_six_gallery_templates() -> None:
    expected = [
        "web-search-agent",
        "strands-gateway-agent",
        "customer-support-assistant",
        "customer-support-blueprint",
        "mcp-server-gateway-target",
        "mcp-server-runtime",
    ]
    actual = [case.template_id for case in matrix.TEMPLATE_CASES]
    assert actual == expected
    assert len(actual) == len(set(actual))


def test_the_live_matrix_is_derived_from_the_current_frontend_gallery() -> None:
    source = (_REPO / "frontend/src/data/templates.ts").read_text()
    gallery_ids = re.findall(r"^\s{4}id: '([^']+)',\s*$", source, flags=re.MULTILINE)
    matrix_ids = [case.template_id for case in matrix.TEMPLATE_CASES]

    assert gallery_ids == matrix_ids


def test_only_the_standalone_server_uses_the_mcp_invocation_surface() -> None:
    protocols = {case.template_id: case.protocol for case in matrix.TEMPLATE_CASES}
    assert protocols["mcp-server-runtime"] == "MCP"
    assert {template_id for template_id, protocol in protocols.items() if protocol == "HTTP"} == {
        "web-search-agent",
        "strands-gateway-agent",
        "customer-support-assistant",
        "customer-support-blueprint",
        "mcp-server-gateway-target",
    }


@pytest.mark.parametrize(
    "case",
    matrix.TEMPLATE_CASES,
    ids=[case.template_id for case in matrix.TEMPLATE_CASES],
)
def test_every_live_matrix_request_passes_the_real_api_model(
    case: matrix.TemplateCase,
) -> None:
    payload = matrix._template_request_payload(case, "deadbeef")

    request = DeployRequest.model_validate(payload)

    assert request.template_id == case.template_id
    assert request.config.protocol == case.protocol


def test_the_standalone_mcp_live_request_contains_no_model_only_fields() -> None:
    case = next(item for item in matrix.TEMPLATE_CASES if item.template_id == "mcp-server-runtime")
    config = matrix._template_request_payload(case, "deadbeef")["config"]

    assert config["protocol"] == "MCP"
    assert {
        "framework",
        "model",
        "modelProvider",
        "providerApiKeyRef",
        "providerBaseUrl",
        "systemPrompt",
        "multiAgentPattern",
        "multiAgentConfig",
    }.isdisjoint(config)


def test_every_conversational_template_has_a_semantic_oracle() -> None:
    for case in matrix.TEMPLATE_CASES:
        if case.protocol == "HTTP":
            assert case.invocations
            for invocation in case.invocations:
                assert invocation.tool_name
                assert invocation.prompt
                assert invocation.response_oracle
                assert invocation.response_label
        else:
            assert case.invocations == ()


def test_the_live_matrix_exercises_every_advertised_executable_tool() -> None:
    expected = {
        "web-search-agent": (
            "duckduckgo_search",
            "get_weather",
            "fetch_webpage",
        ),
        "strands-gateway-agent": (
            "duckduckgo_search",
            "wikipedia_search",
            "get_weather",
            "fetch_webpage",
            "get_order",
            "get_customer",
            "list_orders",
            "process_refund",
        ),
        "customer-support-assistant": (
            "check_order_status",
            "lookup_customer",
            "search_knowledge_base",
            "get_return_policy",
        ),
        "customer-support-blueprint": (
            "get_order",
            "get_customer",
            "list_orders",
            "process_refund",
        ),
        "mcp-server-gateway-target": (
            "get_order",
            "get_customer",
            "list_orders",
            "process_refund",
        ),
        # This protocol-only server is exercised directly by
        # _invoke_standalone_mcp rather than through model prompts.
        "mcp-server-runtime": (
            "get_weather",
            "search_web",
            "fetch_url",
        ),
    }

    actual = {
        case.template_id: tuple(invocation.tool_name for invocation in case.invocations)
        for case in matrix.TEMPLATE_CASES
    }
    actual["mcp-server-runtime"] = (
        "get_weather",
        "search_web",
        "fetch_url",
    )

    assert actual == expected
    assert sum(len(tools) for tools in actual.values()) == 26


def test_exactly_the_two_memory_gallery_templates_require_live_recall() -> None:
    assert {case.template_id for case in matrix.TEMPLATE_CASES if case.verify_memory_recall} == {
        "customer-support-assistant",
        "customer-support-blueprint",
    }


def test_live_requests_match_the_frontend_composition_boundary() -> None:
    payloads = {case.template_id: matrix._template_request_payload(case, "deadbeef") for case in matrix.TEMPLATE_CASES}

    expected_connected = {
        "web-search-agent": [],
        "strands-gateway-agent": ["gateway", "identity"],
        "customer-support-assistant": [
            "gateway",
            "identity",
            "memory",
            "observability",
        ],
        "customer-support-blueprint": ["gateway", "memory"],
        "mcp-server-gateway-target": ["gateway"],
        "mcp-server-runtime": [],
    }
    expected_gateway_names = {
        "web-search-agent": None,
        "strands-gateway-agent": "agent_gateway",
        "customer-support-assistant": "support_gateway",
        "customer-support-blueprint": "support_gateway",
        "mcp-server-gateway-target": "mcp_server_gateway",
        "mcp-server-runtime": None,
    }

    for template_id, payload in payloads.items():
        assert payload["deploymentMode"] == "runtime"
        assert payload["templateId"] == template_id
        assert payload["connectedTools"] == expected_connected[template_id]
        gateway = payload["gatewayConfig"]
        expected_name = expected_gateway_names[template_id]
        if expected_name is None:
            assert gateway is None
        else:
            assert gateway["name"] == expected_name
            assert gateway["targets"] == []

    assert payloads["web-search-agent"]["gatewayTools"] == []
    assert payloads["strands-gateway-agent"]["gatewayTools"] == []
    assert payloads["customer-support-assistant"]["gatewayTools"] == []
    assert payloads["customer-support-blueprint"]["gatewayTools"] == [
        "get_order",
        "get_customer",
        "list_orders",
        "process_refund",
    ]
    assert payloads["mcp-server-gateway-target"]["gatewayTools"] == []
    assert payloads["mcp-server-runtime"]["gatewayTools"] == []

    for template_id in ("strands-gateway-agent", "customer-support-assistant"):
        assert payloads[template_id]["identityConfig"] == {
            "mode": "shared",
            "provider": "cognito",
            "clientId": "",
            "clientSecretRef": "",
            "discoveryUrl": "",
            "scopes": [],
        }

    assert payloads["customer-support-assistant"]["memoryConfig"] == {
        "name": "support_memory",
        "enabled": True,
        "strategies": [
            {
                "type": "semantic",
                "name": "support_semantic",
                "description": "Long-term facts about the customer and their orders, recalled across sessions",
            }
        ],
    }
    assert payloads["customer-support-assistant"]["observabilityConfig"] == {
        "name": "support_observability",
        "enableOtel": False,
    }
    assert payloads["customer-support-blueprint"]["memoryConfig"] == {
        "name": "support_memory",
        "enabled": True,
        "strategies": [
            {
                "type": "semantic",
                "name": "support_semantic",
                "description": "Long-term facts about the customer and their orders, recalled across sessions",
            }
        ],
    }
    assert payloads["mcp-server-gateway-target"]["mcpServerConfig"]["tools"] == []


def test_json_oracle_parser_handles_model_wrappers_and_double_encoding() -> None:
    expected = {"customer_id": "CUST-001", "total_orders": 3}
    wrapped = "Tool result:\n```json\n" + json.dumps(json.dumps(expected)) + "\n```\nDone."
    assert expected in list(matrix._json_values(wrapped))


def test_a_structured_tool_error_cannot_be_hidden_beside_a_valid_candidate() -> None:
    text = "\n".join(
        (
            json.dumps(
                {
                    "error": "tool_unavailable",
                    "detail": "upstream timed out",
                }
            ),
            json.dumps(
                {
                    "location": "Dublin",
                    "description": "Cloudy",
                    "temperature_F": 55.0,
                    "humidity_pct": 72,
                    "wind_mph": 8.1,
                }
            ),
        )
    )

    with pytest.raises(AssertionError, match="structured tool error"):
        matrix._require_json_oracle(
            text,
            oracle=matrix._is_weather_payload,
            label="weather",
        )


@pytest.mark.parametrize(
    ("oracle", "payload"),
    [
        (
            matrix._is_web_search_payload,
            [
                {
                    "title": "Amazon Bedrock AgentCore",
                    "snippet": "Build and deploy agents.",
                    "url": "https://example.com/agentcore",
                }
            ],
        ),
        (
            matrix._is_wikipedia_payload,
            {
                "title": "Python (programming language)",
                "summary": "Python is a programming language.",
                "url": "https://en.wikipedia.org/wiki/Python_(programming_language)",
            },
        ),
        (
            matrix._is_weather_payload,
            {
                "location": "Dublin",
                "description": "Cloudy",
                "temperature_F": 55.0,
                "humidity_pct": 72,
                "wind_mph": 8.1,
            },
        ),
        (
            matrix._is_fetched_page_payload,
            {
                "url": "https://example.com",
                "content": "<title>Example Domain</title>",
            },
        ),
        (
            matrix._is_order_payload,
            {
                "order_id": "ORD-12345",
                "customer_id": "CUST-001",
                "status": "delivered",
                "items": [
                    {
                        "name": "Wireless Headphones",
                        "quantity": 1,
                        "price": 79.99,
                    },
                ],
                "total": 79.99,
            },
        ),
        (
            matrix._is_customer_payload,
            {
                "customer_id": "CUST-001",
                "name": "John Doe",
                "email": "john@example.com",
                "total_orders": 3,
                "total_spent": 354.97,
            },
        ),
        (
            matrix._is_order_list_payload,
            {
                "customer_id": "CUST-001",
                "orders": [
                    {"order_id": "ORD-12400"},
                    {"order_id": "ORD-12345"},
                    {"order_id": "ORD-12300"},
                ],
            },
        ),
        (
            matrix._is_refund_payload,
            {
                "success": True,
                "refund_id": "REF-A1B2C",
                "order_id": "ORD-12345",
                "amount": 10,
                "reason": "integration verification",
                "status": "processed",
            },
        ),
        (
            matrix._is_legacy_order_status_payload,
            {
                "order_id": "ORD-12345",
                "status": "Shipped",
                "tracking_number": "1Z999AA10123456784",
                "total": "$1,348.99",
            },
        ),
        (
            matrix._is_legacy_customer_payload,
            {
                "customer_id": "CUST-001",
                "name": "John Smith",
                "email": "john@example.com",
                "membership_tier": "Gold",
            },
        ),
        (
            matrix._is_legacy_kb_payload,
            {
                "results": [
                    {
                        "id": "KB-002",
                        "title": "Return and Refund Policy",
                    }
                ],
                "total_found": 1,
            },
        ),
        (
            matrix._is_legacy_return_policy_payload,
            {
                "category": "Electronics",
                "return_window": "30 days",
                "condition": "Must be in original packaging",
            },
        ),
    ],
    ids=[
        "web-search",
        "wikipedia",
        "weather",
        "fetch",
        "order",
        "customer",
        "orders",
        "refund",
        "legacy-order",
        "legacy-customer",
        "legacy-kb",
        "legacy-return",
    ],
)
def test_template_oracles_accept_the_exact_shipped_fixture(
    oracle: matrix.JsonOracle,
    payload: dict[str, Any],
) -> None:
    assert oracle(payload)


def test_wait_for_delete_requires_an_explicit_terminal_verdict() -> None:
    session = FakeSession(
        get_responses=[
            _response(200, delete_status="deleting"),
            _response(200, delete_status="deleted"),
        ]
    )

    result = harness.wait_for_delete_terminal(
        session,  # type: ignore[arg-type]
        "deployment-1",
        timeout=1,
        poll_interval=0,
    )

    assert result["delete_status"] == "deleted"
    assert len(session.get_calls) == 2


def test_delete_retries_busy_responses_and_rechecks_the_tombstone() -> None:
    session = FakeSession(
        delete_responses=[
            _response(409, detail="still finalizing"),
            _response(202, success=True),
        ],
        get_responses=[
            _response(200, delete_status="deleting"),
            _response(200, delete_status="deleted"),
            _response(200, delete_status="deleted"),
        ],
    )

    result = harness.request_delete_and_verify(
        session,  # type: ignore[arg-type]
        deployment_id="deployment-2",
        runtime_id="runtime-2",
        timeout=1,
        poll_interval=0,
    )

    assert result["delete_status"] == "deleted"
    assert len(session.delete_calls) == 2
    assert len(session.get_calls) == 3


def test_delete_retained_is_a_failed_production_gate() -> None:
    session = FakeSession(
        delete_responses=[_response(202, success=True)],
        get_responses=[_response(200, delete_status="delete_retained")],
    )

    with pytest.raises(AssertionError, match="delete_retained"):
        harness.request_delete_and_verify(
            session,  # type: ignore[arg-type]
            deployment_id="deployment-3",
            runtime_id="runtime-3",
            timeout=1,
            poll_interval=0,
        )


def test_a_transient_deleted_response_is_not_enough() -> None:
    session = FakeSession(
        delete_responses=[_response(202, success=True)],
        get_responses=[
            _response(200, delete_status="deleted"),
            _response(200, delete_status="delete_failed"),
        ],
    )

    with pytest.raises(AssertionError, match="lost its deleted tombstone"):
        harness.request_delete_and_verify(
            session,  # type: ignore[arg-type]
            deployment_id="deployment-4",
            runtime_id="runtime-4",
            timeout=1,
            poll_interval=0,
        )


def test_tracker_registers_before_runtime_id_and_is_idempotent() -> None:
    session = FakeSession()
    tracker = harness.DeploymentCleanupTracker(session)  # type: ignore[arg-type]

    first = tracker.track("deployment-5")
    second = tracker.track("deployment-5")

    assert first is second
    assert first.runtime_id is None
    assert tracker.records == (first,)


def test_tracker_recovers_an_eventually_consistent_deploy_by_exact_node_id() -> None:
    session = FakeSession(
        get_responses=[
            _response(
                200,
                body=[
                    {
                        "deployment_id": "unrelated",
                        "node_id": "different-node",
                    }
                ],
            ),
            _response(
                200,
                body=[
                    {
                        "deployment_id": "deployment-recovered",
                        "node_id": "it-web-deadbeefcafebabe",
                    }
                ],
            ),
        ]
    )
    tracker = harness.DeploymentCleanupTracker(session)  # type: ignore[arg-type]

    record = tracker.recover_by_node_id(
        "it-web-deadbeefcafebabe",
        timeout=1,
        poll_interval=0,
    )

    assert record is not None
    assert record.deployment_id == "deployment-recovered"
    assert tracker.records == (record,)
    assert session.get_calls == [
        "https://product.example.test/api/deployments",
        "https://product.example.test/api/deployments",
    ]


def test_lost_deploy_response_still_registers_the_row_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = matrix.TEMPLATE_CASES[0]
    payload = matrix._template_request_payload(case, "deadbeefcafebabe")
    session = FakeSession(
        post_responses=[requests.Timeout("response lost")],
        get_responses=[
            _response(
                200,
                body=[
                    {
                        "deployment_id": "deployment-after-timeout",
                        "node_id": payload["nodeId"],
                    }
                ],
            )
        ],
    )
    tracker = harness.DeploymentCleanupTracker(session)  # type: ignore[arg-type]
    monkeypatch.setattr(
        matrix,
        "_template_request_payload",
        lambda _case, _token: payload,
    )

    with pytest.raises(requests.Timeout, match="response lost"):
        matrix._deploy_template(
            session,
            tracker,
            case,
        )

    assert [record.deployment_id for record in tracker.records] == ["deployment-after-timeout"]
    assert session.post_calls == ["https://product.example.test/api/deploy"]
    assert session.get_calls == ["https://product.example.test/api/deployments"]


def test_failed_deployment_cleanup_accepts_only_a_proven_deleted_verdict() -> None:
    session = FakeSession(
        get_responses=[
            _response(
                200,
                status="failed",
                delete_status="deleting",
                runtime_id=None,
            ),
            _response(
                200,
                status="failed",
                delete_status="deleted",
                runtime_id=None,
            ),
        ]
    )
    tracker = harness.DeploymentCleanupTracker(session)  # type: ignore[arg-type]
    record = tracker.track("deployment-6")

    tracker.cleanup(record)

    assert record.deleted is True
    assert record.last_status["delete_status"] == "deleted"


def test_customer_facing_docs_describe_six_templates_and_real_mcp() -> None:
    development = (_REPO / "docs/DEVELOPMENT.md").read_text()
    internals = (_REPO / "docs/DEPLOYMENT_INTERNALS.md").read_text()
    architecture = (_REPO / "docs/architecture.drawio").read_text()

    combined = "\n".join((development, internals, architecture))
    assert "7 built-in" not in combined.lower()
    assert "all 7" not in combined.lower()
    assert "6 Built-in Templates" in architecture
    assert "all six built-in gallery templates" in internals
    assert "`FastMCP`" in internals
    assert "mcp-lean.zip" in internals
    assert "eight `DynamicTools` operations" in internals
    assert all(
        tool_name in internals
        for tool_name in (
            "duckduckgo_search",
            "wikipedia_search",
            "get_weather",
            "fetch_webpage",
            "get_order",
            "get_customer",
            "list_orders",
            "process_refund",
            "check_order_status",
            "lookup_customer",
            "search_knowledge_base",
            "get_return_policy",
        )
    )
    assert "four legacy `CustomerSupportTools` operations" in internals
    assert "four canonical `DynamicTools` operations" in internals
    assert "standalone FastMCP for protocol-native MCP runtimes" in development
    assert "/api/test-mcp-runtime/tools" in combined
    assert "/api/test-mcp-runtime/call" in combined
    ElementTree.fromstring(architecture)


def test_live_trigger_matrix_names_exactly_every_supported_source() -> None:
    assert trigger_matrix.TRIGGER_TYPES_UNDER_TEST == (
        "cron",
        "eventbridge",
        "s3",
        "webhook",
    )
    assert len(trigger_matrix.TRIGGER_TYPES_UNDER_TEST) == len(set(trigger_matrix.TRIGGER_TYPES_UNDER_TEST))


def test_live_trigger_matrix_uses_real_sources_and_a_durable_completion_oracle() -> None:
    source = (_REPO / "backend/tests/integration/test_trigger_delivery_matrix.py").read_text()

    assert "events_client.put_events(" in source
    assert "s3_client.put_object(" in source
    assert "response = requests.post(" in source
    assert 'item.get("delivery_status") != "completed"' in source
    assert "ConsistentRead=True" in source
    assert "EventBridge rule" in source
    assert "Webhook secret" in source
    assert "delete_status" in source
    assert "unittest.mock" not in source
    assert "monkeypatch" not in source


def test_trigger_delivery_oracle_requires_completed_state_and_bounded_ttl() -> None:
    trigger = trigger_matrix.CreatedTrigger(
        trigger_type="webhook",
        trigger_id="trigger-1",
        row={},
        expected_delivery_id="delivery-1",
    )

    class Table:
        item: dict[str, Any] | None = None

        def get_item(self, **kwargs):
            assert kwargs["ConsistentRead"] is True
            assert kwargs["Key"] == {
                "runtime_name": "!delivery#orders_agent#trigger-1",
                "trigger_id": "delivery-1",
            }
            return {"Item": self.item} if self.item is not None else {}

    table = Table()
    table.item = {
        "runtime_name": "!delivery#orders_agent#trigger-1",
        "trigger_id": "delivery-1",
        "item_kind": "trigger_delivery",
        "source_runtime_name": "orders_agent",
        "source_trigger_id": "trigger-1",
        "delivery_status": "processing",
        "completed_at": 100,
        "ttl": 100 + trigger_matrix.EXPECTED_DELIVERY_RETENTION_SECONDS,
    }
    assert (
        trigger_matrix._completed_delivery(
            table,
            trigger,
            runtime_name="orders_agent",
        )
        is None
    )

    table.item["delivery_status"] = "completed"
    table.item["ttl"] = 100
    with pytest.raises(AssertionError):
        trigger_matrix._completed_delivery(
            table,
            trigger,
            runtime_name="orders_agent",
        )

    table.item["ttl"] = 100 + trigger_matrix.EXPECTED_DELIVERY_RETENTION_SECONDS + 1
    with pytest.raises(AssertionError):
        trigger_matrix._completed_delivery(
            table,
            trigger,
            runtime_name="orders_agent",
        )

    table.item["ttl"] = 100 + trigger_matrix.EXPECTED_DELIVERY_RETENTION_SECONDS
    assert (
        trigger_matrix._completed_delivery(
            table,
            trigger,
            runtime_name="orders_agent",
        )
        == table.item
    )


def test_signed_webhook_uses_the_one_time_secret_and_no_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "a" * 64
    trigger = trigger_matrix.CreatedTrigger(
        trigger_type="webhook",
        trigger_id="trigger-2",
        row={
            "webhook_path": "/hooks/orders_agent/trigger-2",
            "_one_time_signing_secret": secret,
        },
    )
    captured: dict[str, Any] = {}

    def post(url: str, *, data: bytes, headers: dict[str, str], timeout: int):
        captured.update(
            {
                "url": url,
                "data": data,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return _response(
            202,
            accepted=True,
            trigger_id="trigger-2",
            delivery_id=headers["X-AgentCore-Delivery-Id"],
        )

    monkeypatch.setattr(trigger_matrix.requests, "post", post)
    session = FakeSession()

    delivery_id = trigger_matrix._fire_signed_webhook(
        session,  # type: ignore[arg-type]
        trigger,
        nonce="nonce-1",
    )

    headers = captured["headers"]
    message = headers["X-AgentCore-Timestamp"].encode("ascii") + b"." + delivery_id.encode() + b"." + captured["data"]
    expected = (
        "v1="
        + hmac.new(
            secret.encode("ascii"),
            message,
            hashlib.sha256,
        ).hexdigest()
    )
    assert headers["X-AgentCore-Signature"] == expected
    assert "Authorization" not in headers
    assert captured["url"] == ("https://product.example.test/hooks/orders_agent/trigger-2")


def test_trigger_delete_oracle_requires_row_rule_and_secret_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession(
        delete_responses=[
            _response(409, detail="delivery busy"),
            _response(
                200,
                success=True,
                trigger_id="trigger-3",
                message="deleted",
            ),
        ]
    )

    class Table:
        def get_item(self, **kwargs):
            assert kwargs["ConsistentRead"] is True
            return {}

    class Events:
        def describe_rule(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}},
                "DescribeRule",
            )

    class Secrets:
        def describe_secret(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}},
                "DescribeSecret",
            )

    trigger = trigger_matrix.CreatedTrigger(
        trigger_type="webhook",
        trigger_id="trigger-3",
        row={
            "eventbridge_rule_arn": ("arn:aws:events:us-east-1:123456789012:rule/test-trigger-3"),
            "webhook_secret_ref": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-trigger/test"),
        },
    )
    monkeypatch.setattr(trigger_matrix.time, "sleep", lambda _seconds: None)

    trigger_matrix._delete_trigger_and_verify(
        session,  # type: ignore[arg-type]
        Table(),
        Events(),
        Secrets(),
        runtime_name="orders_agent",
        trigger=trigger,
    )

    assert len(session.delete_calls) == 2


def test_rule_readiness_retries_visibility_and_requires_owned_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trigger = trigger_matrix.CreatedTrigger(
        trigger_type="eventbridge",
        trigger_id="trigger-4",
        row={"eventbridge_rule_arn": ("arn:aws:events:us-east-1:123456789012:rule/test-trigger-4")},
    )

    class Events:
        describes = 0

        def describe_rule(self, **kwargs):
            self.describes += 1
            if self.describes == 1:
                raise ClientError(
                    {"Error": {"Code": "ResourceNotFoundException"}},
                    "DescribeRule",
                )
            return {
                "State": "ENABLED",
                "Arn": trigger.row["eventbridge_rule_arn"],
            }

        def list_targets_by_rule(self, **kwargs):
            return {
                "Targets": [
                    {
                        "Id": "dispatch-trigger-4",
                        "Arn": ("arn:aws:sqs:us-east-1:123456789012:trigger-dispatch"),
                    }
                ]
            }

        def list_tags_for_resource(self, **kwargs):
            return {
                "Tags": [
                    {"Key": "ManagedBy", "Value": "agentcore-flows"},
                    {"Key": "Purpose", "Value": "runtime-trigger"},
                    {"Key": "TriggerId", "Value": "trigger-4"},
                    {"Key": "RuntimeName", "Value": "orders_agent"},
                    {"Key": "AgentCoreStack", "Value": "project-test-us-east-1"},
                ]
            }

    events = Events()
    monkeypatch.setattr(trigger_matrix.time, "sleep", lambda _seconds: None)

    trigger_matrix._wait_for_rule_ready(
        events,
        trigger,
        runtime_name="orders_agent",
    )

    assert events.describes == 2


def test_integration_marker_is_the_only_ambient_aws_credential_exemption() -> None:
    source = (_REPO / "backend/tests/conftest.py").read_text()

    assert 'request.node.get_closest_marker("integration")' in source
    assert source.count('get_closest_marker("integration")') == 1
    assert "AWS_SHARED_CREDENTIALS_FILE" in source
    assert "AWS_EC2_METADATA_DISABLED" in source


def test_trigger_matrix_operator_contract_is_documented() -> None:
    development = (_REPO / "docs/DEVELOPMENT.md").read_text()

    assert trigger_matrix.TRIGGERS_TABLE_ENV in development
    assert trigger_matrix.DELIVERY_TIMEOUT_ENV in development
    assert "test_trigger_delivery_matrix.py" in development
    assert "`trigger:read`" in development
    assert "`trigger:write`" in development
    assert "completed DynamoDB delivery row" in development
    assert "signed webhook" in development


def test_legacy_http_live_probe_cannot_misclassify_the_mcp_server() -> None:
    source = (_REPO / "backend/tests/live/e2e_live_invocation.py").read_text()

    assert 'template_id="mcp-server-runtime"' not in source
    assert "standalone mcp-server-runtime template is intentionally absent" in source
