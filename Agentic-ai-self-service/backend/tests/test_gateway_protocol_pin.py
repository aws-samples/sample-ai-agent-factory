"""F-62: every gateway the platform builds pins its MCP protocol versions.

Measured live (us-west-2, 2026-09-22) on throwaway gateways:
  * created without ``protocolConfiguration``: GetGateway reports ``None`` and initialize
    for 2025-06-18 or 2025-11-25 is answered with 2025-03-26.
  * created with the pinned versions: each is negotiated exactly.
  * ``update_gateway`` that omits ``protocolConfiguration`` resets it to ``None``.
  * an unknown version is rejected at create ("Unsupported MCP Version(s)").
  * pinned to 2026-07-28 as well, its ``server/discover`` omits the ``resultType`` that
    version's schema requires, so the platform does not advertise it.
So the create path, the adoption update and the CFN export must each pin the set, and
the update must carry over what the gateway already has.
"""

from __future__ import annotations

import ast
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services import gateway_deployer
from app.services.cfn_template_generator import CfnTemplateGenerator
from app.services.mcp_gateway_protocol import MCP_SUPPORTED_VERSIONS, pinned_protocol_configuration

from tests.test_cfn_export_contract import _require_scanner
from tests.test_foreign_gateway_is_not_adopted import (
    ADOPT,
    GW_ID,
    GW_NAME,
    GW_URL,
    OLD_CLIENT,
    OUR_ROLE,
    REGION,
    SHARED_POOL,
    _cognito_auth,
    _FakeCognito,
    _FakeCtrl,
    _install,
    shared_env,  # noqa: F401  (fixture)
)

PINNED = ["2025-11-25", "2025-06-18", "2025-03-26"]


def test_the_pinned_set():
    assert list(MCP_SUPPORTED_VERSIONS) == PINNED
    assert "2025-03-26" in MCP_SUPPORTED_VERSIONS, "generated clients and the prewarm still use it"


def test_a_version_the_gateway_cannot_conform_to_is_not_advertised():
    """Re-add 2026-07-28 only with a live verify-mcp-protocol.py pass for it."""
    assert "2026-07-28" not in MCP_SUPPORTED_VERSIONS


def test_pinning_keeps_every_other_mcp_setting():
    existing = {
        "mcp": {
            "supportedVersions": ["2025-03-26"],
            "searchType": "SEMANTIC",
            "sessionConfiguration": {"sessionTimeoutInSeconds": 3600},
            "streamingConfiguration": {"enableResponseStreaming": True},
        }
    }
    out = pinned_protocol_configuration(existing)
    assert out == {"mcp": {**existing["mcp"], "supportedVersions": PINNED}}
    assert existing["mcp"]["supportedVersions"] == ["2025-03-26"], "the input must not be mutated"


@pytest.mark.parametrize("existing", [None, {}, {"mcp": None}, {"mcp": {}}])
def test_an_unpinned_gateway_gets_the_full_set(existing):
    assert pinned_protocol_configuration(existing) == {"mcp": {"supportedVersions": PINNED}}


class _CreatingCtrl(_FakeCtrl):
    """A first deploy: create_gateway succeeds."""

    def __init__(self):
        super().__init__(existing_auth={}, existing_role_arn=OUR_ROLE)
        self.created: list[dict] = []

    def list_gateways(self, **kw):
        return {"items": []}

    def create_gateway(self, **kw):
        self.calls.append("create_gateway")
        self.created.append(kw)
        return {"gatewayId": GW_ID, "gatewayUrl": GW_URL, "gatewayArn": "arn:x", "roleArn": OUR_ROLE}


def test_create_pins_the_versions(shared_env, monkeypatch):  # noqa: F811
    ctrl = _CreatingCtrl()
    _install(monkeypatch, ctrl=ctrl, cog=_FakeCognito(owned_pools=()))

    out = gateway_deployer.deploy_gateway({"name": GW_NAME}, REGION, deployment_id="d-new")

    assert out["gateway_id"] == GW_ID
    assert [c["protocolConfiguration"] for c in ctrl.created] == [{"mcp": {"supportedVersions": PINNED}}]


