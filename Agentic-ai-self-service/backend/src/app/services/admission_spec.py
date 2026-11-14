"""The Spec v2 document workspace admission judges, built from the ``DeployRequest`` that is about to
be deployed (plan §3.B, step 3).

Why from the REQUEST and not from the saved flow: ``POST /api/deploy`` deploys its body. The flow a
request names (``flowId``) is the canvas the body was derived from, but nothing binds the two: a
caller can name a benign flow they own and submit a body with a Jira connector, a foreign model or a
custom tool the flow never had. Admission that judged the flow would admit the body. So the
capabilities judged here come from the request alone -- model, connectors, Gateway tools, custom
tools, external MCP servers, the MCP server runtime, knowledge, Region -- using the same derivation
``spec_service.project`` applies to a canvas, and the saved flow contributes exactly one thing: the
``riskClass``/``dataClass`` a human gave an entry with the same id. Classifications can be carried
over; capabilities cannot.

Pure: no I/O. The deployment handler loads the flow (after proving ownership) and passes its spec.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.models.deployment_models import DeployRequest
from app.schemas.agent_spec_v2 import AgentSpecV2, KnowledgeSpec, ToolSpec
from app.services.spec_compiler import CONNECTOR_TOOL_PREFIX
from app.services.spec_service import KNOWLEDGE_GATEWAY_TOOLS, _merge_classified, _spec_safe_id


def _model(request: DeployRequest) -> dict[str, Any] | None:
    model_cfg = request.config.model if isinstance(request.config.model, dict) else None
    if not model_cfg or not model_cfg.get("modelId"):
        return None
    return {"provider": request.config.model_provider, "modelId": str(model_cfg["modelId"])}


def _gateway_tools(request: DeployRequest) -> tuple[list[ToolSpec], list[KnowledgeSpec]]:
    """``gatewayTools`` names: knowledge for the managed search/answer tools, else Lambda tools. A
    ``connector:<id>`` entry is a connector by reference."""
    tools: list[ToolSpec] = []
    knowledge: list[KnowledgeSpec] = []
    for index, tool_id in enumerate(request.gateway_tools or []):
        if not isinstance(tool_id, str) or not tool_id:
            continue
        safe_id = _spec_safe_id(tool_id, f"tool-{index}")
        if tool_id in KNOWLEDGE_GATEWAY_TOOLS:
            knowledge.append(
                KnowledgeSpec(id=safe_id, type=KNOWLEDGE_GATEWAY_TOOLS[tool_id], config={"toolId": tool_id})
            )
        elif tool_id.startswith(CONNECTOR_TOOL_PREFIX):
            connector_id = tool_id[len(CONNECTOR_TOOL_PREFIX) :]
            tools.append(
                ToolSpec(
                    id=_spec_safe_id(f"connector-{connector_id}", f"connector-{index}"),
                    type="connector",
                    ref=connector_id,
                )
            )
        else:
            tools.append(ToolSpec(id=safe_id, type="lambda", ref=tool_id))
    return tools, knowledge


def _connectors(request: DeployRequest) -> list[ToolSpec]:
    tools: list[ToolSpec] = []
    for index, connector in enumerate(request.connectors or []):
        connector_id = str(getattr(connector, "connector_id", "") or "")
        auth_method = getattr(connector, "auth_method", None)
        scopes = [str(scope) for scope in (getattr(connector, "scopes", None) or [])]
        tools.append(
            ToolSpec(
                id=_spec_safe_id(f"connector-{connector_id or index}", f"connector-{index}"),
                type="connector",
                ref=connector_id,
                auth={"method": auth_method} if auth_method else {},
                scopes=scopes,
            )
        )
    return tools


def _custom_tools(request: DeployRequest) -> tuple[list[ToolSpec], dict[str, Any]]:
    """Custom Lambda tools are both a tool and custom code the classifier floors on."""
    tools: list[ToolSpec] = []
    custom_nodes: dict[str, Any] = {}
    for index, custom in enumerate(request.custom_tools or []):
        tool_name = str(getattr(custom, "tool_name", "") or f"custom-{index}")
        safe_id = _spec_safe_id(tool_name, f"custom-{index}")
        # The code's real hash is part of the tool's identity: a classification given to one body of
        # code must not survive the code changing under the same name.
        code_hash = hashlib.sha256(str(getattr(custom, "lambda_code", "") or "").encode("utf-8")).hexdigest()
        tools.append(ToolSpec(id=safe_id, type="lambda", ref=tool_name, auth={"custom": True, "sha256": code_hash}))
        custom_nodes[safe_id] = {"language": "python", "sourceRef": f"inline:{tool_name}", "sha256": code_hash}
    return tools, custom_nodes


def _external_mcp(request: DeployRequest) -> list[ToolSpec]:
    tools: list[ToolSpec] = []
    for index, server in enumerate(request.external_mcp_servers or []):
        if not isinstance(server, dict):
            continue
        ref = str(server.get("server_id") or server.get("serverId") or server.get("endpoint") or f"mcp-{index}")
        auth_type = server.get("auth_type") or server.get("authType")
        endpoint = server.get("endpoint") or server.get("url")
        # The endpoint is part of the server's identity: a stable server_id pointed at a new host is a
        # different resource and must not inherit the old one's classification.
        auth: dict[str, Any] = {"type": auth_type} if auth_type else {}
        if endpoint:
            auth["endpoint"] = str(endpoint)
        tools.append(ToolSpec(id=_spec_safe_id(f"mcp-{index}-{ref}", f"mcp-{index}"), type="mcp", ref=ref, auth=auth))
    return tools


def _mcp_server_runtime(request: DeployRequest) -> list[ToolSpec]:
    if not request.mcp_server_config:
        return []
    name = str(request.mcp_server_config.get("name") or "mcp-server")
    return [ToolSpec(id="mcp-server-runtime", type="agent", ref=name)]


def _channels(request: DeployRequest) -> list[dict[str, Any]]:
    """The channels this deployment exposes, from the request's authoritative configuration: the runtime
    protocol (``api`` for HTTP, ``mcp`` / ``a2a`` for the protocol servers) and an A2A configuration. The
    channel allow-list judges these; Slack/Teams channels are attached by their own routes after
    deployment and are judged there."""
    channels: list[dict[str, Any]] = []
    protocol = str(getattr(request.config, "protocol", "HTTP") or "HTTP").upper()
    channel_type = {"HTTP": "api", "MCP": "mcp", "A2A": "a2a"}.get(protocol, "api")
    channels.append({"id": f"channel-{channel_type}", "type": channel_type, "config": {"protocol": protocol}})
    if request.a2a_config and channel_type != "a2a":
        channels.append({"id": "channel-a2a", "type": "a2a", "config": dict(request.a2a_config)})
    return channels


#: ``connectedTools`` kinds that give the agent an executable capability of their own (a sandbox
#: that runs model-written code, a browser that reaches the web). Gateway/identity/memory/observability
#: entries are infrastructure the other fields already describe.
_EXECUTABLE_CONNECTED_TOOLS = {"code_interpreter": "code-interpreter", "browser": "browser"}


def _connected_tools(request: DeployRequest) -> list[ToolSpec]:
    tools: list[ToolSpec] = []
    for kind in request.connected_tools or []:
        ref = _EXECUTABLE_CONNECTED_TOOLS.get(str(kind))
        if ref is not None and all(tool.ref != ref for tool in tools):
            tools.append(ToolSpec(id=ref, type="lambda", ref=ref, auth={"managed": True}))
    return tools


def _knowledge_base(request: DeployRequest) -> list[KnowledgeSpec]:
    kb = request.knowledge_base_config
    if not kb:
        return []
    existing = kb.get("knowledgeBaseId") or kb.get("existingKnowledgeBaseId")
    return [KnowledgeSpec(id="knowledge-base", type="customKb" if existing else "managedKb", config=dict(kb))]


def _carry_classifications(previous: list, derived: list) -> list:
    """The projection stage's identity-bound merge (``spec_service._merge_classified``), applied between
    the saved flow's entries and the request-derived ones."""
    return _merge_classified(previous, derived)


