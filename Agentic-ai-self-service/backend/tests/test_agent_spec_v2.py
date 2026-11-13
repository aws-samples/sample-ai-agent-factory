"""Agent Spec v2 (Phase 1, component A): schema, migration, compiler, service and the read routes.

What is pinned here, and why each pin exists:

* **The frontend and the backend derive the same deploy request.** ``frontend/src/data/
  templates.specV2Fixtures.test.ts`` records, for every gallery template, the v1 flow document the
  canvas persists and the ``POST /api/deploy`` body the frontend derives from it (its own code, fixed
  clock and PRNG). ``migrate_v1_flow`` + ``compile_deploy_request`` must reproduce that body byte for
  byte, and the generated agent code / requirements and both export ZIPs built from the two requests
  must be identical. A frontend change to the derivation shows up as a fixture diff that the compiler
  has to follow, never as a silent split between the two paths.
* **Migration is lossless.** ``to_v1_flow(migrate_v1_flow(d)) == d`` for every template and for
  hypothesis-generated documents carrying unknown node, edge and document properties.
* **The canonical form is storage-independent.** A document hashes the same from the wire and from a
  DynamoDB round trip (``Decimal`` numbers), and ``diff`` + ``apply_patch`` round-trip.
* **Malformed input is refused with the field's path**, never accepted with a field ignored.
* **The read routes are flag-gated**: off is the exact 404 of a nonexistent route, on is tenant-scoped.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from app.models.deployment_models import DeployRequest
from app.schemas.agent_spec_v2 import AgentSpecV2, json_schema
from app.services import spec_compiler, spec_service
from app.services.spec_migration import SpecMigrationError, migrate_v1_flow, to_v1_flow
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
FIXTURES = BACKEND / "tests" / "fixtures" / "spec_v2" / "templates"
TEMPLATE_IDS = sorted(p.name[: -len(".flow.json")] for p in FIXTURES.glob("*.flow.json"))
EXPECTED_TEMPLATES = {
    "customer-support-assistant",
    "customer-support-blueprint",
    "mcp-server-gateway-target",
    "mcp-server-runtime",
    "strands-gateway-agent",
    "web-search-agent",
}


def _fixture(template_id: str, kind: str) -> dict:
    return json.loads((FIXTURES / f"{template_id}.{kind}.json").read_text(encoding="utf-8"))


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _migrated(template_id: str) -> AgentSpecV2:
    return migrate_v1_flow(_fixture(template_id, "flow"), template_id=template_id, name=template_id)


# --------------------------------------------------------------------------- fixtures ↔ compiler


def test_every_gallery_template_has_a_recorded_fixture_pair() -> None:
    assert set(TEMPLATE_IDS) == EXPECTED_TEMPLATES
    for template_id in TEMPLATE_IDS:
        assert (FIXTURES / f"{template_id}.request.json").exists()


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_a_migrated_template_compiles_to_the_frontends_deploy_request_byte_for_byte(template_id: str) -> None:
    compiled = spec_compiler.compile_deploy_request(_migrated(template_id))
    recorded = _fixture(template_id, "request")
    assert _canonical(compiled) == _canonical(recorded)
    # Key order is part of the contract too: the frontend's JSON.stringify order is what the
    # recorded file holds, and a reordered body is a different byte stream to any signer or log.
    assert list(compiled) == list(recorded)


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_the_recorded_request_is_a_valid_deploy_request(template_id: str) -> None:
    DeployRequest.model_validate(_fixture(template_id, "request"))


def _codegen(request: dict) -> tuple[str, str]:
    from app.services.code_generator import generate_agent_code, generate_requirements

    req = DeployRequest.model_validate(request)
    tools = list(req.connected_tools or [])
    code = generate_agent_code(
        config=req.config,
        tools=tools,
        gateway_config=copy.deepcopy(req.gateway_config),
        template_id=req.template_id,
        gateway_tools=list(req.gateway_tools or []),
        custom_tools=[t.model_dump(by_alias=True) for t in (req.custom_tools or [])] or None,
        observability_enabled=bool(
            req.observability_config or "observability" in tools or getattr(req.config, "enable_otel", False)
        ),
        kb_config=copy.deepcopy(req.knowledge_base_config) or {},
        a2a_config=copy.deepcopy(req.a2a_config) or {},
    )
    requirements = generate_requirements(
        config=req.config, tools=tools, template_id=req.template_id, gateway_tools=list(req.gateway_tools or [])
    )
    return code, requirements


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_generated_agent_code_is_byte_identical_from_the_compiled_and_recorded_requests(template_id: str) -> None:
    from_spec = _codegen(spec_compiler.compile_deploy_request(_migrated(template_id)))
    from_frontend = _codegen(_fixture(template_id, "request"))
    assert from_spec == from_frontend
    assert len(from_spec[0]) > 500, "codegen produced no agent"


@pytest.mark.parametrize(
    "template_id", sorted(EXPECTED_TEMPLATES - {"mcp-server-runtime", "mcp-server-gateway-target"})
)
def test_export_zips_are_byte_identical_from_the_compiled_and_recorded_requests(template_id: str) -> None:
    """The exit criterion of §8.6 item 4: export ZIP byte equality old vs new."""
    from app.services.cfn_template_generator import CfnTemplateGenerator
    from app.services.python_exporter import build_and_zip

    compiled = DeployRequest.model_validate(spec_compiler.compile_deploy_request(_migrated(template_id)))
    recorded = DeployRequest.model_validate(_fixture(template_id, "request"))
    assert CfnTemplateGenerator().generate(compiled).to_zip() == CfnTemplateGenerator().generate(recorded).to_zip()
    python_from_spec, _ = build_and_zip(compiled)
    python_from_frontend, _ = build_and_zip(recorded)
    assert python_from_spec == python_from_frontend


# --------------------------------------------------------------------------- migration


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_migration_of_a_template_is_lossless(template_id: str) -> None:
    flow = _fixture(template_id, "flow")
    spec = _migrated(template_id)
    assert to_v1_flow(spec) == flow
    assert spec.spec_version == 2 and spec.origin == "template"
    assert spec.deployment.template_id == template_id
    assert spec.meta.derived_from is not None and spec.meta.derived_from.ref == template_id
    # Nothing the v1 document carried is dropped: name/version/metadata/timestamps are parked.
    assert spec.meta.x_legacy["name"] == flow["name"]
    assert spec.meta.x_legacy["metadata"] == flow["metadata"]
    assert spec.meta.x_legacy["created_at"] == flow["created_at"]


def test_projections_are_derived_from_the_graph_and_classifications_default_to_unclassified() -> None:
    spec = _migrated("customer-support-blueprint")
    assert [t.ref for t in spec.tools] == ["get_order", "get_customer", "list_orders", "process_refund"]
    assert {t.type for t in spec.tools} == {"lambda"}
    # A v1 canvas carries no risk classification; the migration must not invent one.
    assert {t.risk_class for t in spec.tools} == {"unclassified"}
    assert spec.model is not None and spec.model.model_id == "us.anthropic.claude-sonnet-5"
    assert spec.model.provider == "bedrock" and spec.model.vendor == "anthropic"
    assert spec.instructions.system.startswith("You are")
    web = _migrated("web-search-agent")
    assert [k.type for k in web.knowledge] == ["webSearch"] and web.knowledge[0].data_class == "unclassified"
    mcp = _migrated("mcp-server-runtime")
    assert mcp.model is None and mcp.tools == [] and mcp.instructions.system == ""


def test_a_human_classification_survives_re_projection() -> None:
    spec = _migrated("customer-support-blueprint")
    spec.tools[3] = spec.tools[3].model_copy(update={"risk_class": "pay"})
    normalized = spec_service.normalize(spec)
    assert normalized.tools[3].ref == "process_refund" and normalized.tools[3].risk_class == "pay"
    assert normalized.tools[0].risk_class == "unclassified"


def test_region_in_v1_metadata_is_not_a_deploy_target() -> None:
    spec = _migrated("strands-gateway-agent")
    assert spec.deployment.region is None
    assert spec.meta.x_legacy["metadata"]["aws_region"] == "us-east-1"
    assert "targetRegion" not in spec_compiler.compile_deploy_request(spec)


def test_migration_refuses_what_is_not_a_v1_flow() -> None:
    with pytest.raises(SpecMigrationError, match="already Spec v2"):
        migrate_v1_flow(_migrated("strands-gateway-agent").model_dump(by_alias=True, mode="json"))
    with pytest.raises(SpecMigrationError, match="every node needs an id"):
        migrate_v1_flow({"nodes": [{"type": "runtime"}], "edges": []})
    with pytest.raises(SpecMigrationError, match="every edge needs id, source and target"):
        migrate_v1_flow({"nodes": [], "edges": [{"id": "e"}]})
    with pytest.raises(SpecMigrationError, match="must be an object"):
        migrate_v1_flow({"nodes": [{"id": "n1", "data": "not-an-object"}], "edges": []})


# --------------------------------------------------------------------------- property-based


_ident = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12)
_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=20),
)
_json_value = st.recursive(
    _scalar,
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(_ident, inner, max_size=3)),
    max_leaves=12,
)
_component_types = st.sampled_from(
    [
        "runtime",
        "gateway",
        "memory",
        "identity",
        "tool",
        "observability",
        "policy",
        "guardrails",
        "evaluation",
        "browser",
        "code_interpreter",
        "a2a",
    ]
)


@st.composite
def _v1_documents(draw):
    count = draw(st.integers(min_value=0, max_value=5))
    nodes = []
    for index in range(count):
        kind = draw(_component_types)
        data = draw(st.dictionaries(_ident, _json_value, max_size=4))
        data["component_type"] = kind
        data["name"] = draw(st.text(min_size=1, max_size=12))
        if kind == "runtime":
            data["protocol"] = draw(st.sampled_from(["HTTP", "MCP"]))
        node = {
            "id": f"{kind}-{index}",
            "type": kind,
            "position": {
                "x": draw(st.integers(0, 2000)),
                "y": draw(st.one_of(st.integers(0, 2000), st.floats(0, 2000, allow_nan=False))),
            },
            "data": data,
            "selected": draw(st.booleans()),
            "validation_status": draw(st.sampled_from(["pending", "valid", "invalid"])),
        }
        # Properties this release does not know must survive the round trip under xLegacy.
        for extra_key in draw(st.lists(st.sampled_from(["measured", "width", "zIndex"]), max_size=2, unique=True)):
            node[extra_key] = draw(_json_value)
        nodes.append(node)
    edges = []
    if count >= 2:
        for index in range(draw(st.integers(0, 4))):
            source, target = draw(st.sampled_from(nodes))["id"], draw(st.sampled_from(nodes))["id"]
            edge = {
                "id": f"edge-{index}",
                "source": source,
                "target": target,
                "source_handle": "output",
                "target_handle": "input",
                "type": draw(st.sampled_from(["data", "authentication", "policy"])),
                "animated": draw(st.booleans()),
            }
            if draw(st.booleans()):
                edge["label"] = draw(st.text(max_size=8))
            edges.append(edge)
    document = {
        "id": "flow-" + draw(st.text(alphabet="abcdef0123456789", min_size=4, max_size=8)),
        "name": draw(st.text(min_size=1, max_size=20)),
        "description": draw(st.text(max_size=30)),
        "version": "1.0.0",
        "nodes": nodes,
        "edges": edges,
        "viewport": {
            "x": draw(st.integers(-500, 500)),
            "y": draw(st.integers(-500, 500)),
            "zoom": draw(st.sampled_from([0.5, 1, 1.5])),
        },
        "metadata": {
            "author": "system",
            "tags": draw(st.lists(st.text(min_size=1, max_size=8), max_size=3)),
            "aws_region": "us-east-1",
            "deployment_status": "not_deployed",
        },
        "governance": {
            "version": 1,
            "namingProfile": None,
            "tags": {"explicitValues": {}, "effectiveValues": {}, "profile": None, "policyRevision": ""},
        },
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-01T00:00:00.000Z",
    }
    if draw(st.booleans()):
        document["x_unknown_top_level"] = draw(_json_value)
    return document


def _dynamodb_round_trip(document: dict) -> dict:
    """What boto3's TypeDeserializer hands back: every number as Decimal."""
    return json.loads(json.dumps(document), parse_float=Decimal, parse_int=Decimal)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_v1_documents())
