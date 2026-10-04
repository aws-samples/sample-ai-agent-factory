"""Connected components must compose or be refused; they may never disappear.

The visual builder permits one Runtime to connect to A2A, Memory, Gateway,
Browser, Code Interpreter, and Knowledge Base components. Code generation is a
priority-ordered chain of early returns, however, so a dominant branch can
produce a valid agent while silently omitting another connected component. The
deployment still creates and bills for the omitted resource and reports
success.

This contract is deliberately implementation-neutral: a combination may emit
both capabilities, or ``generate_agent_code`` may raise an actionable
``CodeGenerationUnsupportedError``. Returning runnable source that contains
only one requested capability is never acceptable.
"""

from __future__ import annotations

import ast

import pytest
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import CfnTemplateGenerator
from app.services.code_generator import (
    CodeGenerationUnsupportedError,
    generate_agent_code,
)
from app.services.python_exporter import build_python_project


def _config(*, multi_agent: bool = False, protocol: str = "HTTP") -> RuntimeConfig:
    values: dict = {
        "name": "composition_probe",
        "model": {"modelId": "us.anthropic.claude-sonnet-5"},
        "modelProvider": "bedrock",
        "systemPrompt": "Use every connected component.",
        "protocol": protocol,
    }
    if multi_agent:
        values.update(
            {
                "multiAgentPattern": "graph",
                "multiAgentConfig": {
                    "agents": [
                        {
                            "agentId": "researcher",
                            "systemPrompt": "Research.",
                            "modelId": "us.anthropic.claude-sonnet-5",
                        },
                        {
                            "agentId": "writer",
                            "systemPrompt": "Write.",
                            "modelId": "us.anthropic.claude-sonnet-5",
                        },
                    ],
                    "edges": [
                        {
                            "source": "researcher",
                            "target": "writer",
                        }
                    ],
                    "entryPoint": "researcher",
                },
            }
        )
    return RuntimeConfig(**values)


def _symbols(source: str) -> set[str]:
    tree = ast.parse(source)
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Name):
            symbols.add(node.id)
        elif isinstance(node, ast.Attribute):
            symbols.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            symbols.update(alias.asname or alias.name.rsplit(".", 1)[-1] for alias in node.names)
    return symbols


# Every tuple is an AND of groups; each group is an OR of accepted symbols.
_FEATURE_SYMBOLS: dict[str, tuple[set[str], ...]] = {
    "a2a": (
        {"call_a2a_peer"},
        {"_build_agent_card"},
    ),
    "memory": (
        {"MEMORY_ID"},
        {"MemoryClient", "AgentCoreMemorySessionManager"},
    ),
    "gateway": (
        {"GATEWAY_URL"},
        {"MCPClient"},
    ),
    "browser": ({"browser_session"},),
    "code_interpreter": ({"code_session"},),
    "knowledge_base": (
        {"kb_id"},
        {"retrieve_from_kb", "retrieve_from_knowledge_base", "retrieve"},
    ),
}


def _assert_feature(source: str, feature: str) -> None:
    symbols = _symbols(source)
    missing = [sorted(group) for group in _FEATURE_SYMBOLS[feature] if symbols.isdisjoint(group)]
    assert not missing, (
        f"generated source silently dropped connected feature {feature!r}; missing symbol group(s): {missing}"
    )


def _assert_composes_or_refuses(
    tools: list[str],
    *,
    multi_agent: bool = False,
    template_id: str | None = None,
) -> None:
    try:
        source = generate_agent_code(
            config=_config(
                multi_agent=multi_agent,
                protocol="MCP" if template_id == "mcp-server-runtime" else "HTTP",
            ),
            tools=tools,
            gateway_config={} if "gateway" in tools else None,
            template_id=template_id,
            kb_config={"knowledgeBaseId": "kb-composition-probe"} if "knowledge_base" in tools else None,
            a2a_config={
                "capabilities": ["chat"],
                "peer_allowlist": [],
            },
            portable=True,
        )
    except CodeGenerationUnsupportedError as exc:
        message = str(exc).lower().replace("_", " ")
        for tool in tools:
            assert tool.replace("_", " ") in message, (
                f"an explicit combination refusal must name every capability the customer must change; got {exc!s}"
            )
        if template_id:
            assert template_id in message, (
                f"an explicit template refusal must name templateId {template_id!r}; got {exc!s}"
            )
        return

    compile(source, "<composed-agent.py>", "exec")
    for tool in tools:
        _assert_feature(source, tool)


