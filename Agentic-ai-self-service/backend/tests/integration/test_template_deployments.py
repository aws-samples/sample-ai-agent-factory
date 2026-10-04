"""Authenticated deploy/invoke/delete matrix for all six gallery templates.

Every case follows the user-visible product path:

1. ``POST /api/deploy`` with the template's real protocol and composition.
2. Poll the deployment record to a successful terminal state.
3. Exercise a template-specific tool oracle (not merely a non-empty response).
4. Delete through the product API and require a durable ``deleted`` verdict.

The standalone MCP template additionally discovers its exact tool catalog,
calls every advertised tool through the product MCP explorer, checks an unknown
tool fails closed, and proves the HTTP chat and trigger-creation endpoints
refuse the MCP runtime without leaving a trigger row.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

import pytest
import requests
from app.services.runtime_mcp import MCP_PROTOCOL_VERSION

from tests.integration.conftest import (
    DeploymentCleanupTracker,
    TrackedDeployment,
)

JsonOracle = Callable[[Any], bool]


@dataclass(frozen=True)
class ToolInvocation:
    tool_name: str
    prompt: str
    response_label: str
    response_oracle: JsonOracle


@dataclass(frozen=True)
class TemplateCase:
    template_id: str
    name_token: str
    protocol: Literal["HTTP", "MCP"]
    invocations: tuple[ToolInvocation, ...] = ()
    expected_status_fields: tuple[str, ...] = ()
    verify_memory_recall: bool = False
    mcp_gateway_target: bool = False


def _is_web_search_payload(value: Any) -> bool:
    return bool(
        isinstance(value, list)
        and value
        and any(
            isinstance(item, dict)
            and isinstance(item.get("title"), str)
            and item.get("title")
            and isinstance(item.get("url"), str)
            and item.get("url", "").startswith("http")
            for item in value
        )
    )


def _is_wikipedia_payload(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and isinstance(value.get("title"), str)
        and value.get("title")
        and isinstance(value.get("summary"), str)
        and value.get("summary")
        and isinstance(value.get("url"), str)
        and value.get("url", "").startswith("http")
    )


def _is_weather_payload(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and "dublin" in str(value.get("location", "")).lower()
        and isinstance(value.get("description"), str)
        and value.get("description")
        and isinstance(value.get("temperature_F"), int | float)
        and isinstance(value.get("humidity_pct"), int | float)
        and isinstance(value.get("wind_mph"), int | float)
    )


def _is_fetched_page_payload(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("url") == "https://example.com"
        and "example domain" in str(value.get("content", "")).lower()
    )


def _is_order_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    items = value.get("items")
    return bool(
        value.get("order_id") == "ORD-12345"
        and value.get("customer_id") == "CUST-001"
        and value.get("status") == "delivered"
        and value.get("total") == 79.99
        and isinstance(items, list)
        and any(
            isinstance(item, dict) and item.get("name") == "Wireless Headphones" and item.get("quantity") == 1
            for item in items
        )
    )


def _is_customer_payload(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("customer_id") == "CUST-001"
        and value.get("name") == "John Doe"
        and value.get("email") == "john@example.com"
        and value.get("total_orders") == 3
        and value.get("total_spent") == 354.97
    )


def _is_order_list_payload(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("customer_id") != "CUST-001":
        return False
    orders = value.get("orders")
    if not isinstance(orders, list):
        return False
    ids = {item.get("order_id") for item in orders if isinstance(item, dict)}
    return {"ORD-12345", "ORD-12300", "ORD-12400"} <= ids


def _is_refund_payload(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("success") is True
        and value.get("order_id") == "ORD-12345"
        and value.get("amount") == 10
        and value.get("reason") == "integration verification"
        and value.get("status") == "processed"
        and re.fullmatch(r"REF-[0-9A-F]{5}", str(value.get("refund_id", "")))
    )


def _is_legacy_order_status_payload(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("order_id") == "ORD-12345"
        and value.get("status") == "Shipped"
        and value.get("tracking_number") == "1Z999AA10123456784"
        and value.get("total") == "$1,348.99"
    )


def _is_legacy_customer_payload(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("customer_id") == "CUST-001"
        and value.get("name") == "John Smith"
        and value.get("email") == "john@example.com"
        and value.get("membership_tier") == "Gold"
    )


def _is_legacy_kb_payload(value: Any) -> bool:
    results = value.get("results") if isinstance(value, dict) else None
    return bool(
        isinstance(results, list)
        and value.get("total_found") == len(results)
        and any(
            isinstance(item, dict) and item.get("id") == "KB-002" and "refund" in str(item.get("title", "")).lower()
            for item in results
        )
    )


def _is_legacy_return_policy_payload(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("category") == "Electronics"
        and value.get("return_window") == "30 days"
        and value.get("condition") == "Must be in original packaging"
    )


def _tool_call(
    tool_name: str,
    arguments: str,
    response_label: str,
    response_oracle: JsonOracle,
) -> ToolInvocation:
    return ToolInvocation(
        tool_name=tool_name,
        prompt=(
            f"Call {tool_name} with {arguments}. Return only the exact JSON value "
            "returned by the tool, with no commentary or markdown."
        ),
        response_label=response_label,
        response_oracle=response_oracle,
    )


_CANONICAL_SUPPORT_INVOCATIONS = (
    _tool_call(
        "get_order",
        "order_id ORD-12345",
        "canonical get_order tool",
        _is_order_payload,
    ),
    _tool_call(
        "get_customer",
        "customer_id CUST-001",
        "canonical get_customer tool",
        _is_customer_payload,
    ),
    _tool_call(
        "list_orders",
        "customer_id CUST-001 and limit 10",
        "canonical list_orders tool",
        _is_order_list_payload,
    ),
    _tool_call(
        "process_refund",
        "order_id ORD-12345, amount 10, and reason integration verification",
        "canonical process_refund tool",
        _is_refund_payload,
    ),
)

_DYNAMIC_WEB_INVOCATIONS = (
    _tool_call(
        "duckduckgo_search",
        "query Amazon Bedrock AgentCore",
        "DuckDuckGo search tool",
        _is_web_search_payload,
    ),
    _tool_call(
        "wikipedia_search",
        "query Python programming language",
        "Wikipedia search tool",
        _is_wikipedia_payload,
    ),
    _tool_call(
        "get_weather",
        "location Dublin",
        "Open-Meteo weather tool",
        _is_weather_payload,
    ),
    _tool_call(
        "fetch_webpage",
        "url https://example.com",
        "web page fetcher tool",
        _is_fetched_page_payload,
    ),
)

_LEGACY_SUPPORT_INVOCATIONS = (
    _tool_call(
        "check_order_status",
        "order_id ORD-12345",
        "legacy check_order_status tool",
        _is_legacy_order_status_payload,
    ),
    _tool_call(
        "lookup_customer",
        "email john@example.com",
        "legacy lookup_customer tool",
        _is_legacy_customer_payload,
    ),
    _tool_call(
        "search_knowledge_base",
        "query refund",
        "legacy search_knowledge_base tool",
        _is_legacy_kb_payload,
    ),
    _tool_call(
        "get_return_policy",
        "product_category electronics",
        "legacy get_return_policy tool",
        _is_legacy_return_policy_payload,
    ),
)


TEMPLATE_CASES: tuple[TemplateCase, ...] = (
    TemplateCase(
        template_id="web-search-agent",
        name_token="web",
        protocol="HTTP",
        invocations=(
            _DYNAMIC_WEB_INVOCATIONS[0],
            _DYNAMIC_WEB_INVOCATIONS[2],
            _DYNAMIC_WEB_INVOCATIONS[3],
        ),
    ),
    TemplateCase(
        template_id="strands-gateway-agent",
        name_token="gateway",
        protocol="HTTP",
        invocations=(*_DYNAMIC_WEB_INVOCATIONS, *_CANONICAL_SUPPORT_INVOCATIONS),
        expected_status_fields=("gateway_url",),
    ),
    TemplateCase(
        template_id="customer-support-assistant",
        name_token="support",
        protocol="HTTP",
        invocations=_LEGACY_SUPPORT_INVOCATIONS,
        expected_status_fields=("gateway_url", "memory_result"),
        verify_memory_recall=True,
    ),
    TemplateCase(
        template_id="customer-support-blueprint",
        name_token="blueprint",
        protocol="HTTP",
        invocations=_CANONICAL_SUPPORT_INVOCATIONS,
        expected_status_fields=("gateway_url", "memory_result"),
        verify_memory_recall=True,
    ),
    TemplateCase(
        template_id="mcp-server-gateway-target",
        name_token="mcpchain",
        protocol="HTTP",
        invocations=_CANONICAL_SUPPORT_INVOCATIONS,
        expected_status_fields=(
            "gateway_url",
            "mcp_server_runtime_id",
        ),
        mcp_gateway_target=True,
    ),
    TemplateCase(
        template_id="mcp-server-runtime",
        name_token="mcpsolo",
        protocol="MCP",
    ),
)


_WEB_SEARCH_PROMPT = (
    "You are a helpful web search assistant. Use the DuckDuckGo search tool to find "
    "relevant URLs, then use the fetch_webpage tool to retrieve the actual page "
    "content for up-to-date information. Always fetch page content rather than "
    "relying on search snippets alone. Cite your sources."
)
_STRANDS_GATEWAY_PROMPT = (
    "You are a helpful assistant with access to tools through the MCP Gateway. "
    "Use available tools to answer user questions. Be precise and helpful."
)
_SUPPORT_ASSISTANT_PROMPT = """You are a customer support assistant. You have access to tools through the MCP Gateway to look up customer information, order status, and knowledge base articles.

