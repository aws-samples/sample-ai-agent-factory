"""Customer-visible tool routing in the exported gallery templates.

The six-template live harness names every tool a customer is promised, but the
CloudFormation generator has its own routing and packaging decisions.  These
tests bind the two together: the emitted Gateway target must advertise the
right tools, and the Lambda source shipped in the same bundle must actually
execute every advertised tool.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml
from app.models.deployment_models import DeployRequest
from app.services.cfn_template_generator import (
    CfnBundle,
    CfnExportUnsupportedError,
    CfnTemplateGenerator,
)
from app.services.deployment import generate_mcp_server_code

from tests.integration import test_template_deployments as matrix

MODEL_ID = "us.anthropic.claude-sonnet-5"
CANONICAL_TOOLS = ("get_order", "get_customer", "list_orders", "process_refund")
LEGACY_TOOLS = (
    "check_order_status",
    "lookup_customer",
    "search_knowledge_base",
    "get_return_policy",
)
WEB_TOOLS = ("duckduckgo_search", "wikipedia_search", "get_weather", "fetch_webpage")

EXPECTED_ROUTES = {
    "strands-gateway-agent": ("DynamicTools", WEB_TOOLS + CANONICAL_TOOLS),
    "customer-support-assistant": ("CustomerSupportTools", LEGACY_TOOLS),
    "customer-support-blueprint": ("DynamicTools", CANONICAL_TOOLS),
}


def _request(template_id: str) -> DeployRequest:
    return DeployRequest.model_validate(
        {
            "nodeId": "routing-contract-node",
            "templateId": template_id,
            "config": {
                "name": f"{template_id}-routing-contract",
                "protocol": "HTTP",
                "model": {"modelId": MODEL_ID},
            },
            "gatewayConfig": {
                "name": "routing-contract-gateway",
                "gateway_provider": "agentcore",
                "targetType": "lambda",
            },
            "gatewayTools": list(CANONICAL_TOOLS) if template_id == "customer-support-blueprint" else [],
        }
    )


def _export(template_id: str) -> tuple[CfnBundle, dict[str, Any]]:
    bundle = CfnTemplateGenerator().generate(_request(template_id))
    return bundle, yaml.safe_load(bundle.template_yaml)


def _lambda_target(template: dict[str, Any]) -> tuple[str, tuple[str, ...], str]:
    resources = template["Resources"]
    matches: list[tuple[str, tuple[str, ...], str]] = []
    for resource in resources.values():
        if resource.get("Type") != "AWS::BedrockAgentCore::GatewayTarget":
            continue
        properties = resource["Properties"]
        lambda_target = properties.get("TargetConfiguration", {}).get("Mcp", {}).get("Lambda")
        if not lambda_target:
            continue
        schemas = lambda_target["ToolSchema"]["InlinePayload"]
        lambda_logical_id = lambda_target["LambdaArn"]["Fn::GetAtt"][0]
        matches.append(
            (
                properties["Name"],
                tuple(schema["Name"] for schema in schemas),
                resources[lambda_logical_id]["Properties"]["Handler"],
            )
        )

    assert len(matches) == 1, f"expected one built-in Lambda target, got {matches!r}"
    return matches[0]


@pytest.mark.parametrize("template_id", EXPECTED_ROUTES)
def test_exported_gallery_target_advertises_exactly_its_intended_tools(
    template_id: str,
) -> None:
    _bundle, template = _export(template_id)
    target_name, tool_names, _handler = _lambda_target(template)
    expected_target, expected_tools = EXPECTED_ROUTES[template_id]

    assert target_name == expected_target
    assert set(tool_names) == set(expected_tools)
    assert len(tool_names) == len(expected_tools)


def test_export_refuses_an_unknown_tool_instead_of_silently_dropping_it() -> None:
    request = DeployRequest.model_validate(
        {
            "nodeId": "unknown-routing-contract-node",
            "config": {
                "name": "unknown-routing-contract",
                "protocol": "HTTP",
                "model": {"modelId": MODEL_ID},
            },
            "gatewayConfig": {
                "name": "unknown-routing-contract-gateway",
                "gateway_provider": "agentcore",
                "targetType": "lambda",
            },
            "gatewayTools": ["get_order", "not_a_real_gateway_tool"],
        }
    )

    with pytest.raises(
        CfnExportUnsupportedError,
        match="not_a_real_gateway_tool",
    ):
        CfnTemplateGenerator().generate(request)


def _load_packaged_handler(
    bundle: CfnBundle,
    handler_reference: str,
) -> tuple[dict[str, Any], Callable[[dict[str, Any], Any], dict[str, Any]]]:
    assert bundle.tool_lambda_code is not None
    module_name, function_name = handler_reference.rsplit(".", 1)
    path = f"{module_name.replace('.', '/')}.py"
    with zipfile.ZipFile(io.BytesIO(bundle.tool_lambda_code)) as archive:
        source = archive.read(path).decode("utf-8")

    namespace: dict[str, Any] = {"__name__": f"routing_contract_{module_name}"}
    exec(compile(source, path, "exec"), namespace)
    return namespace, namespace[function_name]


def _decoded_body(response: dict[str, Any]) -> Any:
    value = response["body"]
    for _ in range(3):
        if not isinstance(value, str):
            break
        value = json.loads(value)
    return value


def _context(target_name: str, tool_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": f"{target_name}___{tool_name}"})
    )


TOOL_CASES: dict[str, tuple[dict[str, Any], Callable[[Any], bool]]] = {
    "get_order": (
        {"order_id": "ORD-12345"},
        matrix._is_order_payload,
    ),
    "get_customer": (
        {"customer_id": "CUST-001"},
        matrix._is_customer_payload,
    ),
    "list_orders": (
        {"customer_id": "CUST-001", "limit": 10},
        matrix._is_order_list_payload,
    ),
    "process_refund": (
        {
            "order_id": "ORD-12345",
            "amount": 10,
            "reason": "integration verification",
        },
        matrix._is_refund_payload,
    ),
    "check_order_status": (
        {"order_id": "ORD-12345"},
        matrix._is_legacy_order_status_payload,
    ),
    "lookup_customer": (
        {"email": "john@example.com"},
        matrix._is_legacy_customer_payload,
    ),
    "search_knowledge_base": (
        {"query": "refund"},
        matrix._is_legacy_kb_payload,
    ),
    "get_return_policy": (
        {"product_category": "Electronics"},
        matrix._is_legacy_return_policy_payload,
    ),
}


@pytest.mark.parametrize("template_id", EXPECTED_ROUTES)
def test_exported_gallery_lambda_executes_every_intended_tool(
    template_id: str,
) -> None:
    bundle, template = _export(template_id)
    target_name, _advertised_tools, handler_reference = _lambda_target(template)
    _expected_target, expected_tools = EXPECTED_ROUTES[template_id]
    namespace, handler = _load_packaged_handler(bundle, handler_reference)

    web_probes = {
        "duckduckgo_search": ("_do_duckduckgo_search", {"query": "AgentCore"}),
        "wikipedia_search": ("_do_wikipedia_search", {"query": "Python"}),
        "get_weather": ("_do_weather", {"location": "Dublin"}),
        "fetch_webpage": ("_do_fetch_webpage", {"url": "https://example.com"}),
    }
    for tool_name, (implementation_name, _event) in web_probes.items():
        namespace[implementation_name] = lambda *args, _tool_name=tool_name: json.dumps(
            {"routing_probe": _tool_name, "arguments": list(args)}
        )

    for tool_name in expected_tools:
        if tool_name in web_probes:
            _implementation_name, event = web_probes[tool_name]
            actual = _decoded_body(handler(event, _context(target_name, tool_name)))
            assert actual["routing_probe"] == tool_name
            continue

        event, oracle = TOOL_CASES[tool_name]
        actual = _decoded_body(handler(event, _context(target_name, tool_name)))
        assert oracle(actual), f"{template_id}/{tool_name} returned {actual!r}"


class _FakeFastMCP:
    instances: list[_FakeFastMCP] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.tools: dict[str, Callable[..., str]] = {}
        self.instances.append(self)

    def tool(self) -> Callable[[Callable[..., str]], Callable[..., str]]:
        def register(function: Callable[..., str]) -> Callable[..., str]:
            self.tools[function.__name__] = function
            return function

        return register

    def run(self, *, transport: str) -> None:
        raise AssertionError(f"generated server unexpectedly started with {transport=}")


def _install_fake_fastmcp(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_module = ModuleType("mcp")
    mcp_module.__path__ = []  # type: ignore[attr-defined]
    server_module = ModuleType("mcp.server")
    server_module.__path__ = []  # type: ignore[attr-defined]
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    mcp_module.server = server_module  # type: ignore[attr-defined]
    server_module.fastmcp = fastmcp_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)


def test_hosted_mcp_server_executes_the_same_canonical_order_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeFastMCP.instances.clear()
    _install_fake_fastmcp(monkeypatch)
    server_name = 'ECB "support" server\nprimary'
    instructions = 'Use "approved" support actions only.\\nDo not improvise.'
    source = generate_mcp_server_code(
        server_name=server_name,
        tools=list(CANONICAL_TOOLS),
        system_prompt=instructions,
    )

    namespace: dict[str, Any] = {"__name__": "generated_hosted_mcp"}
    exec(compile(source, "generated_hosted_mcp.py", "exec"), namespace)

    assert len(_FakeFastMCP.instances) == 1
    server = _FakeFastMCP.instances[0]
    assert server.kwargs == {
        "name": server_name,
        "instructions": instructions,
        "host": "0.0.0.0",
        "port": 8000,
        "stateless_http": True,
    }
    assert set(server.tools) == set(CANONICAL_TOOLS)

    for tool_name in CANONICAL_TOOLS:
        event, oracle = TOOL_CASES[tool_name]
        actual = json.loads(server.tools[tool_name](**event))
        assert oracle(actual), f"hosted MCP {tool_name} returned {actual!r}"