class _AdoptingCtrl(_FakeCtrl):
    """A redeploy onto our own gateway, which already carries protocol settings."""

    def __init__(self, protocol_configuration):
        super().__init__(existing_auth=_cognito_auth(SHARED_POOL, [OLD_CLIENT]), existing_role_arn=OUR_ROLE)
        self.protocol_configuration = protocol_configuration
        self.updates: list[dict] = []

    def get_gateway(self, **kw):
        detail = super().get_gateway(**kw)
        if self.protocol_configuration is not None:
            detail["protocolConfiguration"] = self.protocol_configuration
        return detail

    def update_gateway(self, **kw):
        self.updates.append(kw)
        return super().update_gateway(**kw)


@pytest.mark.parametrize(
    "existing, expected_mcp",
    [
        pytest.param(None, {"supportedVersions": PINNED}, id="a pre-fix gateway with none"),
        pytest.param(
            {"mcp": {"supportedVersions": ["2025-03-26"], "streamingConfiguration": {"enableResponseStreaming": True}}},
            {"supportedVersions": PINNED, "streamingConfiguration": {"enableResponseStreaming": True}},
            id="streaming only",
        ),
        pytest.param(
            {"mcp": {"sessionConfiguration": {"sessionTimeoutInSeconds": 1800}}},
            {"supportedVersions": PINNED, "sessionConfiguration": {"sessionTimeoutInSeconds": 1800}},
            id="session only",
        ),
    ],
)
def test_adoption_pins_the_versions_and_keeps_the_rest(shared_env, monkeypatch, existing, expected_mcp):  # noqa: F811
    """update_gateway is a full replace; without this the redeploy resets the gateway."""
    ctrl = _AdoptingCtrl(existing)
    _install(monkeypatch, ctrl=ctrl, cog=_FakeCognito(owned_pools=()))

    out = gateway_deployer.deploy_gateway({"name": GW_NAME}, REGION, deployment_id="d-ours", **ADOPT)

    assert out["gateway_id"] == GW_ID
    assert [u.get("protocolConfiguration") for u in ctrl.updates] == [{"mcp": expected_mcp}]


# --- the CFN export ---------------------------------------------------------------------


def _gateway_template():
    request = DeployRequest(
        config=RuntimeConfig(name="pintest", model={"modelId": "us.anthropic.claude-sonnet-5"}),
        nodeId="node-1",
        gateway_config={"gateway_provider": "agentcore", "targetType": "lambda"},
    )
    return CfnTemplateGenerator().generate(request).template_yaml


def test_the_export_pins_the_versions():
    gateways = [
        r
        for r in yaml.safe_load(_gateway_template())["Resources"].values()
        if r["Type"] == "AWS::BedrockAgentCore::Gateway"
    ]
    assert len(gateways) == 1
    assert gateways[0]["Properties"]["ProtocolConfiguration"] == {"Mcp": {"SupportedVersions": PINNED}}


def test_the_pinned_export_passes_cfn_lint():
    """The registry schema is the oracle for the property's shape, not this file."""
    _require_scanner("cfn-lint", "pip install -e '.[dev]'")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "template.yaml"
        path.write_text(_gateway_template())
        proc = subprocess.run(
            ["cfn-lint", str(path), "--format", "parseable", "--ignore-checks", "W"],
            capture_output=True,
            text=True,
            check=False,
        )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- the policy-engine updates: full replaces on a gateway that may predate the pin ----


def test_the_policy_step_attach_pins_a_pre_fix_gateway():
    from tests.test_policy_step_failclosed import _event, _run

    _out, ctrl = _run(_event())  # its get_gateway carries no protocolConfiguration

    _, kw = ctrl.update_gateway.call_args
    assert kw["protocolConfiguration"] == {"mcp": {"supportedVersions": PINNED}}


