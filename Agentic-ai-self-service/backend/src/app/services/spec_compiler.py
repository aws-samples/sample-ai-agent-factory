"""Agent Spec v2 -> the deploy request the platform already accepts.

The deploy pipeline (``POST /api/deploy`` -> ``DeployRequest`` -> Step Functions -> code generation)
does not change for Spec v2: this module turns a spec into exactly the ``POST /api/deploy`` body the
frontend derives from the same canvas today, so Step Functions, the deployment state machine and
``code_generator.py`` consume what they always consumed.

Every function here is a line-for-line port of a frontend function, and the pairing is pinned by
fixtures the frontend writes from its own code (``frontend/src/data/templates.specV2Fixtures.test.ts``
-> ``backend/tests/fixtures/spec_v2/templates/<template>.{flow,request}.json``):

    deriveCanvasDeployInputs      -> derive_canvas_deploy_inputs
    findDefaultDeployableRuntime  -> find_default_deployable_runtime
    runtimeConfigForRequest       -> runtime_config_for_request
    mapGatewayDeployTargets       -> map_gateway_deploy_targets
    identityConfigForRequest      -> identity_config_for_request
    useDeployment's request body  -> compile_deploy_request

``backend/tests/test_agent_spec_v2.py`` requires ``compile_deploy_request(migrate(flow))`` to equal the
recorded request for every gallery template, and the generated agent code to be byte-identical.
When the frontend derivation changes, its fixture changes, and that test tells this module to follow.

Key casing: the persisted canvas (``LogicNode.data``) is snake_case; the frontend holds the same
configuration camelCase in memory and the request body is camelCase, so this module camelizes
configuration objects with the frontend's rule (``keysToCamelCase``: ``contentFilters`` and
``resourceAttributes`` children are domain identifiers and are not renamed). The storage
discriminator ``component_type`` is dropped from emitted configuration objects.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.schemas.agent_spec_v2 import AgentSpecV2, LogicEdge, LogicNode

logger = logging.getLogger(__name__)

CONNECTED_COMPONENT_TYPES = (
    "browser",
    "code_interpreter",
    "memory",
    "gateway",
    "identity",
    "observability",
    "evaluation",
    "policy",
    "guardrails",
    "a2a",
)
CONNECTOR_TOOL_PREFIX = "connector:"
STANDALONE_MCP_TEMPLATE_ID = "mcp-server-runtime"
MODEL_ONLY_RUNTIME_FIELDS = (
    "framework",
    "model",
    "modelProvider",
    "providerApiKeyRef",
    "providerBaseUrl",
    "systemPrompt",
    "multiAgentPattern",
    "multiAgentConfig",
)
_PRESERVE_CHILD_KEYS = frozenset({"contentFilters", "resourceAttributes"})
_SNAKE_SEGMENT = re.compile(r"_([a-z])")


class SpecCompileError(ValueError):
    """The spec cannot be compiled into a deploy request (no deployable runtime, dangling edge, ...)."""


# --------------------------------------------------------------------------- key casing


def to_camel_case(key: str) -> str:
    return _SNAKE_SEGMENT.sub(lambda m: m.group(1).upper(), key)


def keys_to_camel_case(value: Any, parent_key: str | None = None) -> Any:
    """Frontend ``keysToCamelCase``: rename configuration fields, never the identifiers inside
    ``contentFilters`` / ``resourceAttributes`` maps (``prompt_attack`` is a key the editor reads)."""
    if isinstance(value, list):
        return [keys_to_camel_case(item, parent_key) for item in value]
    if isinstance(value, dict):
        preserve = parent_key in _PRESERVE_CHILD_KEYS
        out: dict[str, Any] = {}
        for key, item in value.items():
            converted = key if preserve else to_camel_case(str(key))
            out[converted] = keys_to_camel_case(item, converted)
        return out
    return value


def node_configuration(node: LogicNode) -> dict[str, Any] | None:
    """The node's configuration as the frontend holds it in memory: camelCase, without the storage
    discriminator. None when the node carries no configuration at all."""
    if not node.data:
        return None
    data = {key: value for key, value in node.data.items() if key != "component_type"}
    return keys_to_camel_case(data)


# --------------------------------------------------------------------------- graph walks


def _node_type(node: LogicNode) -> str:
    return node.type or str(node.data.get("component_type") or "")


def _by_id(nodes: list[LogicNode]) -> dict[str, LogicNode]:
    return {node.id: node for node in nodes}


def find_default_deployable_runtime(nodes: list[LogicNode], edges: list[LogicEdge]) -> LogicNode | None:
    """Frontend ``findDefaultDeployableRuntime``: prefer HTTP runtimes; on a gateway with two or more
    runtimes the MCP-protocol ones are server targets, and when all are HTTP every runtime but the
    most-connected one is."""
    index = _by_id(nodes)
    mcp_server_ids: set[str] = set()
    for gateway in (n for n in nodes if _node_type(n) == "gateway"):
        connected_runtime_ids = [
            other
            for other in (
                e.target if e.source == gateway.id else e.source for e in edges if gateway.id in (e.source, e.target)
            )
            if other in index and _node_type(index[other]) == "runtime"
        ]
        if len(connected_runtime_ids) < 2:
            continue
        infos = []
        for rid in connected_runtime_ids:
            cfg = index[rid].data or {}
            total_edges = sum(1 for e in edges if rid in (e.source, e.target))
            infos.append({"id": rid, "protocol": cfg.get("protocol") or "HTTP", "total_edges": total_edges})
        mcp_ones = [info for info in infos if info["protocol"] == "MCP"]
        if mcp_ones:
            mcp_server_ids.update(info["id"] for info in mcp_ones)
        else:
            ordered = sorted(infos, key=lambda info: info["total_edges"])  # stable, like Array.prototype.sort
            mcp_server_ids.update(info["id"] for info in ordered[:-1])

    for node in nodes:
        if _node_type(node) != "runtime" or node.id in mcp_server_ids:
            continue
        if not node.data or node.data.get("protocol") != "MCP":
            return node
    for node in nodes:
        if _node_type(node) == "runtime" and node.id not in mcp_server_ids:
            return node
    for node in nodes:
        if _node_type(node) == "runtime":
            return node
    return None


def derive_canvas_deploy_inputs(
    deployable_node_id: str | None,
    nodes: list[LogicNode],
    edges: list[LogicEdge],
    connector_secrets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Frontend ``deriveCanvasDeployInputs`` over the persisted canvas."""
    secrets = connector_secrets or {}
    empty = {
        "tools": [],
        "gateway_config": None,
        "gateway_tools": [],
        "identity_config": None,
        "custom_tools": [],
        "connectors": [],
        "memory_config": None,
        "evaluation_config": None,
        "policy_config": None,
        "guardrails_config": None,
        "observability_config": None,
        "mcp_server_config": None,
        "knowledge_base_config": None,
        "a2a_config": None,
    }
    if not deployable_node_id:
        return empty
    index = _by_id(nodes)
    out: dict[str, Any] = dict(empty)
    connected_tools: list[str] = []
    gateway_node_id: str | None = None

    for edge in edges:
        if deployable_node_id not in (edge.source, edge.target):
            continue
        other = index.get(edge.target if edge.source == deployable_node_id else edge.source)
        if other is None:
            continue
        kind = _node_type(other)
        if kind not in CONNECTED_COMPONENT_TYPES:
            continue
        connected_tools.append(kind)
        configuration = node_configuration(other)
        if kind == "gateway" and configuration:
            out["gateway_config"] = configuration
            gateway_node_id = other.id
        if kind == "identity" and configuration:
            out["identity_config"] = configuration
        if kind == "memory":
            out["memory_config"] = configuration or {"enabled": True}
        if kind == "evaluation":
            out["evaluation_config"] = configuration or {"enabled": True}
        if kind == "observability":
            out["observability_config"] = configuration or {"enabled": False}
        if kind == "a2a":
            cfg = configuration or {}
            out["a2a_config"] = {
                "capabilities": cfg.get("capabilities") or [],
                "advertised_description": cfg.get("advertisedDescription") or "",
                "peer_allowlist": cfg.get("peerAllowlist") or [],
            }
        if kind == "policy":
            out["policy_config"] = configuration or {"enabled": True}
        if kind == "guardrails":
            out["guardrails_config"] = configuration or {"enabled": True}
    out["tools"] = connected_tools

    if gateway_node_id is None:
        return out
    gateway_tools: list[str] = []
    custom_tools: list[dict[str, Any]] = []
    connectors: list[dict[str, Any]] = []
    mcp_server_tools: list[str] = []
    for edge in edges:
        if gateway_node_id not in (edge.source, edge.target):
            continue
        other_id = edge.target if edge.source == gateway_node_id else edge.source
        if other_id == deployable_node_id:
            continue
        other = index.get(other_id)
        if other is None:
            continue
        kind = _node_type(other)
        if kind == "tool":
            tool = node_configuration(other) or {}
            tool_id = tool.get("toolId")
            if tool.get("isConnector") or (isinstance(tool_id, str) and tool_id.startswith(CONNECTOR_TOOL_PREFIX)):
                is_oauth = tool.get("authMethod") == "oauth2_cc"
                connectors.append(
                    _drop_none(
                        {
                            "connector_id": tool.get("connectorId") or (tool_id or "")[len(CONNECTOR_TOOL_PREFIX) :],
                            "auth_method": tool.get("authMethod"),
                            "secret_value": secrets.get(other_id) or tool.get("secretValue") or None,
                            "secret_arn": tool.get("secretArn") or None,
                            "spec_url": tool.get("specUrl") or None,
                            "spec_inline": tool.get("specContent") or None,
                            "scopes": (tool.get("scopes") or []) if is_oauth else None,
                            "client_id": (tool.get("clientId") or None) if is_oauth else None,
                            "oauth_vendor": (tool.get("oauthVendor") or None) if is_oauth else None,
                            "discovery_url": (tool.get("discoveryUrl") or None) if is_oauth else None,
                            "credential_location": (tool.get("credentialLocation") or None) if not is_oauth else None,
                            "credential_parameter_name": (tool.get("credentialParameterName") or None)
                            if not is_oauth
                            else None,
                            "credential_prefix": (tool.get("credentialPrefix") or None) if not is_oauth else None,
                        }
                    )
                )
            elif tool_id == "knowledge_base" and tool.get("isKnowledgeBase"):
                out["knowledge_base_config"] = tool
            elif tool_id and not tool.get("isCustom"):
                gateway_tools.append(tool_id)
            if tool.get("isCustom") and tool.get("lambdaCode"):
                custom_tools.append(
                    {
                        "toolName": tool_id or "",
                        "displayName": tool.get("displayName") or tool_id or "",
                        "description": tool.get("description") or "",
                        "lambdaCode": tool["lambdaCode"],
                        "inputSchema": tool.get("inputSchema") or {},
                    }
                )
        if kind == "runtime" and other.data:
            runtime_cfg = node_configuration(other) or {}
            if (runtime_cfg.get("protocol") or "HTTP") == "HTTP":
                logger.warning(
                    "Runtime %r connected to gateway has HTTP protocol; treated as an MCP server target",
                    runtime_cfg.get("name"),
                )
            out["mcp_server_config"] = {
                "name": runtime_cfg.get("name") or "mcp-server",
                "framework": runtime_cfg.get("framework") or "strands_agents",
                "systemPrompt": runtime_cfg.get("systemPrompt") or "",
                "model": runtime_cfg.get("model"),
                "tools": mcp_server_tools,
            }
            for mcp_edge in edges:
                if other_id not in (mcp_edge.source, mcp_edge.target):
                    continue
                tool_node_id = mcp_edge.target if mcp_edge.source == other_id else mcp_edge.source
                if tool_node_id == gateway_node_id:
                    continue
                tool_node = index.get(tool_node_id)
                if tool_node is not None and _node_type(tool_node) == "tool":
                    tool_id = (node_configuration(tool_node) or {}).get("toolId")
                    if tool_id:
                        mcp_server_tools.append(tool_id)
    out["gateway_tools"] = gateway_tools
    out["custom_tools"] = custom_tools
    out["connectors"] = connectors
    return out