def test_any_v1_flow_migrates_losslessly_and_survives_a_wire_round_trip(document: dict) -> None:
    spec = migrate_v1_flow(document, template_id=None)
    assert to_v1_flow(spec) == document
    wire = spec.model_dump(by_alias=True, mode="json")
    again = spec_service.load(wire)
    assert again.model_dump(by_alias=True, mode="json") == wire
    assert to_v1_flow(again) == document


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_v1_documents())
def test_content_hash_is_the_same_from_the_wire_and_from_dynamodb(document: dict) -> None:
    spec = spec_service.normalize(migrate_v1_flow(document))
    wire = spec.model_dump(by_alias=True, mode="json")
    from_wire = spec_service.load(wire, check_projections=False)
    from_table = spec_service.load(_dynamodb_round_trip(wire), check_projections=False)
    assert spec_service.content_hash(from_wire) == spec_service.content_hash(from_table) == spec.meta.content_hash


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_v1_documents(), _v1_documents())
def test_diff_then_apply_patch_reproduces_the_target(before_doc: dict, after_doc: dict) -> None:
    before = migrate_v1_flow(before_doc).model_dump(by_alias=True, mode="json")
    after = migrate_v1_flow(after_doc).model_dump(by_alias=True, mode="json")
    patch = spec_service.diff(before, after)
    # diff and apply_patch operate on the normalized views (integral floats folded), so the patch
    # applied to the normalized "before" must reproduce the normalized "after".
    assert spec_service.apply_patch(spec_service._jsonable(before), patch) == spec_service._jsonable(after)
    assert spec_service.diff(after, after) == []
    assert all(op["op"] in {"add", "remove", "replace"} for op in patch)