def test_the_promoter_flip_pins_and_keeps_the_rest():
    from unittest.mock import MagicMock, patch

    from app.services import policy_promoter as pp

    from tests.gateway_fakes import applying_updates
    from tests.test_policy_promoter import _state

    ctrl = MagicMock()
    ctrl.list_policies.return_value = {"policies": [{"name": "allow", "status": "ACTIVE", "policyId": "p1"}]}
    ctrl.get_policy.return_value = {  # ACTIVE is not success: the promoter reads and reconciles the live Cedar first
        "status": "ACTIVE",
        "policyId": "p1",
        "definition": {"cedar": {"statement": "permit(...);"}},
    }
    ctrl.get_gateway.return_value = {
        "name": "gw",
        "roleArn": "r",
        "protocolType": "MCP",
        "authorizerType": "CUSTOM_JWT",
        "policyEngineConfiguration": {"arn": "arn:eng"},
        "protocolConfiguration": {"mcp": {"streamingConfiguration": {"enableResponseStreaming": True}}},
    }
    applying_updates(ctrl)
    with patch.object(pp, "_ctrl", return_value=ctrl):
        assert pp.try_promote_to_enforce(_state(), "us-east-1")["promoted"] is True

    _, kw = ctrl.update_gateway.call_args
    assert kw["protocolConfiguration"] == {
        "mcp": {"supportedVersions": PINNED, "streamingConfiguration": {"enableResponseStreaming": True}}
    }


def _pinned_call(node) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "pinned_protocol_configuration"
    )


def _gateway_writes():
    """Every create_gateway/update_gateway call in the product, with its enclosing function."""
    src = Path(__file__).resolve().parents[1] / "src" / "app"
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("create_gateway", "update_gateway")
                ):
                    yield path.relative_to(src.parent), fn, node


def _is_call_to(node, name: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


def _lock_updates():
    """Every ``<lock>.update(...)`` on a lock bound by ``with gateway_mutation_lock(...) as <lock>``."""
    src = Path(__file__).resolve().parents[1] / "src" / "app"
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            locks = {
                item.optional_vars.id
                for w in ast.walk(fn)
                if isinstance(w, ast.With)
                for item in w.items
                if _is_call_to(item.context_expr, "gateway_mutation_lock") and isinstance(item.optional_vars, ast.Name)
            }
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "update"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in locks
                ):
                    yield path.relative_to(src.parent), fn, node


def test_every_gateway_write_in_the_product_pins_the_versions():
    """A structural guard over every site, and any added later.

    A create passes ``protocolConfiguration=pinned_protocol_configuration(...)``. An
    update is a full replace, so it must be built by ``preserving_gateway_update``, the
    only thing allowed to decide what it re-sends, and which applies the pin last (F-62,
    F62-B). Since F-66e the one ``update_gateway`` call is inside the gateway lock's
    ``update``, and every writer calls that with ``preserving_gateway_update(...)`` as the
    request. ``DeploymentService.deploy`` has no harness, so this is its only test.
    """
    writes, wrong = list(_gateway_writes()), []
    for path, fn, call in writes:
        if call.func.attr == "create_gateway":
            ok = any(k.arg == "protocolConfiguration" and _pinned_call(k.value) for k in call.keywords)
        else:
            ok = str(path) == "app/services/gateway_mutation_lock.py" and fn.name == "update"
        if not ok:
            wrong.append(f"{path}:{call.lineno} {call.func.attr} in {fn.name}")
    updates = list(_lock_updates())
    for path, fn, call in updates:
        if not (call.args and _is_call_to(call.args[0], "preserving_gateway_update")):
            wrong.append(f"{path}:{call.lineno} lock update in {fn.name}")
    assert len(updates) >= 6 and len(writes) >= 2, [f"{p}:{c.lineno}" for p, _, c in writes + updates]
    assert wrong == [], wrong


def test_preserved_fields_are_exactly_what_get_and_update_share():
    """The list is explicit, so a field the service model adds must fail here, not be
    silently cleared by the next full-replace update (F62-B)."""
    import botocore.session
    from app.services.gateway_update import PRESERVED_FIELDS

    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    update = set(model.operation_model("UpdateGateway").input_shape.members)
    get = set(model.operation_model("GetGateway").output_shape.members)
    assert update - get == {"gatewayIdentifier"}
    assert set(PRESERVED_FIELDS) == update & get