def spec_for_deploy_request(
    request: DeployRequest,
    caller_sub: str,
    *,
    classified_from: AgentSpecV2 | None = None,
    home_region: str | None = None,
) -> AgentSpecV2:
    """The spec admission judges for ``request``.

    Capabilities come from the request only. ``classified_from`` (the saved flow's spec, when the
    request names a flow the caller owns) supplies ``riskClass``/``dataClass`` for entries that are
    the SAME resource (type, ref and configuration, not just id), its budget declaration, and its
    ``workspaceId`` so policy resolution can check the deployer's membership; nothing else. A request
    that names no flow gets no classifications: everything it carries is unclassified until a human
    classifies it on a saved canvas.

    ``deployment.region`` is the Region the deployment will actually run in: the explicit
    ``targetRegion`` when the caller sent one, else the platform's home Region (``home_region``).
    Leaving it ``None`` would let an omitted field pass a Region allow-list the home Region is not in.
    """
    gateway_tools, gateway_knowledge = _gateway_tools(request)
    custom_tools, custom_nodes = _custom_tools(request)
    tools = (
        gateway_tools
        + custom_tools
        + _connectors(request)
        + _external_mcp(request)
        + _mcp_server_runtime(request)
        + _connected_tools(request)
    )
    knowledge = gateway_knowledge + _knowledge_base(request)

    budget: dict[str, Any] = {}
    if classified_from is not None:
        if classified_from.policy.budget.monthly_usd is not None:
            budget["monthlyUsd"] = classified_from.policy.budget.monthly_usd
        if classified_from.policy.budget.per_run_usd is not None:
            budget["perRunUsd"] = classified_from.policy.budget.per_run_usd

    spec = AgentSpecV2.model_validate(
        {
            "id": _spec_safe_id(request.flow_id or request.config.name or "unsaved", "unsaved"),
            "name": request.config.name or "unsaved",
            "workspaceId": classified_from.workspace_id if classified_from is not None else None,
            "ownerSub": caller_sub,
            "origin": "canvas" if request.flow_id else "code",
            "model": _model(request),
            "tools": [tool.model_dump(by_alias=True) for tool in tools],
            "knowledge": [item.model_dump(by_alias=True) for item in knowledge],
            "channels": _channels(request),
            "deployment": {
                "region": request.target_region or home_region,
                "mode": request.deployment_mode or "runtime",
            },
            "policy": {"budget": budget},
            "code": {"customNodes": custom_nodes},
        }
    )
    if classified_from is None:
        return spec
    return spec.model_copy(
        update={
            "tools": _carry_classifications(classified_from.tools, spec.tools),
            "knowledge": _carry_classifications(classified_from.knowledge, spec.knowledge),
        }
    )
