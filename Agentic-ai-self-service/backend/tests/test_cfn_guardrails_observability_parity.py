"""The canvas export carries its controls into a usable, integrity-bound bundle."""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import zipfile

import pytest
import yaml
from app.models.deployment_models import DeployRequest
from app.services.cfn_template_generator import CfnExportUnsupportedError, CfnTemplateGenerator
from fastapi.testclient import TestClient


def _body() -> dict:
    return {
        "nodeId": "governed-research-agent",
        "config": {
            "name": "governed_research",
            "model": {"modelId": "us.anthropic.claude-sonnet-5"},
            "systemPrompt": "Produce a short research brief with checkable sources.",
            "entrypoint": "agent.py",
        },
        # Deliberately omit redundant capability markers. The actual config,
        # not a second client-side list, must drive both resources and code.
        "guardrailsConfig": {
            "mode": "create_new",
            "contentFilters": {"hate": "MEDIUM", "prompt_attack": "HIGH"},
            "piiFilters": [{"type": "EMAIL", "action": "ANONYMIZE"}],
            "deniedTopics": [{"name": "Credentials", "definition": "Requests to disclose authentication secrets"}],
            "wordFilters": ["restricted-demo-topic"],
            "regexFilters": [{"name": "InternalIdentifier", "pattern": "INTERNAL-[0-9]{4}", "action": "BLOCK"}],
        },
        "observabilityConfig": {
            "name": "research_observability",
            "enableOtel": True,
            "provider": "custom",
            "otlpEndpoint": "https://telemetry.example.test",
            "sampleRate": 0.25,
            "serviceName": "research-export",
        },
    }


def _assert_controls(template: dict, source: str) -> None:
    resources = template["Resources"]
    guardrail = resources["BedrockGuardrail"]["Properties"]
    filters = {entry["Type"]: entry for entry in guardrail["ContentPolicyConfig"]["FiltersConfig"]}
    assert filters["HATE"]["InputStrength"] == "MEDIUM"
    assert filters["HATE"]["OutputStrength"] == "MEDIUM"
    assert filters["PROMPT_ATTACK"]["InputStrength"] == "HIGH"
    assert filters["PROMPT_ATTACK"]["OutputStrength"] == "NONE"
    pii = guardrail["SensitiveInformationPolicyConfig"]
    assert {"Type": "EMAIL", "Action": "ANONYMIZE"} in pii["PiiEntitiesConfig"]
    assert pii["RegexesConfig"][0]["Pattern"] == "INTERNAL-[0-9]{4}"
    assert guardrail["WordPolicyConfig"]["WordsConfig"] == [{"Text": "restricted-demo-topic"}]
    assert guardrail["TopicPolicyConfig"]["TopicsConfig"][0]["Name"] == "Credentials"

    runtime = resources["AgentCoreRuntime"]["Properties"]
    env = runtime["EnvironmentVariables"]
    assert env["GUARDRAIL_ID"] == {"Fn::GetAtt": ["BedrockGuardrail", "GuardrailId"]}
    assert env["GUARDRAIL_VERSION"] == {"Fn::GetAtt": ["BedrockGuardrailVersion", "Version"]}
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://telemetry.example.test"
    assert env["OTEL_SERVICE_NAME"] == "research-export"
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.25"
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env
    assert "DRAFT" not in json.dumps(runtime)

    # Provisioning alone is not protection: model and tool enforcement must
    # also be compiled into the actual source included in the download.
    ast.parse(source)
    assert "**_GUARDRAIL_KWARGS" in source
    assert "guardrail_trace" in source
    assert "_guardrail_allows" in source
    assert "apply_guardrail(" in source
    assert "_GUARDRAIL_HOOKS" in source
    assert "StrandsTelemetry" in source


def test_canvas_controls_reach_template_and_generated_source_without_redundant_markers():
    body = _body()
    request = DeployRequest.model_validate(body)
    before = request.model_dump()
    bundle = CfnTemplateGenerator().generate(request)
    _assert_controls(yaml.safe_load(bundle.template_yaml), bundle.agent_code)
    assert request.model_dump() == before, "exporting must not mutate the saved flow's request"