def test_integral_floats_and_decimals_canonicalize_to_the_same_number() -> None:
    assert spec_service._jsonable(
        {"a": 300.0, "b": Decimal("300.0"), "c": 300, "d": 0.7, "e": Decimal("0.7"), "f": True, "g": -0.0}
    ) == {"a": 300, "b": 300, "c": 300, "d": 0.7, "e": 0.7, "f": True, "g": 0}
    # Above 2**53 the binary float and the decimal string DynamoDB stores name different integers;
    # the canonical form follows the decimal string, so both sides agree (the hypothesis case that
    # found this: 3.894835687299482e+16 -> ...816 as a float, ...820 as the stored Decimal).
    big = 3.894835687299482e16
    assert spec_service._jsonable(big) == spec_service._jsonable(Decimal(repr(big))) == 38948356872994820
    # from_storage only undoes Decimal; it never rewrites a float the writer sent.
    assert spec_service.from_storage({"a": 300.0, "b": Decimal("300"), "c": Decimal("0.5")}) == {
        "a": 300.0,
        "b": 300,
        "c": 0.5,
    }
    assert isinstance(spec_service.from_storage({"a": 300.0})["a"], float)
    with pytest.raises(ValueError, match="NaN"):
        spec_service._jsonable({"x": float("nan")})


