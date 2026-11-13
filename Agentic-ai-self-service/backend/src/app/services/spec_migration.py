"""v1 flow document -> Agent Spec v2, lossless.

The input is the document the canvas persists (``frontend/src/utils/flowSavePayload.ts``
``buildFlowSavePayload``: ``{id, name, description, version, nodes, edges, viewport, metadata,
governance, created_at, updated_at}``), which is also the shape ``models/workflow.WorkflowDefinition``
reads. Every byte of it lands somewhere in the v2 document:

* ``nodes`` / ``edges`` / ``viewport`` -> ``logic``, nodes and edges verbatim; node or edge properties
  this release does not know go under that node's ``xLegacy``;
* ``description`` -> ``description``; ``governance`` -> ``deployment.governance``;
  ``metadata.tags`` -> ``tags``;
* ``name``, ``version``, ``metadata`` (whole, including ``aws_region``: the Region the canvas was
  saved in, which is not a deploy-target choice), ``created_at``, ``updated_at`` and any unknown
  document-level key -> ``meta.xLegacy`` (``name`` is ``"auto-save"`` for every autosaved flow, so it
  is not the agent's name; the caller passes the flow's display name);
* the projections (``model``, ``instructions``, ``tools``, ``knowledge``, ``policy``) are derived from
  the graph by ``spec_service.project``, the same function ``normalize`` and ``validate`` use.

``template_id`` is a parameter: the gallery template a canvas came from lives in frontend memory
only (``workflowStore.activeTemplateId``) and is not in the persisted document, so the caller
supplies what the frontend would have sent. v2 persists it (``deployment.templateId``).
"""

from __future__ import annotations

import copy
from typing import Any

from app.schemas.agent_spec_v2 import SPEC_VERSION, AgentSpecV2, LogicEdge, LogicNode

DOCUMENT_KEYS = frozenset(
    {
        "id",
        "name",
        "description",
        "version",
        "nodes",
        "edges",
        "viewport",
        "metadata",
        "governance",
        "created_at",
        "updated_at",
    }
)
NODE_KEYS = frozenset({"id", "type", "position", "data", "selected", "validation_status"})
EDGE_KEYS = frozenset({"id", "source", "target", "source_handle", "target_handle", "type", "animated"})
#: Document-level keys that map to a v2 field and are therefore NOT legacy.
_MAPPED_DOCUMENT_KEYS = frozenset({"id", "description", "nodes", "edges", "viewport", "governance"})


class SpecMigrationError(ValueError):
    pass


def is_spec_v2(document: dict[str, Any]) -> bool:
    return document.get("specVersion") == SPEC_VERSION or document.get("spec_version") == SPEC_VERSION


def _node(raw: dict[str, Any]) -> LogicNode:
    known = {key: raw[key] for key in NODE_KEYS if key in raw}
    legacy = {key: copy.deepcopy(raw[key]) for key in raw if key not in NODE_KEYS}
    data = known.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SpecMigrationError(f"node {raw.get('id')!r}: data must be an object")
    position = known.get("position") or {}
    return LogicNode(
        id=str(known["id"]),
        type=str(known.get("type") or data.get("component_type") or ""),
        position={"x": position.get("x", 0), "y": position.get("y", 0)},
        data=copy.deepcopy(data),
        selected=bool(known.get("selected", False)),
        validation_status=str(known.get("validation_status") or "pending"),
        x_legacy=legacy,
    )


def _edge(raw: dict[str, Any]) -> LogicEdge:
    known = {key: raw[key] for key in EDGE_KEYS if key in raw}
    legacy = {key: copy.deepcopy(raw[key]) for key in raw if key not in EDGE_KEYS}
    return LogicEdge(
        id=str(known["id"]),
        source=str(known["source"]),
        target=str(known["target"]),
        source_handle=str(known.get("source_handle") or "output"),
        target_handle=str(known.get("target_handle") or "input"),
        type=str(known.get("type") or "data"),
        animated=bool(known.get("animated", False)),
        x_legacy=legacy,
    )


