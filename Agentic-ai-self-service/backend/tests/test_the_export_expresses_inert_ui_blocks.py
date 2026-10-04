"""F-G04-003: the UI sends an identity node and an observability block on every export, and two of
the six gallery templates carry them in an INERT shape -- provider selected, every credential field
empty; ``enableOtel: false``. Neither changes the deployed graph, and the platform path does nothing
with them either, so the export must express them by omission rather than refuse the template.
The same fields with a substantive value are still refused (negative controls), and the refusal
allow-list stays closed for every other field.

The positive cases use the exact request bodies the integration harness composes from the frontend
templates (``tests/integration/test_template_deployments.py``), so they track the real UI payload.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest
from app.models.deployment_models import DeployRequest
from app.services.cfn_template_generator import (
    _INERT_IS_EXPRESSIBLE,
    CfnExportUnsupportedError,
    CfnTemplateGenerator,
    _is_inert,
)


def _harness():
    path = pathlib.Path(__file__).resolve().parent / "integration" / "test_template_deployments.py"
    spec = importlib.util.spec_from_file_location("matrix_cases_for_inert_blocks", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need the module registered before exec
    spec.loader.exec_module(module)
    return module


def _real_body(template_id: str) -> dict:
    m = _harness()
    case = next(c for c in m.TEMPLATE_CASES if c.template_id == template_id)
    return m._template_request_payload(case, "deadbeef")


# --------------------------------------------------------------------------- the real UI shapes export


@pytest.mark.parametrize("template_id", ["strands-gateway-agent", "customer-support-assistant"])
def test_the_real_gallery_body_exports(template_id):
    body = _real_body(template_id)
    assert body.get("identityConfig"), "the control: the real body carries the identity node the export used to refuse"
    bundle = CfnTemplateGenerator().generate(DeployRequest.model_validate(body))
    assert "Resources:" in bundle.template_yaml
    # Nothing of the inert blocks leaks into the artifact: no OTLP wiring, no identity client.
    assert "OTEL_EXPORTER_OTLP" not in bundle.template_yaml
    assert body["identityConfig"]["clientId"] == "" and "clientSecretRef: ''" not in bundle.template_yaml


_TEXT_ARTIFACTS = (
    "template_yaml",
    "agent_code",
    "deploy_sh",
    "teardown_sh",
    "readme",
    "build_bundle_sh",
    "deployment_name",
)


def _artifacts(body: dict) -> dict[str, str]:
    bundle = CfnTemplateGenerator().generate(DeployRequest.model_validate(body))
    return {part: (getattr(bundle, part) or "") for part in _TEXT_ARTIFACTS}


@pytest.mark.parametrize("template_id", ["strands-gateway-agent", "customer-support-assistant"])
def test_the_inert_blocks_change_no_artifact_at_all(template_id):
    """The positive proof, measured: every deterministic text artifact of the bundle is byte-identical
    with and without the inert blocks. "Inert" is a claim about the artifacts, not about the template
    text alone -- agent code, deploy/teardown scripts and README included."""
    body = _real_body(template_id)
    stripped = {k: v for k, v in body.items() if k not in ("identityConfig", "observabilityConfig")}
    assert stripped != body, "the control: the real body carried at least one of the blocks"
    with_blocks, without_blocks = _artifacts(body), _artifacts(stripped)
    assert with_blocks == with_blocks, "artifacts must be deterministic for the comparison to mean anything"
    assert _artifacts(body) == with_blocks
    differing = sorted(part for part in _TEXT_ARTIFACTS if with_blocks[part] != without_blocks[part])
    assert differing == [], f"the inert blocks changed {differing}"


def test_the_inert_shapes_are_exactly_the_ones_the_ui_sends():
    strands = _real_body("strands-gateway-agent")
    support = _real_body("customer-support-assistant")
    assert strands["identityConfig"] == {
        "mode": "shared",
        "provider": "cognito",
        "clientId": "",
        "clientSecretRef": "",
        "discoveryUrl": "",
        "scopes": [],
    }
    assert support["observabilityConfig"] == {"name": "support_observability", "enableOtel": False}


# --------------------------------------------------------------------------- substantive values still refuse


@pytest.mark.parametrize(
    "override,field",
    [
        ({"identityConfig": {"provider": "cognito", "clientId": "3fj9s8d7f6g5h4j3k2l1"}}, "identity_config"),
        ({"identityConfig": {"provider": "cognito", "scopes": ["agent/invoke"]}}, "identity_config"),
        ({"identityConfig": {"provider": "cognito", "mode": "per_agent"}}, "identity_config"),
        (
            {
                "identityConfig": {
                    "provider": "cognito",
                    "discoveryUrl": "https://idp.example/.well-known/openid-configuration",
                }
            },
            "identity_config",
        ),
        ({"observabilityConfig": {"name": "o", "enableOtel": True}}, "observability_config"),
        (
            {"observabilityConfig": {"name": "o", "enableOtel": False, "otlpEndpoint": "https://o.example"}},
            "observability_config",
        ),
        ({"observabilityConfig": {"name": "o", "enableOtel": False, "samplingRate": 0}}, "observability_config"),
    ],
)
def test_a_substantive_value_is_still_refused(override, field):
    body = {**_real_body("strands-gateway-agent"), **override}
    with pytest.raises(CfnExportUnsupportedError) as exc:
        CfnTemplateGenerator().generate(DeployRequest.model_validate(body))
    assert field in str(exc.value)


def test_the_allow_list_is_exactly_two_fields():
    assert _INERT_IS_EXPRESSIBLE == {"identity_config", "observability_config"}


def test_an_inert_shape_of_a_non_allow_listed_field_is_still_refused():
    # guardrails_config is not allow-listed: even an "off" shape is refused, because nobody has
    # argued its inert shape is inert.
    body = {**_real_body("strands-gateway-agent"), "guardrailsConfig": {"enabled": False}}
    with pytest.raises(CfnExportUnsupportedError) as exc:
        CfnTemplateGenerator().generate(DeployRequest.model_validate(body))
    assert "guardrails_config" in str(exc.value)


# --------------------------------------------------------------------------- the predicate


def test_is_inert_treats_numbers_as_settings_and_descriptors_as_noise():
    assert _is_inert({"name": "x", "provider": "cognito", "enabled": False, "items": [], "note": ""})
    assert not _is_inert({"name": "x", "samplingRate": 0})
    assert not _is_inert({"enabled": True})
    assert not _is_inert({"nested": {"deep": ["v"]}})
    assert _is_inert(None) and _is_inert("") and _is_inert([]) and _is_inert({})


def test_is_inert_on_the_typed_identity_model_uses_defaults_not_falsiness():
    from app.models.deployment_models import IdentityConfig

    assert _is_inert(
        IdentityConfig.model_validate({"mode": "shared", "provider": "cognito", "clientId": "", "scopes": []})
    )
    assert not _is_inert(IdentityConfig.model_validate({"mode": "per_agent"}))
    assert not _is_inert(IdentityConfig.model_validate({"clientId": "abc"}))
    assert not _is_inert(IdentityConfig.model_validate({"audience": "api://x"}))
    # The result must be reachable from JSON too (what the harness composes).
    assert json.dumps(_real_body("strands-gateway-agent")["identityConfig"])


# --------------------------------------------------------------------------- no dynamic dispatch (isolation)

_DYNAMIC_DISPATCH = {"getattr", "hasattr", "setattr", "vars", "__import__", "attrgetter", "methodcaller"}


def _dynamic_dispatch_calls(source: str) -> list[str]:
    import ast

    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in _DYNAMIC_DISPATCH:
                found.append(f"{f.id}:{node.lineno}")
            if isinstance(f, ast.Attribute) and f.attr in ("__getattribute__", "__getattr__"):
                found.append(f"{f.attr}:{node.lineno}")
    return found


def test_is_inert_uses_no_dynamic_attribute_dispatch():
    """The export bundle isolation test refuses run-time-chosen attributes in this module (an S3
    operation could be reached without being named). Pinned here too, at the function, so the
    regression is caught by THIS file even if the inventory-based test is ever relaxed."""
    import inspect

    from app.services import cfn_template_generator as gen

    assert _dynamic_dispatch_calls(inspect.getsource(gen._is_inert)) == []


def test_the_dispatch_scanner_catches_a_planted_getattr():
    """Control: the scanner is not vacuous."""
    planted = "def f(value, name):\n    return getattr(value, name, None)\n"
    assert _dynamic_dispatch_calls(planted) == ["getattr:2"]
    assert _dynamic_dispatch_calls("def g(o):\n    return o.__getattribute__('x')\n") == ["__getattribute__:2"]