# --------------------------------------------------------------------------- adversarial: exact field paths


def _valid_document() -> dict:
    return spec_service.normalize(_migrated("strands-gateway-agent")).model_dump(by_alias=True, mode="json")


def _paths(exc: spec_service.SpecValidationError) -> list[str]:
    return [e["path"] for e in exc.errors]


@pytest.mark.parametrize(
    ("mutate", "expected_path"),
    [
        (lambda d: d.__setitem__("unknownTop", 1), "/unknownTop"),
        (lambda d: d["logic"]["nodes"][0].__setitem__("width", 120), "/logic/nodes/0/width"),
        (lambda d: d["logic"]["edges"][0].__setitem__("label", "x"), "/logic/edges/0/label"),
        (lambda d: d["deployment"].__setitem__("mode", "lambda"), "/deployment/mode"),
        (
            lambda d: d["tools"].append({"id": "t1", "type": "lambda", "ref": "x", "riskClass": "harmless"}),
            "/tools/0/riskClass",
        ),
        (lambda d: d["knowledge"].append({"id": "k1", "type": "pdf", "config": {}}), "/knowledge/0/type"),
        (lambda d: d.pop("id"), "/id"),
        (lambda d: d.__setitem__("specVersion", 3), "/specVersion"),
        (lambda d: d["meta"].__setitem__("contentHash", "nothex"), "/meta/contentHash"),
        (lambda d: d["logic"]["nodes"][0]["position"].__setitem__("z", 1), "/logic/nodes/0/position"),
    ],
)
def test_a_malformed_document_is_refused_with_the_exact_field_path(mutate, expected_path: str) -> None:
    document = _valid_document()
    mutate(document)
    with pytest.raises(spec_service.SpecValidationError) as info:
        spec_service.load(document)
    assert expected_path in _paths(info.value), info.value.errors