def _generation_supports(
    tools: list[str],
    *,
    multi_agent: bool = False,
    template_id: str | None = None,
) -> bool:
    """Whether current codegen emits every requested capability."""
    try:
        source = generate_agent_code(
            config=_config(
                multi_agent=multi_agent,
                protocol="MCP" if template_id == "mcp-server-runtime" else "HTTP",
            ),
            tools=tools,
            gateway_config={} if "gateway" in tools else None,
            template_id=template_id,
            kb_config={"knowledgeBaseId": "kb-composition-probe"} if "knowledge_base" in tools else None,
            a2a_config={
                "capabilities": ["chat"],
                "peer_allowlist": [],
            },
            portable=True,
        )
    except CodeGenerationUnsupportedError:
        return False

    try:
        for tool in tools:
            _assert_feature(source, tool)
    except AssertionError:
        return False
    return True


def _deploy_request(
    tools: list[str],
    *,
    multi_agent: bool = False,
    template_id: str | None = None,
) -> DeployRequest:
    if template_id == "mcp-server-runtime":
        # Exercise the composition preflight with the API-valid, model-free
        # standalone MCP shape. Reusing _config() here explicitly supplies
        # model/modelProvider/systemPrompt, so the earlier MCP admission guard
        # correctly rejects those fields before it can name the connected
        # capability this test is meant to prove is refused.
        config: RuntimeConfig | dict = {
            "name": "composition_probe",
            "protocol": "MCP",
            "enableOtel": False,
        }
    else:
        config = _config(multi_agent=multi_agent, protocol="HTTP")

    values: dict = {
        "nodeId": "composition-node",
        "config": config,
        "connectedTools": tools,
    }
    if template_id:
        values["templateId"] = template_id
    if "memory" in tools:
        values["memoryConfig"] = {"enabled": True}
    if "gateway" in tools:
        values["gatewayConfig"] = {"name": "composition-gateway"}
    if "knowledge_base" in tools:
        values["knowledgeBaseConfig"] = {
            "kbMode": "existing",
            "knowledgeBaseId": "KB-COMPOSITION-PROBE",
        }
    if "a2a" in tools:
        values["a2aConfig"] = {
            "capabilities": ["chat"],
            "peerAllowlist": [],
        }
    return DeployRequest(**values)


@pytest.mark.parametrize(
    "tool",
    [
        "a2a",
        "memory",
        "gateway",
        "browser",
        "code_interpreter",
        "knowledge_base",
    ],
)
def test_feature_detector_recognizes_each_working_single_component(tool):
    """Negative control: the detector must not report every generated source broken."""
    _assert_composes_or_refuses([tool])


@pytest.mark.parametrize(
    "tools",
    [
        pytest.param(
            ["memory", "gateway", "knowledge_base"],
            id="memory-gateway-knowledge-base",
        ),
        pytest.param(
            ["browser", "code_interpreter", "knowledge_base"],
            id="built-in-tools-knowledge-base",
        ),
    ],
)
def test_feature_detector_recognizes_declared_supported_compositions(tools):
    """Positive compositions keep the failure matrix calibrated."""
    _assert_composes_or_refuses(tools)


@pytest.mark.parametrize(
    "tools",
    [
        pytest.param(["a2a", "memory"], id="a2a-memory"),
        pytest.param(["a2a", "gateway"], id="a2a-gateway"),
        pytest.param(["a2a", "browser"], id="a2a-browser"),
        pytest.param(["a2a", "code_interpreter"], id="a2a-code-interpreter"),
        pytest.param(["a2a", "knowledge_base"], id="a2a-knowledge-base"),
        pytest.param(["memory", "browser"], id="memory-browser"),
        pytest.param(["memory", "code_interpreter"], id="memory-code-interpreter"),
        pytest.param(["gateway", "browser"], id="gateway-browser"),
        pytest.param(["gateway", "code_interpreter"], id="gateway-code-interpreter"),
        pytest.param(["gateway", "knowledge_base"], id="gateway-knowledge-base"),
    ],
)
def test_connected_component_pairs_are_never_silently_dropped(tools):
    _assert_composes_or_refuses(tools)


@pytest.mark.parametrize(
    "tool",
    [
        "memory",
        "gateway",
        "browser",
        "code_interpreter",
        "knowledge_base",
    ],
)
def test_multi_agent_patterns_do_not_silently_drop_connected_components(tool):
    _assert_composes_or_refuses([tool], multi_agent=True)


