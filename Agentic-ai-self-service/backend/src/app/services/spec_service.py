"""Agent Spec v2 operations: validate, project, normalize, content hash, diff.

* ``validate(document)``: strict parse; every problem is reported with its JSON-pointer field path,
  and a document whose projections disagree with its graph is refused the same way. Raises
  ``SpecValidationError`` carrying the list; the API layer turns it into a 422 body.
* ``project(spec)``: derive ``model``, ``instructions``, ``tools``, ``knowledge`` and
  ``policy`` from ``logic`` + ``deployment`` through the deploy compiler's graph walk, so the
  projections are exactly what the deploy would use.
* ``normalize(spec)``: project, sort what is unordered (tags), clear ``meta.contentHash`` and
  set it from the normalized content.
* ``content_hash(spec)``: SHA-256 of the canonical JSON (by alias, sorted keys, no whitespace, no
  NaN, ``meta.contentHash`` and ``meta.revision`` excluded). Numbers are normalized first: ``Decimal``
  (what DynamoDB hands back for every number) becomes int or float and an integral float becomes an
  int, so the same document hashes the same whether it came from the wire (``300``) or the table
  (``Decimal('300.0')``). ``load()`` applies the same normalization before validation.
* ``diff(before, after)``: RFC 6902 operations (add / remove / replace), with list elements
  addressed by index and lists compared element-wise, so a revision history reads as patches.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.schemas.agent_spec_v2 import AgentSpecV2, KnowledgeSpec, ModelSpec, ToolSpec
from app.services import spec_compiler

KNOWLEDGE_GATEWAY_TOOLS = {"web_search": "webSearch", "web_answer": "webAnswer"}


class SpecValidationError(ValueError):
    """``errors`` is a list of ``{"path": "/a/b/0", "message": "...", "type": "..."}``."""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors) or "invalid spec")


def _pointer(loc: tuple[Any, ...]) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in loc) if loc else ""


def _errors_from_pydantic(exc: ValidationError) -> list[dict[str, str]]:
    return [{"path": _pointer(e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]


# --------------------------------------------------------------------------- projections


def _projection_inputs(
    spec: AgentSpecV2,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]] | None]:
    try:
        deployable = spec_compiler.resolve_deployable_node(spec)
    except spec_compiler.SpecCompileError:
        return None, None, None
    config = spec_compiler.node_configuration(deployable) or {}
    inputs = spec_compiler.derive_canvas_deploy_inputs(deployable.id, spec.logic.nodes, spec.logic.edges)
    external_mcp, _ = spec_compiler.map_gateway_deploy_targets(inputs["gateway_config"])
    return config, inputs, external_mcp


def _tool_node_ids(spec: AgentSpecV2) -> dict[str, str]:
    """tool_id -> node id for tool nodes, so projected tools keep a stable id (the node's)."""
    out: dict[str, str] = {}
    for node in spec.logic.nodes:
        if (node.type or node.data.get("component_type")) == "tool":
            tool_id = node.data.get("tool_id")
            if isinstance(tool_id, str) and tool_id and tool_id not in out:
                out[tool_id] = node.id
    return out


def _spec_safe_id(raw: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "_.:-" else "-" for ch in raw)[:128]
    return cleaned if cleaned and (cleaned[0].isalnum()) else fallback


def project(spec: AgentSpecV2) -> AgentSpecV2:
    """Return a copy whose projections are derived from ``logic`` + ``deployment``."""
    config, inputs, external_mcp = _projection_inputs(spec)
    if config is None or inputs is None:
        return spec.model_copy(deep=True)
    model = None
    model_cfg = config.get("model") if isinstance(config.get("model"), dict) else None
    if model_cfg and model_cfg.get("modelId"):
        params = {key: value for key, value in model_cfg.items() if key not in ("provider", "modelId")}
        model = {
            "provider": config.get("modelProvider") or "bedrock",
            "model_id": model_cfg["modelId"],
            "params": params,
            "vendor": model_cfg.get("provider"),
        }
    policy_cfg = inputs["policy_config"] or {}
    guardrails = policy_cfg.get("guardrails") if isinstance(policy_cfg.get("guardrails"), dict) else {}
    profile = guardrails.get("profile") if guardrails.get("profile") in ("off", "standard", "strict") else "off"

    node_ids = _tool_node_ids(spec)
    tools: list[ToolSpec] = []
    knowledge: list[KnowledgeSpec] = []
    for index, tool_id in enumerate(inputs["gateway_tools"]):
        node_id = _spec_safe_id(node_ids.get(tool_id, tool_id), f"tool-{index}")
        if tool_id in KNOWLEDGE_GATEWAY_TOOLS:
            knowledge.append(
                KnowledgeSpec(id=node_id, type=KNOWLEDGE_GATEWAY_TOOLS[tool_id], config={"toolId": tool_id})
            )
        else:
            tools.append(ToolSpec(id=node_id, type="lambda", ref=tool_id))
    for index, custom in enumerate(inputs["custom_tools"]):
        tool_id = custom["toolName"] or f"custom-{index}"
        tools.append(
            ToolSpec(
                id=_spec_safe_id(node_ids.get(tool_id, tool_id), f"custom-{index}"),
                type="lambda",
                ref=tool_id,
                auth={"custom": True},
            )
        )
    for index, connector in enumerate(inputs["connectors"]):
        tools.append(
            ToolSpec(
                id=_spec_safe_id(f"connector-{connector.get('connector_id') or index}", f"connector-{index}"),
                type="connector",
                ref=str(connector.get("connector_id") or ""),
                auth={"method": connector.get("auth_method")} if connector.get("auth_method") else {},
                scopes=[str(s) for s in (connector.get("scopes") or [])],
            )
        )
    for index, server in enumerate(external_mcp or []):
        ref = str(server.get("server_id") or server.get("endpoint") or f"mcp-{index}")
        tools.append(
            ToolSpec(
                id=_spec_safe_id(f"mcp-{index}-{ref}", f"mcp-{index}"),
                type="mcp",
                ref=ref,
                auth={"type": server.get("auth_type")} if server.get("auth_type") else {},
            )
        )
    if inputs["mcp_server_config"]:
        tools.append(
            ToolSpec(
                id="mcp-server-runtime", type="agent", ref=str(inputs["mcp_server_config"].get("name") or "mcp-server")
            )
        )
    kb = inputs["knowledge_base_config"]
    if kb:
        knowledge.append(
            KnowledgeSpec(
                id=_spec_safe_id(node_ids.get("knowledge_base", "knowledge-base"), "knowledge-base"),
                type="customKb" if kb.get("knowledgeBaseId") or kb.get("existingKnowledgeBaseId") else "managedKb",
                config=dict(kb),
            )
        )

    updated = spec.model_copy(deep=True)
    if model is None:
        updated.model = None
    elif spec.model is not None:
        updated.model = spec.model.model_copy(update=model)
    else:
        updated.model = ModelSpec(**model)
    updated.instructions = spec.instructions.model_copy(
        update={"system": str(config.get("systemPrompt") or ""), "guardrail_profile": profile}
    )
    updated.tools = _merge_classified(spec.tools, tools)
    updated.knowledge = _merge_classified(spec.knowledge, knowledge)
    updated.policy = spec.policy.model_copy(update={"gateway_guardrail_profile": profile})
    return updated


def _merge_classified(previous: list, derived: list) -> list:
    """Re-projection keeps the classification a human already gave an entry with the same id."""
    by_id = {entry.id: entry for entry in previous}
    merged = []
    for entry in derived:
        old = by_id.get(entry.id)
        if old is not None:
            if hasattr(entry, "risk_class") and getattr(old, "risk_class", "unclassified") != "unclassified":
                entry = entry.model_copy(update={"risk_class": old.risk_class})
            if hasattr(entry, "data_class") and getattr(old, "data_class", "unclassified") != "unclassified":
                entry = entry.model_copy(update={"data_class": old.data_class})
        merged.append(entry)
    return merged


# --------------------------------------------------------------------------- validate / normalize


def _projection_drift(spec: AgentSpecV2) -> list[dict[str, str]]:
    projected = project(spec)
    errors: list[dict[str, str]] = []
    if projected.model != spec.model:
        errors.append(
            {
                "path": "/model",
                "message": "model disagrees with the deployable runtime node in logic.nodes; run normalize",
                "type": "projection_drift",
            }
        )
    if projected.instructions.system != spec.instructions.system:
        errors.append(
            {
                "path": "/instructions/system",
                "message": "instructions.system disagrees with the runtime node's systemPrompt",
                "type": "projection_drift",
            }
        )
    if projected.instructions.guardrail_profile != spec.instructions.guardrail_profile:
        errors.append(
            {
                "path": "/instructions/guardrailProfile",
                "message": "guardrailProfile disagrees with the policy node",
                "type": "projection_drift",
            }
        )
    if [t.model_dump() for t in projected.tools] != [t.model_dump() for t in spec.tools]:
        errors.append(
            {
                "path": "/tools",
                "message": "tools disagree with the gateway's connected tool nodes",
                "type": "projection_drift",
            }
        )
    if [k.model_dump() for k in projected.knowledge] != [k.model_dump() for k in spec.knowledge]:
        errors.append(
            {
                "path": "/knowledge",
                "message": "knowledge disagrees with the gateway's connected knowledge nodes",
                "type": "projection_drift",
            }
        )
    if projected.policy.gateway_guardrail_profile != spec.policy.gateway_guardrail_profile:
        errors.append(
            {
                "path": "/policy/gatewayGuardrailProfile",
                "message": "gatewayGuardrailProfile disagrees with the policy node",
                "type": "projection_drift",
            }
        )
    return errors


def _graph_integrity(spec: AgentSpecV2) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    ids: dict[str, int] = {}
    for index, node in enumerate(spec.logic.nodes):
        if node.id in ids:
            errors.append(
                {
                    "path": f"/logic/nodes/{index}/id",
                    "message": f"duplicate node id {node.id!r}",
                    "type": "duplicate_id",
                }
            )
        ids[node.id] = index
    for index, edge in enumerate(spec.logic.edges):
        for end in ("source", "target"):
            if getattr(edge, end) not in ids:
                errors.append(
                    {
                        "path": f"/logic/edges/{index}/{end}",
                        "message": f"edge {end} {getattr(edge, end)!r} is not a node id",
                        "type": "dangling_edge",
                    }
                )
    if spec.deployment.deployable_node_id and spec.deployment.deployable_node_id not in ids:
        errors.append(
            {"path": "/deployment/deployableNodeId", "message": "not a node id", "type": "dangling_reference"}
        )
    return errors


def validate(document: dict[str, Any] | AgentSpecV2, *, check_projections: bool = True) -> AgentSpecV2:
    """Strict parse + graph integrity + projection consistency. Raises SpecValidationError."""
    if isinstance(document, AgentSpecV2):
        spec = document
    else:
        try:
            spec = AgentSpecV2.model_validate(document)
        except ValidationError as exc:
            raise SpecValidationError(_errors_from_pydantic(exc)) from None
    errors = _graph_integrity(spec)
    if not errors and check_projections:
        errors = _projection_drift(spec)
    if errors:
        raise SpecValidationError(errors)
    return spec


def normalize(spec: AgentSpecV2) -> AgentSpecV2:
    """Project, order the unordered, and stamp ``meta.contentHash``."""
    projected = project(spec)
    projected.tags = sorted(dict.fromkeys(projected.tags))
    projected.meta = projected.meta.model_copy(update={"content_hash": None})
    projected.meta = projected.meta.model_copy(update={"content_hash": content_hash(projected)})
    return projected


# --------------------------------------------------------------------------- canonical form / hash / diff


def _number_from_decimal(value: Decimal) -> int | float:
    if not value.is_finite():
        raise ValueError("NaN and infinity are not representable in a spec")
    return int(value) if value == value.to_integral_value() else float(value)


def from_storage(value: Any) -> Any:
    """Undo DynamoDB's number representation and nothing else: every ``Decimal`` becomes an int
    when integral, else a float. Types the writer sent are otherwise kept, so a document read back
    from the table equals the document written (DynamoDB itself keeps no int/float distinction)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return _number_from_decimal(value)
    if isinstance(value, dict):
        return {str(key): from_storage(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [from_storage(item) for item in value]
    return value


def _jsonable(value: Any) -> Any:
    """Normalize a JSON view so the same document hashes the same wherever it came from.

    * ``Decimal`` (what DynamoDB returns for every number) becomes int or float;
    * a float is read through its shortest repr, the exact decimal string JSON and DynamoDB carry,
      and becomes an int when that decimal is integral: ``300.0`` and ``300`` are the same number to
      every consumer here, and ``3.894835687299482e+16`` must hash like the ``Decimal`` DynamoDB
      hands back for it (whose integer value differs from the binary float's by 4);
    * NaN / infinity are refused (JSON cannot carry them);
    * tuples become lists; bools and strings pass through.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return _number_from_decimal(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN and infinity are not representable in a spec")
        as_decimal = Decimal(repr(value))
        return int(as_decimal) if as_decimal == as_decimal.to_integral_value() else value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def load(document: dict[str, Any], *, check_projections: bool = True) -> AgentSpecV2:
    """Validate a document read from storage or the wire. DynamoDB ``Decimal`` numbers are
    converted first, because Pydantic would otherwise serialize a Decimal inside an ``Any`` field as
    a string; nothing else about the document is rewritten."""
    return validate(from_storage(document), check_projections=check_projections)


def canonical_document(spec: AgentSpecV2) -> dict[str, Any]:
    """The hashed view: by alias, ``meta.contentHash`` and ``meta.revision`` removed, numbers normalized."""
    document = spec.model_dump(by_alias=True, mode="python")
    meta = document.get("meta") or {}
    meta.pop("contentHash", None)
    meta.pop("revision", None)
    document["meta"] = meta
    return _jsonable(document)


def canonical_json(spec: AgentSpecV2) -> str:
    return json.dumps(
        canonical_document(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def content_hash(spec: AgentSpecV2) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def diff(before: AgentSpecV2 | dict[str, Any], after: AgentSpecV2 | dict[str, Any]) -> list[dict[str, Any]]:
    """RFC 6902 patch from ``before`` to ``after`` over the by-alias JSON views."""
    a = (
        _jsonable(before.model_dump(by_alias=True, mode="python"))
        if isinstance(before, AgentSpecV2)
        else _jsonable(before)
    )
    b = (
        _jsonable(after.model_dump(by_alias=True, mode="python"))
        if isinstance(after, AgentSpecV2)
        else _jsonable(after)
    )
    ops: list[dict[str, Any]] = []
    _diff_into(a, b, "", ops)
    return ops


def _diff_into(a: Any, b: Any, path: str, ops: list[dict[str, Any]]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b), key=str):
            child = f"{path}/{_escape(str(key))}"
            if key not in b:
                ops.append({"op": "remove", "path": child})
            elif key not in a:
                ops.append({"op": "add", "path": child, "value": b[key]})
            else:
                _diff_into(a[key], b[key], child, ops)
        return
    if isinstance(a, list) and isinstance(b, list):
        common = min(len(a), len(b))
        for index in range(common):
            _diff_into(a[index], b[index], f"{path}/{index}", ops)
        # Removals from the end first so earlier indices stay valid when the patch is applied.
        for index in range(len(a) - 1, common - 1, -1):
            ops.append({"op": "remove", "path": f"{path}/{index}"})
        for index in range(common, len(b)):
            ops.append({"op": "add", "path": f"{path}/{index}", "value": b[index]})
        return
    if a != b or type(a) is not type(b):
        ops.append({"op": "replace", "path": path or "", "value": b})


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def apply_patch(document: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply a ``diff`` result (the three ops it emits) to a JSON view; used by tests to prove
    ``apply_patch(before, diff(before, after)) == after``."""
    import copy

    doc = copy.deepcopy(document)
    for op in ops:
        tokens = [t.replace("~1", "/").replace("~0", "~") for t in op["path"].split("/")[1:]]
        if not tokens:
            doc = copy.deepcopy(op["value"])
            continue
        parent = doc
        for token in tokens[:-1]:
            parent = parent[int(token)] if isinstance(parent, list) else parent[token]
        last = tokens[-1]
        if isinstance(parent, list):
            index = int(last)
            if op["op"] == "remove":
                del parent[index]
            elif op["op"] == "add":
                parent.insert(index, copy.deepcopy(op["value"]))
            else:
                parent[index] = copy.deepcopy(op["value"])
        elif op["op"] == "remove":
            del parent[last]
        else:
            parent[last] = copy.deepcopy(op["value"])
    return doc