def test_graph_integrity_and_projection_drift_are_refused_with_their_paths() -> None:
    dangling = _valid_document()
    dangling["logic"]["edges"][0]["target"] = "ghost"
    with pytest.raises(spec_service.SpecValidationError) as info:
        spec_service.load(dangling)
    assert _paths(info.value) == ["/logic/edges/0/target"]

    duplicate = _valid_document()
    duplicate["logic"]["nodes"].append(copy.deepcopy(duplicate["logic"]["nodes"][0]))
    with pytest.raises(spec_service.SpecValidationError) as info:
        spec_service.load(duplicate)
    assert "/logic/nodes/3/id" in _paths(info.value)

    drifted = _valid_document()
    drifted["instructions"]["system"] = "Someone edited the projection, not the graph"
    with pytest.raises(spec_service.SpecValidationError) as info:
        spec_service.load(drifted)
    assert _paths(info.value) == ["/instructions/system"]
    # The same document is accepted once normalize re-projects it from the graph.
    spec_service.load(
        spec_service.normalize(AgentSpecV2.model_validate(drifted)).model_dump(by_alias=True, mode="json")
    )


def test_the_committed_json_schema_is_current() -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT / "scripts" / "export_agent_spec_schema.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=PROJECT,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    schema = json.loads((PROJECT / "schemas" / "agent-spec-v2.schema.json").read_text(encoding="utf-8"))
    assert schema["$schema"].startswith("https://json-schema.org/draft/2020-12")
    assert schema["additionalProperties"] is False
    assert json_schema()["properties"]["specVersion"]["const"] == 2


# --------------------------------------------------------------------------- routes


def _app(monkeypatch, *, flags: str, sub: str = "user-1", groups=("g-users-default",)) -> TestClient:
    from app.routers import specs as specs_router
    from app.services import feature_flags as ff

    monkeypatch.setenv("RBAC_ENFORCE", "true")
    monkeypatch.setenv(ff.LOCAL_ENV_VAR, flags)
    monkeypatch.setenv("ENVIRONMENT", "local")
    ff.reset_feature_flag_cache()
    app = FastAPI()
    app.include_router(specs_router.router)
    claims = {"sub": sub, "cognito:groups": list(groups)}

    @app.middleware("http")
    async def _inject_lambda_event(request, call_next):
        request.scope["aws.event"] = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def owned_flow(monkeypatch):
    import importlib
    from datetime import UTC, datetime

    from app.models.enums import DeploymentStatus
    from app.models.flow import Flow

    # The package re-exports the module-level ``flow_storage`` INSTANCE under the module's own name,
    # so attribute-style imports return the instance; the module itself lives in sys.modules.
    flow_storage_module = importlib.import_module("app.services.flow_storage")

    store = flow_storage_module.FlowStorage()
    now = datetime.now(UTC)
    flow = Flow(
        id="flow-abc123",
        name="Strands gateway agent",
        workflow=_fixture("strands-gateway-agent", "flow"),
        deployment_status=DeploymentStatus.NOT_DEPLOYED,
        created_at=now,
        updated_at=now,
        owner_sub="user-1",
    )
    store._flows[flow.id] = flow
    monkeypatch.setattr(flow_storage_module, "_active_storage", store)
    return flow


