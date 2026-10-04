"""A template's advertised components must reach the state machine, not just codegen.

The customer-support templates are Gateway + Memory templates in the gallery, the
CloudFormation generator and the Python exporter, and ``generate_agent_code`` now reads
the template id the same way. That alone would be worse than before: the state machine
runs the Memory step only when ``$.memory_config`` is present
(``infra/stacks/platform/step_functions.py``), so a request carrying only the template id
would generate a Memory agent and provision no Memory -- a runtime that fails every turn
on its MEMORY_ID guard. These tests go through the real ``/api/deploy`` route and read the
exact StartExecution input.
"""

from __future__ import annotations

import json

import pytest
from app.models.template_composition import template_implied_capabilities

from tests.test_deploy_gate_ordering import _body, _client, spy  # noqa: F401  (spy is a fixture)

CUSTOMER_SUPPORT = ["customer-support-assistant", "customer-support-blueprint"]


def _sent(spy) -> dict:  # noqa: F811
    return json.loads(spy.start.input_json)


@pytest.mark.parametrize("template_id", CUSTOMER_SUPPORT)
def test_the_template_id_alone_provisions_memory_and_gateway(spy, template_id):  # noqa: F811
    response = _client().post("/api/deploy", json=_body(templateId=template_id))

    assert response.status_code == 202, response.text
    sent = _sent(spy)
    assert sent["memory_config"] == {"enabled": True}  # the step's own gate is is_present
    assert {"memory", "gateway"} <= set(sent["connected_tools"])
    assert sent.get("gateway_config"), "the gateway step needs a config to run"


@pytest.mark.parametrize("template_id", CUSTOMER_SUPPORT)
def test_an_explicit_memory_config_is_kept_verbatim(spy, template_id):  # noqa: F811
    memory = {"enabled": True, "name": "support_mem", "strategies": [{"type": "summary"}]}

    response = _client().post("/api/deploy", json=_body(templateId=template_id, memoryConfig=memory))

    assert response.status_code == 202, response.text
    assert _sent(spy)["memory_config"] == memory


@pytest.mark.parametrize("template_id", CUSTOMER_SUPPORT)
def test_disabling_the_memory_a_template_generates_is_refused_before_any_side_effect(spy, template_id):  # noqa: F811
    response = _client().post("/api/deploy", json=_body(templateId=template_id, memoryConfig={"enabled": False}))

    assert response.status_code == 422, response.text
    assert template_id in response.text and "Memory" in response.text
    assert spy.side_effects == []


@pytest.mark.parametrize("template_id", ["strands-gateway-agent", "mcp-server-gateway-target"])
def test_a_gateway_template_provisions_a_gateway_and_no_memory(spy, template_id):  # noqa: F811
    response = _client().post("/api/deploy", json=_body(templateId=template_id))

    assert response.status_code == 202, response.text
    sent = _sent(spy)
    assert "memory_config" not in sent
    assert "memory" not in sent["connected_tools"]
    assert "gateway" in sent["connected_tools"] and sent.get("gateway_config")


def test_an_ordinary_request_is_unchanged(spy):  # noqa: F811
    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 202, response.text
    sent = _sent(spy)
    assert "memory_config" not in sent
    assert not {"memory", "gateway"} & set(sent.get("connected_tools") or [])


def test_the_shared_model_matches_what_both_exporters_infer():
    assert template_implied_capabilities("customer-support-assistant") == {"gateway", "memory"}
    assert template_implied_capabilities("customer-support-blueprint") == {"gateway", "memory"}
    assert template_implied_capabilities("strands-gateway-agent") == {"gateway"}
    assert template_implied_capabilities("mcp-server-gateway-target") == {"gateway"}
    assert template_implied_capabilities("web-search-agent") == frozenset()
    assert template_implied_capabilities(None) == frozenset()