@pytest.mark.parametrize(
    "tools,multi_agent",
    [
        pytest.param(["a2a", "memory"], False, id="a2a-memory"),
        pytest.param(["a2a", "gateway"], False, id="a2a-gateway"),
        pytest.param(["a2a", "browser"], False, id="a2a-browser"),
        pytest.param(["a2a", "code_interpreter"], False, id="a2a-code-interpreter"),
        pytest.param(["a2a", "knowledge_base"], False, id="a2a-knowledge-base"),
        pytest.param(["memory", "browser"], False, id="memory-browser"),
        pytest.param(["memory", "code_interpreter"], False, id="memory-code-interpreter"),
        pytest.param(["gateway", "browser"], False, id="gateway-browser"),
        pytest.param(["gateway", "code_interpreter"], False, id="gateway-code-interpreter"),
        pytest.param(["gateway", "knowledge_base"], False, id="gateway-knowledge-base"),
        pytest.param(["memory"], True, id="graph-memory"),
        pytest.param(["gateway"], True, id="graph-gateway"),
        pytest.param(["browser"], True, id="graph-browser"),
        pytest.param(["code_interpreter"], True, id="graph-code-interpreter"),
        pytest.param(["knowledge_base"], True, id="graph-knowledge-base"),
    ],
)
def test_an_unsupported_live_composition_is_refused_before_step_functions(
    tools,
    multi_agent,
):
    """Provisioning happens before GenerateCode, so a late refusal can leak.

    If codegen genuinely supports a combination, the API request remains valid.
    Otherwise request validation must reject it before a deployment record,
    staged secret, state-machine execution, or billable component is created.
    """
    if _generation_supports(tools, multi_agent=multi_agent):
        _deploy_request(tools, multi_agent=multi_agent)
        return

    with pytest.raises(Exception) as exc_info:  # noqa: BLE001 - Pydantic wraps validators
        _deploy_request(tools, multi_agent=multi_agent)

    message = str(exc_info.value).lower().replace("_", " ")
    for tool in tools:
        assert tool.replace("_", " ") in message, (
            f"the preflight refusal must name every capability the customer must disconnect; got {exc_info.value!s}"
        )


def test_config_only_resources_are_included_in_preflight_composition_checks():
    """Sibling configs trigger provisioning even when connectedTools is incomplete."""
    with pytest.raises(Exception) as exc_info:  # noqa: BLE001 - Pydantic wraps validators
        DeployRequest(
            nodeId="composition-node",
            config=_config(),
            connectedTools=["a2a"],
            memoryConfig={"enabled": True},
            gatewayConfig={"name": "composition-gateway"},
            knowledgeBaseConfig={
                "kbMode": "existing",
                "knowledgeBaseId": "KB-COMPOSITION-PROBE",
            },
            a2aConfig={"capabilities": ["chat"]},
        )

    message = str(exc_info.value).lower().replace("_", " ")
    for capability in ("a2a", "memory", "gateway", "knowledge base"):
        assert capability in message


def test_a2a_protocol_is_included_even_without_an_a2a_canvas_node():
    config = _config()
    config.protocol = "A2A"

    with pytest.raises(Exception) as exc_info:  # noqa: BLE001 - Pydantic wraps validators
        DeployRequest(
            nodeId="composition-node",
            config=config,
            connectedTools=["browser"],
        )

    message = str(exc_info.value).lower()
    assert "a2a" in message
    assert "browser" in message


@pytest.mark.parametrize(
    "tools,expected_expression",
    [
        pytest.param(
            ["memory", "browser", "code_interpreter", "knowledge_base"],
            "tools=[retrieve_from_kb,execute_python,browse_web]",
            id="memory-local-tools",
        ),
        pytest.param(
            ["memory", "gateway", "browser", "code_interpreter", "knowledge_base"],
            "tools=_get_gateway_tools()+[retrieve_from_kb,execute_python,browse_web]",
            id="memory-gateway-local-tools",
        ),
        pytest.param(
            ["gateway", "browser", "code_interpreter", "knowledge_base"],
            "local_tools=[retrieve_from_kb,execute_python,browse_web]",
            id="gateway-local-tools",
        ),
    ],
)
def test_feasible_components_are_wired_into_the_agent_tool_list(
    tools,
    expected_expression,
):
    """Definitions merely present in the module are still a silent drop.

    Pin the actual Agent(..., tools=...) wiring for every unified branch.
    """
    source = generate_agent_code(
        config=_config(),
        tools=tools,
        gateway_config={} if "gateway" in tools else None,
        kb_config={"knowledgeBaseId": "kb-composition-probe"},
        portable=True,
    )

    compact = "".join(source.split())
    assert expected_expression in compact
    if "gateway" in tools and "memory" not in tools:
        assert "tools=tools+local_tools" in compact


_TEMPLATE_COMPONENT_CASES = [
    pytest.param(template_id, tool, id=f"{template_id}-{tool}")
    for template_id in (
        "web-search-agent",
        "strands-gateway-agent",
        "mcp-server-runtime",
        "mcp-server-gateway-target",
        "customer-support-assistant",
        "customer-support-blueprint",
    )
    for tool in (
        "memory",
        "gateway",
        "browser",
        "code_interpreter",
        "knowledge_base",
    )
]


@pytest.mark.parametrize("template_id,tool", _TEMPLATE_COMPONENT_CASES)
def test_template_specific_generators_never_silently_drop_connected_components(
    template_id,
    tool,
):
    """Template early returns obey the same emit-or-refuse contract.

    Selecting a gallery template must not make another canvas component
    disappear from otherwise valid generated source.
    """
    _assert_composes_or_refuses([tool], template_id=template_id)