def test_every_spec_route_is_the_exact_404_of_a_nonexistent_route_while_the_flag_is_off(
    monkeypatch, owned_flow
) -> None:
    client = _app(monkeypatch, flags="{}")
    nonexistent = client.get("/api/specs-does-not-exist")
    for response in (
        client.get(f"/api/specs/{owned_flow.id}"),
        client.post("/api/specs/validate", json=_valid_document()),
        client.post(f"/api/specs/{owned_flow.id}/compile"),
    ):
        assert response.status_code == 404
        assert response.json() == nonexistent.json() == {"detail": "Not Found"}


def test_with_the_flag_on_the_owner_reads_their_flow_as_spec_v2_and_others_get_404(monkeypatch, owned_flow) -> None:
    client = _app(monkeypatch, flags='{"specV2Read": true}')
    response = client.get(f"/api/specs/{owned_flow.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["specVersion"] == 2 and body["id"] == owned_flow.id and body["name"] == owned_flow.name
    assert body["ownerSub"] == "user-1" and body["origin"] == "canvas"
    assert body["meta"]["contentHash"] == spec_service.content_hash(spec_service.load(body))
    assert spec_compiler.compile_deploy_request(spec_service.load(body))["connectedTools"] == ["gateway", "identity"]
    assert (
        _app(monkeypatch, flags='{"specV2Read": true}', sub="someone-else")
        .get(f"/api/specs/{owned_flow.id}")
        .status_code
        == 404
    )
    assert client.get("/api/specs/missing-flow").status_code == 404
    assert client.get("/api/specs/bad%20id!").status_code == 400
    assert (
        _app(monkeypatch, flags='{"specV2Read": true}', groups=()).get(f"/api/specs/{owned_flow.id}").status_code == 403
    )


def test_validate_route_answers_422_with_field_paths_and_200_with_the_hash(monkeypatch) -> None:
    client = _app(monkeypatch, flags='{"specV2Read": true}')
    document = _valid_document()
    ok = client.post("/api/specs/validate", json=document)
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True and ok.json()["contentHash"] == document["meta"]["contentHash"]
    document["logic"]["nodes"][0]["width"] = 1
    document["tools"].append({"id": "t", "type": "lambda", "ref": "x", "riskClass": "harmless"})
    bad = client.post("/api/specs/validate", json=document)
    assert bad.status_code == 422
    paths = [e["path"] for e in bad.json()["detail"]["errors"]]
    assert "/logic/nodes/0/width" in paths and "/tools/0/riskClass" in paths
    assert client.post("/api/specs/validate", json=[1, 2]).status_code == 422
    assert (
        client.post(
            "/api/specs/validate", content=b"not json", headers={"content-type": "application/json"}
        ).status_code
        == 422
    )


def test_compile_route_returns_the_recorded_deploy_request_for_the_flow(monkeypatch, owned_flow) -> None:
    client = _app(monkeypatch, flags='{"specV2Read": true}')
    response = client.post(f"/api/specs/{owned_flow.id}/compile")
    assert response.status_code == 200, response.text
    recorded = _fixture("strands-gateway-agent", "request")
    # A saved flow does not record which gallery template it came from (that lives in frontend
    # memory only), so the compiled body is the recorded one without templateId.
    recorded.pop("templateId")
    assert response.json() == recorded