def _drop_none(mapping: dict[str, Any]) -> dict[str, Any]:
    """JSON.stringify drops ``undefined`` members; the Python port drops None the same way."""
    return {key: value for key, value in mapping.items() if value is not None}


# --------------------------------------------------------------------------- request pieces


def runtime_config_for_request(config: dict[str, Any], template_id: str | None) -> dict[str, Any]:
    """Frontend ``runtimeConfigForRequest`` (operational defaults; the standalone MCP template sends
    no model-only fields and protocol MCP)."""
    with_defaults = dict(config)
    with_defaults["entrypoint"] = config.get("entrypoint") or "agent.py"
    with_defaults["deploymentType"] = config.get("deploymentType") or "S3_CODE_DEPLOY"
    with_defaults["idleTimeout"] = config.get("idleTimeout") if config.get("idleTimeout") is not None else 900
    with_defaults["maxLifetime"] = config.get("maxLifetime") if config.get("maxLifetime") is not None else 28800
    with_defaults["enableOtel"] = config.get("enableOtel") if config.get("enableOtel") is not None else False
    if template_id != STANDALONE_MCP_TEMPLATE_ID:
        return with_defaults
    stripped = {key: value for key, value in with_defaults.items() if key not in MODEL_ONLY_RUNTIME_FIELDS}
    stripped["protocol"] = "MCP"
    return stripped