Guidelines:
- Always greet the customer warmly
- Use available tools to look up relevant information before answering
- If you cannot find the answer, escalate to a human agent
- Keep responses concise and helpful
- Remember context from previous messages in the conversation"""
_SUPPORT_BLUEPRINT_PROMPT = """You are a customer support agent. Your role is to answer customer questions about orders, account information, and refund requests.

Guidelines:
- Use the customer's ID to look up their account and orders automatically
- When showing orders, always fetch full order details (get_order) to include item names, quantities, and prices
- Summarize information clearly and concisely for the customer
- For refunds, validate the order exists and the amount before processing

Demo customers: CUST-001 (John Doe), CUST-002 (Jane Smith)"""
_MCP_GATEWAY_AGENT_PROMPT = (
    "You are a helpful assistant with access to order management tools through the "
    "MCP Gateway. Use the available tools to help users look up orders, customers, "
    "and process refunds."
)
_MCP_SERVER_PROMPT = (
    "MCP Server that exposes order management tools: get_order, get_customer, list_orders, and process_refund."
)

_SYSTEM_PROMPTS = {
    "web-search-agent": _WEB_SEARCH_PROMPT,
    "strands-gateway-agent": _STRANDS_GATEWAY_PROMPT,
    "customer-support-assistant": _SUPPORT_ASSISTANT_PROMPT,
    "customer-support-blueprint": _SUPPORT_BLUEPRINT_PROMPT,
    "mcp-server-gateway-target": _MCP_GATEWAY_AGENT_PROMPT,
}


def _model(model_id: str) -> dict[str, Any]:
    return {
        "provider": "anthropic",
        "modelId": model_id,
        "temperature": 0.7,
        "topP": 0.9,
    }


def _runtime_config(
    case: TemplateCase,
    name: str,
) -> dict[str, Any]:
    operational = {
        "name": name,
        "entrypoint": "agent.py",
        "deploymentType": "direct_code_deploy",
        "pythonRuntime": "PYTHON_3_13",
        "protocol": case.protocol,
        "idleTimeout": 300,
        "maxLifetime": 3600,
        # The standalone MCP admission contract refuses generic agent OTEL, but
        # an explicit false is an operational opt-out rather than an OTEL
        # configuration. Keep it on every case so the live matrix never inherits
        # an accidental client-side enablement.
        "enableOtel": False,
    }
    if case.protocol == "MCP":
        # Match the production frontend's runtimeRequestConfig boundary exactly:
        # a standalone tool server has no conversational model loop, so none of
        # its model-only defaults may be serialized. The API intentionally
        # rejects their *presence*, even if they hold their ordinary defaults,
        # because accepting and silently dropping them would make the live
        # matrix prove a different contract from the UI.
        return operational

    default_model = (
        "us.anthropic.claude-sonnet-5"
        if case.template_id == "customer-support-blueprint"
        else "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    model_id = os.environ.get(
        "INTEGRATION_SONNET_MODEL_ID"
        if case.template_id == "customer-support-blueprint"
        else "INTEGRATION_HAIKU_MODEL_ID",
        os.environ.get("INTEGRATION_MODEL_ID", default_model),
    )
    return {
        **operational,
        "framework": "strands_agents",
        "model": _model(model_id),
        "modelProvider": "bedrock",
        "systemPrompt": _SYSTEM_PROMPTS[case.template_id],
        "multiAgentPattern": "none",
    }


def _gateway_config(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "targetType": "lambda",
        "targetConfig": {"type": "lambda"},
        "enableSemanticSearch": True,
        # DeployPanel.mapGatewayDeployTargets always writes the transformed
        # non-MCP target list back into gatewayConfig, even when it is empty.
        "targets": [],
    }


def _shared_cognito_identity() -> dict[str, Any]:
    return {
        "mode": "shared",
        "provider": "cognito",
        "clientId": "",
        "clientSecretRef": "",
        "discoveryUrl": "",
        "scopes": [],
    }


def _template_request_payload(
    case: TemplateCase,
    token: str,
) -> dict[str, Any]:
    runtime_name = f"it_{case.name_token}_{token}"
    payload: dict[str, Any] = {
        "deploymentMode": "runtime",
        "nodeId": f"it-{case.name_token}-{token}",
        "config": _runtime_config(case, runtime_name),
        "connectedTools": [],
        "gatewayConfig": None,
        "gatewayTools": [],
        "templateId": case.template_id,
    }

    if case.template_id == "strands-gateway-agent":
        payload.update(
            {
                "connectedTools": ["gateway", "identity"],
                "gatewayConfig": _gateway_config("agent_gateway"),
                "identityConfig": _shared_cognito_identity(),
            }
        )
    elif case.template_id == "customer-support-assistant":
        payload.update(
            {
                "connectedTools": [
                    "gateway",
                    "identity",
                    "memory",
                    "observability",
                ],
                "gatewayConfig": _gateway_config("support_gateway"),
                "identityConfig": _shared_cognito_identity(),
                "memoryConfig": {
                    "name": "support_memory",
                    "enabled": True,
                    # Mirrors frontend/src/data/templates.ts: the gallery promises persistent
                    # memory, and without a strategy the platform creates a short-term-only Memory.
                    "strategies": [
                        {
                            "type": "semantic",
                            "name": "support_semantic",
                            "description": "Long-term facts about the customer and their orders, recalled across sessions",
                        }
                    ],
                },
                "observabilityConfig": {
                    "name": "support_observability",
                    "enableOtel": False,
                },
            }
        )
    elif case.template_id == "customer-support-blueprint":
        payload.update(
            {
                "connectedTools": ["gateway", "memory"],
                "gatewayConfig": _gateway_config("support_gateway"),
                "gatewayTools": [
                    "get_order",
                    "get_customer",
                    "list_orders",
                    "process_refund",
                ],
                "memoryConfig": {
                    "name": "support_memory",
                    "enabled": True,
                    # Mirrors frontend/src/data/templates.ts: the gallery promises persistent
                    # memory, and without a strategy the platform creates a short-term-only Memory.
                    "strategies": [
                        {
                            "type": "semantic",
                            "name": "support_semantic",
                            "description": "Long-term facts about the customer and their orders, recalled across sessions",
                        }
                    ],
                },
            }
        )
    elif case.mcp_gateway_target:
        payload.update(
            {
                "connectedTools": ["gateway"],
                "gatewayConfig": _gateway_config("mcp_server_gateway"),
            }
        )
        # This is the second runtime node as DeployPanel serializes it. The
        # hosted MCP path deliberately defaults an empty tools array to its four
        # canonical order tools; the matrix must prove that frontend fallback.
        payload["mcpServerConfig"] = {
            "name": "mcp_server_agent",
            "framework": "strands_agents",
            "systemPrompt": _MCP_SERVER_PROMPT,
            "model": _model(
                os.environ.get(
                    "INTEGRATION_HAIKU_MODEL_ID",
                    os.environ.get(
                        "INTEGRATION_MODEL_ID",
                        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                    ),
                )
            ),
            "tools": [],
        }
    return payload


def _deploy_template(
    api_session,
    deployment_cleanup: DeploymentCleanupTracker,
    case: TemplateCase,
) -> tuple[TrackedDeployment, dict[str, Any]]:
    # This token is also embedded in nodeId, which is the recovery key when the
    # server accepts the deployment but the 202 response is lost. Use 64 bits
    # rather than the old 32-bit suffix so a week-long parallel live campaign
    # cannot plausibly collide with an earlier caller-owned row.
    token = uuid.uuid4().hex[:16]
    payload = _template_request_payload(case, token)
    node_id = payload["nodeId"]

    try:
        response = api_session.post(
            f"{api_session.base_url}/api/deploy",
            json=payload,
            timeout=60,
        )
    except requests.RequestException:
        # A timeout/reset does not tell us whether API Gateway delivered the
        # request. Recover and register any accepted row before preserving the
        # original failure; cleanup must not depend on receiving the 202 body.
        deployment_cleanup.recover_by_node_id(node_id)
        raise

    if response.status_code >= 500:
        # A handler can fail after persisting the row. This response remains a
        # test failure, but first recover any side effect so fixture teardown
        # has an exact deployment id.
        deployment_cleanup.recover_by_node_id(node_id)
    assert response.status_code == 202, (
        f"{case.template_id}: POST /api/deploy returned {response.status_code}: {response.text}"
    )

    try:
        body = response.json()
    except ValueError:
        deployment_cleanup.recover_by_node_id(node_id)
        raise
    if not isinstance(body, dict):
        deployment_cleanup.recover_by_node_id(node_id)
        raise AssertionError(f"{case.template_id}: POST /api/deploy returned a non-object body")

    deployment_id = body.get("deploymentId")
    if not isinstance(deployment_id, str) or not deployment_id:
        deployment_cleanup.recover_by_node_id(node_id)
    assert isinstance(deployment_id, str) and deployment_id

    # Track before asserting any other response field. A malformed success body
    # is still a product failure, but it must never suppress teardown.
    record = deployment_cleanup.track(deployment_id)
    assert body.get("status") == "pending"
    return record, payload


def _json_values(text: str) -> Iterable[Any]:
    """Yield JSON values from plain, fenced, or lightly-prefixed model output."""

    decoder = json.JSONDecoder()
    pending = [
        text.strip(),
        *(
            match.group(1).strip()
            for match in re.finditer(
                r"```(?:json)?\s*(.*?)```",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
        ),
    ]
    seen: set[str] = set()
    while pending:
        candidate = pending.pop()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            value = None
        else:
            yield value
            if isinstance(value, str):
                pending.append(value.strip())

        for index, character in enumerate(candidate):
            if character not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            yield value
            if isinstance(value, str):
                pending.append(value.strip())


def _require_json_oracle(
    text: str,
    *,
    oracle: JsonOracle,
    label: str,
) -> Any:
    values = list(_json_values(text))
    for value in values:
        error = _structured_error(value)
        if error:
            raise AssertionError(
                f"{label} returned a structured tool error ({error}). Actual response: {text[:1200]!r}"
            )
    for value in values:
        if oracle(value):
            return value
    raise AssertionError(
        f"{label} did not return the required structured tool result. Actual response: {text[:1200]!r}"
    )


def _structured_error(value: Any) -> str | None:
    if isinstance(value, dict):
        error = value.get("error")
        if error not in (None, "", False):
            return str(error)
        if value.get("success") is False:
            return "success=false"
        for nested in value.values():
            nested_error = _structured_error(nested)
            if nested_error:
                return nested_error
    elif isinstance(value, list):
        for nested in value:
            nested_error = _structured_error(nested)
            if nested_error:
                return nested_error
    return None


def _invoke_http(
    api_session,
    *,
    case: TemplateCase,
    status: dict[str, Any],
    prompt: str,
    session_id: str,
    label: str,
) -> str:
    response = api_session.post(
        f"{api_session.base_url}/api/test-runtime",
        json={
            "endpoint": status["runtime_endpoint"],
            "input": prompt,
            "runtimeId": status["runtime_id"],
            "sessionId": session_id,
        },
        timeout=180,
    )
    response.raise_for_status()
    body = response.json()
    assert body.get("success") is True, f"{case.template_id}: {label} invocation failed: {body.get('error') or body}"
    returned_session = body.get("sessionId")
    if returned_session is not None:
        assert returned_session == session_id, (
            f"{case.template_id}: {label} changed session {session_id!r} to {returned_session!r}"
        )
    text = body.get("response")
    assert isinstance(text, str) and text.strip(), f"{case.template_id}: {label} returned no response text"
    return text


def _invoke_http_template(
    api_session,
    *,
    case: TemplateCase,
    status: dict[str, Any],
) -> None:
    assert case.invocations
    session_id = f"integration-{uuid.uuid4().hex}"

    for invocation in case.invocations:
        text = _invoke_http(
            api_session,
            case=case,
            status=status,
            prompt=invocation.prompt,
            session_id=session_id,
            label=invocation.response_label,
        )
        _require_json_oracle(
            text,
            oracle=invocation.response_oracle,
            label=invocation.response_label,
        )

    if not case.verify_memory_recall:
        return

    nonce = f"MEM-{uuid.uuid4().hex}"
    _invoke_http(
        api_session,
        case=case,
        status=status,
        prompt=(f"Remember this verification nonce exactly for this conversation: {nonce}. Reply only STORED."),
        session_id=session_id,
        label="memory write",
    )
    recall = _invoke_http(
        api_session,
        case=case,
        status=status,
        prompt=(
            "What exact verification nonce did I ask you to remember in this conversation? Return only that nonce."
        ),
        session_id=session_id,
        label="memory recall",
    )
    recall_errors = [error for value in _json_values(recall) if (error := _structured_error(value))]
    assert not recall_errors, (
        f"{case.template_id}: memory recall returned structured errors {recall_errors}: {recall[:1200]!r}"
    )
    assert nonce in recall, (
        f"{case.template_id}: same-session memory did not recall {nonce!r}. Actual response: {recall[:1200]!r}"
    )


def _mcp_text(body: dict[str, Any]) -> str:
    content = body.get("content")
    assert isinstance(content, list) and content
    text = "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )
    assert text.strip(), f"MCP result has no text content: {body}"
    return text


def _call_mcp_tool(
    api_session,
    *,
    deployment_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    session_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "deploymentId": deployment_id,
        "toolName": tool_name,
        "arguments": arguments,
    }
    if session_id:
        payload["sessionId"] = session_id
    response = api_session.post(
        f"{api_session.base_url}/api/test-mcp-runtime/call",
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    assert body.get("protocolVersion") == MCP_PROTOCOL_VERSION
    assert body.get("isError") is False, body
    _mcp_text(body)
    return body


def _invoke_standalone_mcp(
    api_session,
    *,
    deployment_id: str,
    runtime_name: str,
    status: dict[str, Any],
) -> None:
    discovery_response = api_session.post(
        f"{api_session.base_url}/api/test-mcp-runtime/tools",
        json={"deploymentId": deployment_id},
        timeout=60,
    )
    discovery_response.raise_for_status()
    discovery = discovery_response.json()
    assert discovery.get("protocolVersion") == MCP_PROTOCOL_VERSION
    assert {tool.get("name") for tool in discovery.get("tools", []) if isinstance(tool, dict)} == {
        "get_weather",
        "search_web",
        "fetch_url",
    }
    assert all(
        isinstance(tool.get("inputSchema"), dict) and tool["inputSchema"].get("type") == "object"
        for tool in discovery["tools"]
    )
    server_info = discovery.get("serverInfo")
    assert isinstance(server_info, dict)
    assert server_info.get("name") and server_info.get("version")
    session_id = discovery.get("sessionId")

    weather = _call_mcp_tool(
        api_session,
        deployment_id=deployment_id,
        tool_name="get_weather",
        arguments={"city": "Dublin"},
        session_id=session_id,
    )
    _require_json_oracle(
        _mcp_text(weather),
        oracle=_is_weather_payload,
        label="standalone MCP get_weather",
    )

    search = _call_mcp_tool(
        api_session,
        deployment_id=deployment_id,
        tool_name="search_web",
        arguments={"query": "Python programming language"},
        session_id=session_id,
    )
    _require_json_oracle(
        _mcp_text(search),
        oracle=lambda value: bool(
            isinstance(value, list)
            and value
            and any(isinstance(item, dict) and isinstance(item.get("url"), str) and item.get("url") for item in value)
        ),
        label="standalone MCP search_web",
    )

    fetched = _call_mcp_tool(
        api_session,
        deployment_id=deployment_id,
        tool_name="fetch_url",
        arguments={"url": "https://example.com"},
        session_id=session_id,
    )
    _require_json_oracle(
        _mcp_text(fetched),
        oracle=lambda value: bool(
            isinstance(value, dict)
            and value.get("url") == "https://example.com"
            and "example domain" in str(value.get("content", "")).lower()
        ),
        label="standalone MCP fetch_url",
    )

    # Unknown tools must be a bounded protocol error, never a crash/5xx. MCP
    # servers may express this as JSON-RPC error (mapped to 422) or a successful
    # JSON-RPC envelope with isError=true.
    unknown = api_session.post(
        f"{api_session.base_url}/api/test-mcp-runtime/call",
        json={
            "deploymentId": deployment_id,
            "toolName": "tool_that_does_not_exist",
            "arguments": {},
            **({"sessionId": session_id} if session_id else {}),
        },
        timeout=60,
    )
    assert unknown.status_code in {200, 422}, unknown.text
    unknown_body = unknown.json()
    if unknown.status_code == 200:
        assert unknown_body.get("isError") is True
        _mcp_text(unknown_body)
    else:
        assert unknown_body.get("detail") == ("The MCP runtime rejected this operation.")

    # The old conversational endpoint must refuse this protocol explicitly
    # instead of forwarding a prompt to an MCP server and surfacing its 406.
    wrong_endpoint = api_session.post(
        f"{api_session.base_url}/api/test-runtime",
        json={
            "endpoint": status["runtime_endpoint"],
            "input": "hello",
            "runtimeId": status["runtime_id"],
        },
        timeout=60,
    )
    assert wrong_endpoint.status_code == 409, wrong_endpoint.text
    detail = str(wrong_endpoint.json().get("detail", "")).lower()
    assert "mcp" in detail and "test-mcp-runtime" in detail

    # Trigger dispatch speaks the HTTP agent request/response envelope. Refuse
    # an MCP runtime before Secrets Manager, DynamoDB, or EventBridge side
    # effects rather than registering a trigger that can only fail at delivery.
    trigger_create = api_session.post(
        f"{api_session.base_url}/api/runtimes/{runtime_name}/triggers",
        json={
            "type": "cron",
            "schedule": "cron(0 12 * * ? *)",
        },
        timeout=60,
    )
    assert trigger_create.status_code == 409, trigger_create.text
    trigger_detail = str(trigger_create.json().get("detail", "")).lower()
    assert "mcp" in trigger_detail and "trigger" in trigger_detail

    # The refusal must happen before the trigger row is written. Keeping list
    # available also proves existing rows would remain manageable for cleanup.
    trigger_list = api_session.get(
        f"{api_session.base_url}/api/runtimes/{runtime_name}/triggers",
        timeout=60,
    )
    trigger_list.raise_for_status()
    assert trigger_list.json() == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "case",
    TEMPLATE_CASES,
    ids=[case.template_id for case in TEMPLATE_CASES],
)
def test_gallery_template_deploy_invoke_and_verified_cleanup(
    case: TemplateCase,
    api_session,
    deployment_cleanup: DeploymentCleanupTracker,
    wait_for_deployment,
) -> None:
    record, request_payload = _deploy_template(
        api_session,
        deployment_cleanup,
        case,
    )

    status = wait_for_deployment(record.deployment_id)
    record.last_status = status
    assert status.get("status") == "succeeded", (
        f"{case.template_id} deployment failed: {status.get('error_details') or status}"
    )
    assert status.get("runtime_protocol") == case.protocol
    assert isinstance(status.get("runtime_id"), str) and status["runtime_id"]
    assert isinstance(status.get("runtime_endpoint"), str) and status["runtime_endpoint"]
    assert isinstance(status.get("runtime_arn"), str) and status["runtime_arn"]
    assert isinstance(status.get("created_resources"), list)
    assert status["created_resources"], f"{case.template_id} succeeded without a teardown manifest"
    for field_name in case.expected_status_fields:
        assert status.get(field_name), f"{case.template_id} did not persist its advertised {field_name}: {status}"

    deployment_cleanup.bind_runtime(record, status["runtime_id"])

    if case.protocol == "MCP":
        assert request_payload["config"]["protocol"] == "MCP"
        _invoke_standalone_mcp(
            api_session,
            deployment_id=record.deployment_id,
            runtime_name=request_payload["config"]["name"],
            status=status,
        )
    else:
        _invoke_http_template(
            api_session,
            case=case,
            status=status,
        )

    tombstone = deployment_cleanup.delete_and_verify(record)
    assert tombstone.get("delete_status") == "deleted"
