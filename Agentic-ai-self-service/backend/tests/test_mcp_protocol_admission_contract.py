"""Admission contracts for the dedicated, model-free FastMCP runtime.

An MCP control-plane protocol cannot make an HTTP ``BedrockAgentCoreApp``
artifact speak MCP.  The API must therefore admit ``protocol=MCP`` only for the
gallery template that actually emits FastMCP source.  That template is also a
tool server, not a conversational agent, so model-only settings must be absent
rather than accepted and silently ignored.
"""

from __future__ import annotations

import json

import pytest
from app.models.deployment_models import DeployRequest
from app.services.code_generator import generate_agent_code
from pydantic import ValidationError

from tests.test_deploy_gate_ordering import _client, spy  # noqa: F401


def _runtime_config(**extra) -> dict:
    return {
        "name": "mcp_admission_probe",
        "protocol": "MCP",
        **extra,
    }


@pytest.mark.parametrize(
    "template_id",
    [
        "web-search-agent",
        "strands-gateway-agent",
        "customer-support-assistant",
        None,
    ],
)
def test_only_the_fastmcp_template_can_request_mcp_before_any_side_effect(
    spy,  # noqa: F811
    template_id,
):
    body = {
        "nodeId": "mcp-admission-probe",
        "config": _runtime_config(
            model={"modelId": "us.anthropic.claude-sonnet-5"},
        ),
    }
    if template_id is not None:
        body["templateId"] = template_id

    response = _client().post("/api/deploy", json=body)

    assert response.status_code == 422, response.text
    assert "mcp-server-runtime" in response.text
    assert spy.side_effects == []


def test_the_fastmcp_template_accepts_no_model_and_emits_real_fastmcp_source():
    request = DeployRequest.model_validate(
        {
            "nodeId": "model-free-mcp",
            "config": _runtime_config(),
            "templateId": "mcp-server-runtime",
        }
    )

    source = generate_agent_code(
        request.config,
        template_id=request.template_id,
    )

    assert "FastMCP" in source
    assert "BedrockAgentCoreApp" not in source
    assert "MODEL_ID" not in source


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", {"modelId": "us.anthropic.claude-sonnet-5"}),
        ("modelProvider", "bedrock"),
        ("providerApiKeyRef", "arn:aws:secretsmanager:us-east-1:111122223333:secret:model-key"),
        ("providerBaseUrl", "https://models.example.com/v1"),
        ("systemPrompt", "This prompt cannot affect a tool-only server."),
        ("framework", "strands_agents"),
    ],
)
def test_the_fastmcp_template_rejects_explicit_model_only_fields(field, value):
    with pytest.raises(ValidationError, match=field):
        DeployRequest.model_validate(
            {
                "nodeId": "model-free-mcp",
                "config": _runtime_config(**{field: value}),
                "templateId": "mcp-server-runtime",
            }
        )


def test_http_agent_templates_still_require_a_model():
    with pytest.raises(ValidationError, match="model"):
        DeployRequest.model_validate(
            {
                "nodeId": "http-agent",
                "config": {
                    "name": "http_agent",
                    "protocol": "HTTP",
                },
                "templateId": "web-search-agent",
            }
        )


def test_the_step_functions_payload_contains_no_inert_model_fields(spy):  # noqa: F811
    response = _client().post(
        "/api/deploy",
        json={
            "nodeId": "model-free-mcp",
            "config": _runtime_config(),
            "templateId": "mcp-server-runtime",
        },
    )

    assert response.status_code == 202, response.text
    sent = json.loads(spy.start.input_json)
    assert sent["config"]["protocol"] == "MCP"
    assert {
        "framework",
        "model",
        "modelProvider",
        "providerApiKeyRef",
        "providerBaseUrl",
        "systemPrompt",
        "multiAgentPattern",
        "multiAgentConfig",
    }.isdisjoint(sent["config"])