def map_mcp_target_to_deploy_entry(target: dict[str, Any]) -> dict[str, Any] | None:
    """Frontend ``mapMcpTargetToDeployEntry``."""
    server_id = target.get("serverId")
    if not server_id:
        return None
    is_custom = server_id == "__custom__"
    if is_custom and not target.get("serverUrl"):
        return None
    entry: dict[str, Any] = (
        {
            "endpoint": target.get("serverUrl"),
            "auth_type": target.get("authType") or "none",
            "name": target.get("customName") or "custom-mcp",
        }
        if is_custom
        else {"server_id": server_id}
    )
    endpoint_vars = target.get("endpointVars")
    if endpoint_vars:
        entry["endpoint_vars"] = endpoint_vars
    if target.get("apiKey"):
        entry["secret_value"] = target["apiKey"]
    if is_custom and target.get("authType") == "api_key":
        header = (target.get("apiKeyHeader") or "").strip()
        entry["api_key_descriptor"] = {
            "location": "HEADER",
            "parameter_name": header or "Authorization",
            "prefix": "" if target.get("apiKeyFormat") == "raw" else "Bearer",
        }
    oauth = target.get("oauth") or {}
    if oauth.get("clientId"):
        entry["oauth"] = _drop_none(
            {
                "client_id": oauth.get("clientId"),
                "client_secret": oauth.get("clientSecret"),
                "discovery_url": oauth.get("discoveryUrl"),
                "scopes": oauth.get("scopes"),
            }
        )
    return entry