@pytest.mark.parametrize("template_id,tool", _TEMPLATE_COMPONENT_CASES)
def test_unsupported_template_compositions_are_refused_before_step_functions(
    template_id,
    tool,
):
    """A template refusal happens before any connected resource is provisioned."""
    if _generation_supports([tool], template_id=template_id):
        _deploy_request([tool], template_id=template_id)
        return

    with pytest.raises(Exception) as exc_info:  # noqa: BLE001 - Pydantic wraps validators
        _deploy_request([tool], template_id=template_id)

    message = str(exc_info.value).lower().replace("_", " ")
    assert tool.replace("_", " ") in message
    assert template_id in message


@pytest.mark.parametrize(
    "template_id",
    [
        "customer-support-assistant",
        "customer-support-blueprint",
    ],
)
def test_customer_support_templates_emit_the_gateway_and_memory_they_advertise(
    template_id,
):
    """The gallery's advanced templates must work in their declared shape.

    Both templates ship with runtime→gateway and runtime→memory edges and
    explicitly advertise persistent conversation memory. Refusing that built-in
    combination would merely turn a silent-drop defect into an unusable template.
    """
    source = generate_agent_code(
        config=_config(),
        tools=["gateway", "memory"],
        gateway_config={},
        template_id=template_id,
        portable=True,
    )

    compile(source, f"<{template_id}.py>", "exec")
    _assert_feature(source, "gateway")
    _assert_feature(source, "memory")
    compact = "".join(source.split())
    assert "tools=_get_gateway_tools()" in compact
    assert "_save_to_memory(actor_id,session_id,message,response_text)" in compact


@pytest.mark.parametrize(
    "template_id",
    [
        "customer-support-assistant",
        "customer-support-blueprint",
    ],
)
def test_customer_support_template_id_alone_implies_gateway_and_memory(
    template_id,
):
    """The template ID has the same meaning on live deploy and both exports.

    The gallery, CloudFormation generator, and Python exporter all treat these
    template IDs as Gateway+Memory templates. The live generator must not need
    a redundant connectedTools entry to preserve the Memory the template
    advertises.
    """
    source = generate_agent_code(
        config=_config(),
        tools=[],
        gateway_config={},
        template_id=template_id,
        portable=True,
    )

    compile(source, f"<{template_id}-implicit.py>", "exec")
    _assert_feature(source, "gateway")
    _assert_feature(source, "memory")


@pytest.mark.parametrize(
    "template_id,advertised_features",
    [
        pytest.param(
            "strands-gateway-agent",
            {"gateway"},
            id="strands-gateway-agent",
        ),
        pytest.param(
            "mcp-server-gateway-target",
            {"gateway"},
            id="mcp-server-gateway-target",
        ),
        pytest.param(
            "customer-support-assistant",
            {"gateway", "memory"},
            id="customer-support-assistant",
        ),
        pytest.param(
            "customer-support-blueprint",
            {"gateway", "memory"},
            id="customer-support-blueprint",
        ),
    ],
)
def test_composing_an_extra_tool_keeps_the_templates_advertised_features(
    template_id,
    advertised_features,
):
    """Composition may extend a template, but may not replace its declared shape.

    Checking only the newly connected component would allow a superficially
    green implementation to route around the template and silently lose the
    Gateway, or the Gateway plus Memory, that the gallery promises.
    """
    requested_features = advertised_features | {
        "browser",
        "code_interpreter",
        "knowledge_base",
    }
    source = generate_agent_code(
        config=_config(),
        tools=sorted(requested_features),
        gateway_config={},
        template_id=template_id,
        kb_config={"knowledgeBaseId": "kb-composition-probe"},
        portable=True,
    )

    compile(source, f"<{template_id}-extended.py>", "exec")
    for feature in requested_features:
        _assert_feature(source, feature)


@pytest.mark.parametrize(
    "template_id",
    [
        "customer-support-assistant",
        "customer-support-blueprint",
    ],
)
@pytest.mark.parametrize("artifact_kind", ["cloudformation", "python"])
def test_customer_support_exports_keep_their_advertised_gateway_and_memory(
    template_id,
    artifact_kind,
):
    """Every export surface carries the same advertised template capabilities."""
    request = DeployRequest(
        nodeId="template-artifact-node",
        config=_config(),
        templateId=template_id,
    )
    if artifact_kind == "cloudformation":
        source = CfnTemplateGenerator().generate(request).agent_code
    else:
        source = build_python_project(request)["agent.py"]

    compile(source, f"<{artifact_kind}-{template_id}.py>", "exec")
    _assert_feature(source, "gateway")
    _assert_feature(source, "memory")