@pytest.mark.parametrize("with_gallery_resources", [False, True], ids=["controls-only", "governed-web-search"])
def test_current_demo_controls_export_without_changing_filters_or_enabling_disabled_tracing(with_gallery_resources):
    """Control values verified read-only from the demo's saved flow, version 47."""
    body = _body()
    body["connectedTools"] = ["guardrails", "observability"]
    body["guardrailsConfig"] = {
        "componentType": "guardrails",
        "name": "Production Guardrails",
        "enabled": True,
        "mode": "create_new",
        # Older clients camel-cased even category-map keys on reload.
        "contentFilters": dict.fromkeys(
            ["hate", "insults", "misconduct", "promptAttack", "sexual", "violence"], "HIGH"
        ),
        "piiFilters": [
            {"type": entity, "action": "ANONYMIZE"}
            for entity in ["EMAIL", "PASSWORD", "AWS_ACCESS_KEY", "AWS_SECRET_KEY"]
        ],
    }
    body["observabilityConfig"] = {"componentType": "observability", "name": "Release Telemetry", "enableOtel": False}
    if with_gallery_resources:
        body.update(
            {
                "templateId": "web-search-agent",
                "connectedTools": ["gateway", "identity", "memory", "guardrails", "evaluation", "observability"],
                "gatewayConfig": {"name": "research-search", "provider": "agentcore", "targets": []},
                "gatewayTools": ["web_search"],
                "identityConfig": {"mode": "shared", "provider": "cognito", "clientId": "", "scopes": []},
                "memoryConfig": {"enabled": True, "strategies": [{"type": "semantic", "name": "executive_context"}]},
                "evaluationConfig": {
                    "enabled": True,
                    "evaluators": ["Builtin.GoalSuccessRate", "Builtin.Correctness", "Builtin.Helpfulness"],
                    "samplingRate": 100,
                },
            }
        )
    request = DeployRequest.model_validate(body)
    before = request.model_dump()
    bundle = CfnTemplateGenerator().generate(request)
    template = yaml.safe_load(bundle.template_yaml)
    policy = template["Resources"]["BedrockGuardrail"]["Properties"]
    filters = policy["ContentPolicyConfig"]["FiltersConfig"]
    assert len(filters) == 6
    assert all(item["InputStrength"] == "HIGH" for item in filters)
    assert all(item["OutputStrength"] == ("NONE" if item["Type"] == "PROMPT_ATTACK" else "HIGH") for item in filters)
    assert {
        (item["Type"], item["Action"]) for item in policy["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
    } == {(entity, "ANONYMIZE") for entity in ["EMAIL", "PASSWORD", "AWS_ACCESS_KEY", "AWS_SECRET_KEY"]}
    env = template["Resources"]["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]
    assert not any(key.startswith("OTEL_EXPORTER_") for key in env)
    assert "OTEL_AUTH_SECRET_ARN" not in env
    # The UI checkbox controls external OTLP, not the platform's native usage
    # ledger. Retain native metering without switching external telemetry on.
    assert env["AGENT_OBSERVABILITY_ENABLED"] == "true"
    assert "StrandsTelemetry" in bundle.agent_code
    assert "AGENTCORE_USAGE" in bundle.agent_code
    ast.parse(bundle.agent_code)
    assert "**_GUARDRAIL_KWARGS" in bundle.agent_code
    assert request.model_dump() == before
    if with_gallery_resources:
        assert {"AgentCoreGateway", "ManagedWebSearchTarget", "AgentCoreMemory", "OnlineEvaluation"} <= set(
            template["Resources"]
        )


def test_guardrails_export_refuses_a_foreign_model_hidden_in_a_bedrock_parent():
    body = _body()
    body["config"]["multiAgentPattern"] = "graph"
    body["config"]["multiAgentConfig"] = {
        "agents": [
            {"agentId": "research", "modelId": "us.anthropic.claude-sonnet-5"},
            {"agentId": "summary", "modelProvider": "openai", "modelId": "gpt-4o"},
        ],
        "edges": [{"source": "research", "target": "summary"}],
        "entryPoint": "research",
    }
    request = DeployRequest.model_validate(body)
    with pytest.raises(CfnExportUnsupportedError, match="every generated model"):
        CfnTemplateGenerator().generate(request)


def test_download_route_returns_a_verified_bundle_instead_of_the_old_refusal(monkeypatch):
    import app.deployment_handler as dh

    monkeypatch.setattr(dh, "_export_owner_hash", lambda _: None)
    client = TestClient(dh.deployment_app, raise_server_exceptions=False)
    response = client.post("/api/generate-cfn-template", json=_body())
    assert response.status_code == 200, response.text
    result = response.json()
    archive_bytes = base64.b64decode(result["zip_base64"], validate=True)
    assert hashlib.sha256(archive_bytes).hexdigest() == result["archive_sha256"]
    assert len(archive_bytes) == result["archive_bytes"]
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = archive.namelist()
        manifest_name = next(name for name in names if name.endswith("/artifact-manifest.json"))
        manifest_bytes = archive.read(manifest_name)
        assert hashlib.sha256(manifest_bytes).hexdigest() == result["artifact_manifest_sha256"]
        prefix = manifest_name.removesuffix("artifact-manifest.json")
        manifest = json.loads(manifest_bytes)
        for entry in manifest["files"]:
            assert hashlib.sha256(archive.read(prefix + entry["path"])).hexdigest() == entry["sha256"]
        template = yaml.safe_load(archive.read(prefix + "template.yaml"))
        source = archive.read(prefix + "agent-code/agent.py").decode()
        _assert_controls(template, source)
        assert prefix + ".github/workflows/deploy.yml" in names
        assert prefix + "ci/verify.sh" in names