def map_gateway_deploy_targets(
    gateway_config: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Frontend ``mapGatewayDeployTargets``: (externalMcpServers, gatewayTargets)."""
    if not gateway_config:
        return [], []
    targets = gateway_config.get("targets") or (
        [gateway_config["targetConfig"]] if gateway_config.get("targetConfig") else []
    )
    external: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    for target in targets:
        if target.get("type") == "mcp_server":
            entry = map_mcp_target_to_deploy_entry(target)
            if entry:
                external.append(entry)
        else:
            others.append(target)
    return external, others


def identity_config_for_request(identity_config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Frontend ``identityConfigForRequest``: present only for OAuth2-configured or per-agent identity."""
    if not identity_config:
        return None
    oauth2 = identity_config.get("oauth2Config")
    if not (oauth2 or identity_config.get("mode") == "per_agent"):
        return None
    oauth2 = oauth2 or {}
    return _drop_none(
        {
            "mode": identity_config.get("mode") if identity_config.get("mode") is not None else "shared",
            "provider": oauth2.get("provider"),
            "clientId": oauth2.get("clientId"),
            "clientSecretRef": oauth2.get("clientSecretRef"),
            "discoveryUrl": oauth2.get("discoveryUrl") or "",
            "scopes": oauth2.get("scopes") or [],
            "audience": oauth2.get("audience") or None,
        }
    )


# --------------------------------------------------------------------------- the request


def resolve_deployable_node(spec: AgentSpecV2) -> LogicNode:
    nodes, edges = spec.logic.nodes, spec.logic.edges
    if spec.deployment.deployable_node_id:
        node = _by_id(nodes).get(spec.deployment.deployable_node_id)
        if node is None or _node_type(node) != "runtime":
            raise SpecCompileError(
                f"deployment.deployableNodeId {spec.deployment.deployable_node_id!r} is not a runtime node of logic.nodes"
            )
        return node
    node = find_default_deployable_runtime(nodes, edges)
    if node is None:
        raise SpecCompileError("logic.nodes has no runtime node to deploy")
    return node


def compile_deploy_request(spec: AgentSpecV2, *, connector_secrets: dict[str, str] | None = None) -> dict[str, Any]:
    """The ``POST /api/deploy`` body (camelCase, members the frontend would send as ``undefined`` omitted,
    keys in the order ``useDeployment`` writes them) for this spec."""
    deployable = resolve_deployable_node(spec)
    template_id = spec.deployment.template_id
    config = node_configuration(deployable) or {}
    inputs = derive_canvas_deploy_inputs(deployable.id, spec.logic.nodes, spec.logic.edges, connector_secrets)
    external_mcp, gateway_targets = map_gateway_deploy_targets(inputs["gateway_config"])
    gateway_config = (
        {**inputs["gateway_config"], "targets": gateway_targets}
        if inputs["gateway_config"]
        else inputs["gateway_config"]
    )
    body = {
        "deploymentMode": spec.deployment.mode,
        "nodeId": deployable.id,
        "flowId": None,
        "config": runtime_config_for_request(config, template_id),
        "connectedTools": inputs["tools"],
        "gatewayConfig": gateway_config,
        "gatewayTools": inputs["gateway_tools"],
        "templateId": template_id,
        "identityConfig": identity_config_for_request(inputs["identity_config"]),
        "customTools": inputs["custom_tools"] or None,
        "connectors": inputs["connectors"] or None,
        "externalMcpServers": external_mcp or None,
        "memoryConfig": inputs["memory_config"] or None,
        "evaluationConfig": inputs["evaluation_config"] or None,
        "policyConfig": inputs["policy_config"] or None,
        "guardrailsConfig": inputs["guardrails_config"] or None,
        "mcpServerConfig": inputs["mcp_server_config"] or None,
        "knowledgeBaseConfig": inputs["knowledge_base_config"] or None,
        "observabilityConfig": inputs["observability_config"] or None,
        "a2aConfig": inputs["a2a_config"] or None,
        "targetRegion": spec.deployment.region,
    }
    # ``gatewayConfig: null`` is sent as an explicit null by the frontend (JSON.stringify keeps null),
    # every other absent member is undefined and dropped.
    return {key: value for key, value in body.items() if value is not None or key == "gatewayConfig"}