def migrate_v1_flow(
    document: dict[str, Any],
    *,
    spec_id: str | None = None,
    name: str | None = None,
    owner_sub: str | None = None,
    workspace_id: str | None = None,
    template_id: str | None = None,
    deployable_node_id: str | None = None,
) -> AgentSpecV2:
    """Build a validated, projected v2 spec from a persisted v1 flow document. Pure."""
    from app.services.spec_service import project

    if not isinstance(document, dict):
        raise SpecMigrationError("a v1 flow document must be an object")
    if is_spec_v2(document):
        raise SpecMigrationError("document is already Spec v2")
    nodes_raw = document.get("nodes") or []
    edges_raw = document.get("edges") or []
    if not isinstance(nodes_raw, list) or not isinstance(edges_raw, list):
        raise SpecMigrationError("nodes and edges must be arrays")
    for raw in nodes_raw:
        if not isinstance(raw, dict) or "id" not in raw:
            raise SpecMigrationError("every node needs an id")
    for raw in edges_raw:
        if not isinstance(raw, dict) or not {"id", "source", "target"} <= set(raw):
            raise SpecMigrationError("every edge needs id, source and target")

    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    viewport_raw = document.get("viewport") if isinstance(document.get("viewport"), dict) else {}
    viewport = {"x": viewport_raw.get("x", 0), "y": viewport_raw.get("y", 0), "zoom": viewport_raw.get("zoom", 1)}
    legacy = {key: copy.deepcopy(document[key]) for key in document if key not in _MAPPED_DOCUMENT_KEYS}
    tags = [str(tag) for tag in (metadata.get("tags") or []) if isinstance(tag, str) and tag]

    spec = AgentSpecV2(
        id=str(spec_id or document.get("id") or "spec"),
        name=str(name or document.get("name") or document.get("id") or "agent"),
        description=str(document.get("description") or ""),
        tags=tags,
        workspace_id=workspace_id,
        owner_sub=owner_sub,
        origin="template" if template_id else "canvas",
        logic={
            "nodes": [_node(raw) for raw in nodes_raw],
            "edges": [_edge(raw) for raw in edges_raw],
            "viewport": viewport,
        },
        deployment={
            "mode": "runtime",
            # Home Region stays implicit. ``metadata.aws_region`` is where the canvas was saved (the
            # platform's own Region), not a chosen deploy target; the frontend sends ``targetRegion``
            # only for an explicit choice, and an explicit Region takes the stricter admission path.
            # The saved value is kept under meta.xLegacy.metadata.
            "region": None,
            "template_id": template_id,
            "deployable_node_id": deployable_node_id,
            "governance": copy.deepcopy(document.get("governance") or {}),
        },
        meta={
            "revision": 0,
            "created_at": str(document.get("created_at") or ""),
            "derived_from": {"type": "template", "ref": template_id} if template_id else None,
            "x_legacy": legacy,
        },
    )
    return project(spec)


def to_v1_flow(spec: AgentSpecV2) -> dict[str, Any]:
    """The persisted v1 flow document for a v2 spec: the inverse of ``migrate_v1_flow``.

    What the v2 document owns (``logic``, ``description``, ``deployment.governance``, ``tags``) is
    written back into its v1 place; what the migration parked under ``meta.xLegacy`` (``name``,
    ``version``, ``metadata``, timestamps, unknown keys) is restored verbatim; node and edge
    ``xLegacy`` properties return to their node or edge. ``to_v1_flow(migrate_v1_flow(d)) == d``
    for every v1 document, which is the lossless claim as a test can state it.

    Used when a v1 reader (today's canvas) asks for a document stored as v2.
    """
    legacy = copy.deepcopy(spec.meta.x_legacy)
    metadata = copy.deepcopy(legacy.pop("metadata", None)) if isinstance(legacy.get("metadata"), dict) else {}
    if spec.tags or "tags" in metadata:
        metadata["tags"] = list(spec.tags)
    document: dict[str, Any] = {"id": spec.id}
    if "name" in legacy:
        document["name"] = legacy.pop("name")
    document["description"] = spec.description
    if "version" in legacy:
        document["version"] = legacy.pop("version")
    document["nodes"] = [
        {
            "id": node.id,
            "type": node.type,
            "position": dict(node.position),
            "data": copy.deepcopy(node.data),
            "selected": node.selected,
            "validation_status": node.validation_status,
            **copy.deepcopy(node.x_legacy),
        }
        for node in spec.logic.nodes
    ]
    document["edges"] = [
        {
            "id": edge.id,
            "source": edge.source,
            "target": edge.target,
            "source_handle": edge.source_handle,
            "target_handle": edge.target_handle,
            "type": edge.type,
            "animated": edge.animated,
            **copy.deepcopy(edge.x_legacy),
        }
        for edge in spec.logic.edges
    ]
    document["viewport"] = dict(spec.logic.viewport)
    if metadata:
        document["metadata"] = metadata
    document["governance"] = copy.deepcopy(spec.deployment.governance)
    for key in ("created_at", "updated_at"):
        if key in legacy:
            document[key] = legacy.pop(key)
    document.update(legacy)
    return document
