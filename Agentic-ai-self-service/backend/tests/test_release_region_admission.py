"""Exact seven-Region admission across every NON-export entrypoint.

The export routes carry their own gate (CloudFormation ``Rules`` and ``deploy.sh``). This
suite pins the live surfaces the regional review found informational or bypassable: the
shared request model, the live deploy handler's home-Region fallback, admin deploy-target
registration (API and service layers), target resolution over a legacy allowlist row, the
deploy-panel options the UI is offered, the prepared state-machine payload, the CDK app,
and the evidence recorder's matrix contract. Every surface consumes ONE predicate; the last
test proves each independent copy of the seven (backend, frontend, deploy.sh, CDK matrix, CI
matrix) is literally the same set.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from app.models.deployment_models import DeployRequest
from app.services import deploy_target as dt
from app.services import region_capabilities as capabilities
from app.services.deployment_payload_validation import (
    PayloadPhase,
    ValidationContext,
    validate_deployment_payload,
)
from app.services.region_capabilities import SUPPORTED_SINGLE_REGION_RELEASES, UnsupportedReleaseRegionError
from fastapi.testclient import TestClient
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PROJECT_ROOT.parent
SEVEN = frozenset({"eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3", "us-east-1", "us-east-2", "us-west-2"})
UNSELECTED = ("us-west-1", "ap-southeast-2", "ap-northeast-1", "ca-central-1", "eu-north-1", "eu-south-1")
MODEL_ID = "us.anthropic.claude-sonnet-5"


def _body(**overrides) -> dict:
    body = {
        "nodeId": "region-admission-node",
        "config": {
            "name": "region-admission-agent",
            "framework": "strands_agents",
            "modelProvider": "bedrock",
            "model": {"modelId": MODEL_ID},
            "systemPrompt": "Help the customer.",
        },
        "connectedTools": [],
        "gatewayTools": [],
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- the predicate


def test_the_seven_are_exactly_the_reviewed_selection():
    assert SUPPORTED_SINGLE_REGION_RELEASES == SEVEN
    assert "us-west-1" not in SUPPORTED_SINGLE_REGION_RELEASES


@pytest.mark.parametrize("region", sorted(SEVEN))
def test_predicate_admits_every_selected_region(region):
    assert capabilities.require_release_region(region, surface="x") == region


@pytest.mark.parametrize("value", [*UNSELECTED, "", None, 7, "US-EAST-1", "us-east-1 ", ["us-east-1"]])
def test_predicate_refuses_everything_else_with_the_exact_alternatives(value):
    with pytest.raises(UnsupportedReleaseRegionError) as excinfo:
        capabilities.require_release_region(value, surface="a probe")
    message = str(excinfo.value)
    assert "a probe is refused" in message
    for region in sorted(SEVEN):
        assert f"({region})" in message


# --------------------------------------------------------------------------- request model (all routes)


@pytest.mark.parametrize("region", sorted(SEVEN))
def test_request_model_accepts_a_selected_explicit_target_region(region):
    assert DeployRequest.model_validate(_body(targetRegion=region)).target_region == region


@pytest.mark.parametrize("region", UNSELECTED)
@pytest.mark.parametrize("deployment_mode", ["runtime", "harness"])
def test_request_model_refuses_an_unselected_explicit_target_region_in_every_mode(region, deployment_mode):
    with pytest.raises(ValidationError) as excinfo:
        DeployRequest.model_validate(_body(targetRegion=region, deploymentMode=deployment_mode))
    assert "not a selected production release Region" in str(excinfo.value)


def test_request_model_leaves_an_omitted_target_region_to_the_route():
    assert DeployRequest.model_validate(_body()).target_region is None


# --------------------------------------------------------------------------- live handler: home fallback


def _handler_client(monkeypatch, home_region: str) -> TestClient:
    import app.deployment_handler as handler

    monkeypatch.setattr(handler, "config", dataclasses.replace(handler.config, aws_region=home_region))
    event = {
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "region-owner", "cognito:groups": ["g-users-default"]}}}
        }
    }

    async def inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await handler.deployment_app(scope, receive, send)

    return TestClient(inject, raise_server_exceptions=False)


@pytest.mark.parametrize("home_region", ["us-west-1", "ap-southeast-2"])
def test_live_deploy_refuses_an_unselected_home_region_before_any_write(monkeypatch, home_region):
    """A platform stood up outside the envelope must not quietly become a deployment Region."""
    import app.deployment_handler as handler

    def must_not_be_called(*_args, **_kwargs):
        pytest.fail("a deployment row was written for a refused Region")

    monkeypatch.setattr(handler, "_get_state_store", must_not_be_called)
    response = _handler_client(monkeypatch, home_region).post("/api/deploy", json=_body())
    assert response.status_code == 400, response.text
    assert "not a selected production release Region" in response.text
    assert "this live deployment is refused" in response.text


def test_live_deploy_helper_passes_every_selected_home_region(monkeypatch):
    import app.deployment_handler as handler

    for region in sorted(SEVEN):
        monkeypatch.setattr(handler, "config", dataclasses.replace(handler.config, aws_region=region))
        handler._reject_unsupported_release_region(DeployRequest.model_validate(_body()))


def test_live_deploy_region_gate_runs_before_the_web_search_gate():
    """Order matters: the Region message, not a feature message, must name the root cause."""
    import inspect

    import app.deployment_handler as handler

    source = inspect.getsource(handler.handle_deploy)
    assert source.index("_reject_unsupported_release_region(request)") < source.index(
        "_reject_unsupported_managed_web_search_region(request)"
    )


# --------------------------------------------------------------------------- deploy-target service


def test_add_region_refuses_an_unselected_region_without_touching_the_table(monkeypatch):
    def must_not_write():
        pytest.fail("the settings table was reached for a refused Region")

    monkeypatch.setattr(dt, "_settings_table", must_not_write)
    with pytest.raises(dt.TargetError, match="a deployment target is refused"):
        dt.add_region("ap-south-1", account_id="123456789012", artifact_bucket="agentcore-flows-artifacts-x")


def test_resolve_region_refuses_a_legacy_allowlist_row_outside_the_envelope(monkeypatch):
    """Defense in depth: a row registered before the envelope was enforced must not resolve."""
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "list_regions", lambda: ["eu-west-1", "ap-southeast-2"])
    assert dt.resolve_region("eu-west-1") == "eu-west-1"
    with pytest.raises(dt.TargetError, match="this deployment target is refused"):
        dt.resolve_region("ap-southeast-2")


@pytest.mark.parametrize("home_region", ["us-west-1", "eu-north-1"])
def test_resolve_region_refuses_an_unselected_home_region(monkeypatch, home_region):
    monkeypatch.setenv("APP_AWS_REGION", home_region)
    with pytest.raises(dt.TargetError, match="the platform home Region is refused"):
        dt.resolve_region(None)
    with pytest.raises(dt.TargetError, match="the platform home Region is refused"):
        dt.resolve_region(home_region)


@pytest.mark.parametrize("home_region", sorted(SEVEN))
def test_resolve_region_still_returns_a_selected_home_region(monkeypatch, home_region):
    monkeypatch.setenv("APP_AWS_REGION", home_region)
    assert dt.resolve_region(None) == home_region


# --------------------------------------------------------------------------- admin registration (API boundary)


_KMS = "arn:aws:kms:us-east-1:123456789012:key/0f6c3a6e-0000-4000-8000-000000000001"


def test_admin_region_target_model_refuses_an_unselected_region_before_any_aws_call():
    from app.routers.admin import AccountTargetRequest, RegionTargetRequest

    RegionTargetRequest(region="eu-west-2", evaluation_kms_key_arn=_KMS)
    with pytest.raises(ValidationError, match="a deployment target is refused"):
        RegionTargetRequest(region="us-west-1", evaluation_kms_key_arn=_KMS)
    with pytest.raises(ValidationError, match="a deployment target is refused"):
        AccountTargetRequest(
            account_id="123456789012",
            evaluation_kms_key_arn=_KMS,
            role_arn="arn:aws:iam::123456789012:role/AgentCoreFlowsDeploymentRole",
            region="ap-northeast-1",
        )


def test_admin_region_registration_route_is_a_422_with_no_sts_call(monkeypatch):
    import app.deployment_handler as handler

    def must_not_be_called(*_args, **_kwargs):
        pytest.fail("session_for_target was reached for a refused Region")

    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "session_for_target", must_not_be_called)
    monkeypatch.setattr(handler, "config", dataclasses.replace(handler.config, aws_region="us-east-1"))
    event = {
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "admin", "cognito:groups": ["g-admins-super"]}}}}
    }

    async def inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await handler.deployment_app(scope, receive, send)

    client = TestClient(inject, raise_server_exceptions=False)
    response = client.post(
        "/api/admin/deploy-targets/regions",
        json={"region": "us-west-1", "evaluation_kms_key_arn": _KMS},
    )
    assert response.status_code == 422, response.text
    assert "a deployment target is refused" in response.text
    assert "us-west-2" in response.text, "the refusal names the admissible alternatives"


# --------------------------------------------------------------------------- deploy-panel options


def test_deploy_target_options_never_offer_an_inadmissible_target(monkeypatch):
    import app.deployment_handler as handler

    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "home_region", lambda: "us-east-1")
    monkeypatch.setattr(
        dt,
        "list_region_targets",
        lambda: [
            {"region": "eu-west-1", "evaluation_kms_key_arn": _KMS},
            {"region": "ap-southeast-2", "evaluation_kms_key_arn": _KMS},
        ],
    )
    monkeypatch.setattr(
        dt,
        "list_accounts",
        lambda: [
            {"account_id": "111111111111", "region": "us-west-2", "evaluation_kms_key_arn": _KMS},
            {"account_id": "222222222222", "region": "us-west-1", "evaluation_kms_key_arn": _KMS},
        ],
    )
    event = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "u", "cognito:groups": ["g-users-default"]}}}}}

    async def inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await handler.deployment_app(scope, receive, send)

    response = TestClient(inject, raise_server_exceptions=False).get("/api/deploy-targets")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["regions"] == ["eu-west-1"]
    assert [target["region"] for target in payload["region_targets"]] == ["eu-west-1"]
    assert [account["account_id"] for account in payload["accounts"]] == ["111111111111"]
    assert payload["home_region_admissible"] is True
    assert payload["release_regions"] == sorted(SEVEN)


# --------------------------------------------------------------------------- state-machine payload


_PREPARED_BASE: dict = {
    "deployment_id": "b3f1c2d4-1111-2222-3333-444455556666",
    "workflow_id": "7d1e0c2a-5b9f-4f7e-8a31-2c6d9e0b4a17",
    "node_id": "d47f6a7b85",
    "config": {"name": "my-agent", "model": {"modelId": MODEL_ID}},
    "connected_tools": [],
    "version_id": "v-20260922-0001",
    "friendly_runtime_name": "my-agent",
    "agentcore_runtime_name": "my-agent_a1b2c3d4",
    "deployment_slot": "production",
    "production_gate_required": False,
    "owner_sub": "7478e488-7081-7081-aaaa-bbbbbbbbbbbb",
    "deployment_mode": "runtime",
}


def _prepared_codes(target_region: str) -> tuple[bool, list[str]]:
    context = ValidationContext(home_account_id="123456789012", home_region="us-east-1")
    result = validate_deployment_payload(
        {**_PREPARED_BASE, "target_account_id": "123456789012", "target_region": target_region},
        phase=PayloadPhase.PREPARED,
        context=context,
    )
    return result.is_valid, [error["code"] for error in result.as_error_dicts()]


@pytest.mark.parametrize("region", UNSELECTED)
def test_prepared_payload_naming_an_unselected_region_cannot_start_the_state_machine(region):
    valid, codes = _prepared_codes(region)
    assert not valid
    assert "unsupported_release_region" in codes
    assert "bad_region" not in codes, "a well-formed but unselected Region is an envelope refusal, not a shape error"


@pytest.mark.parametrize("region", sorted(SEVEN))
def test_prepared_payload_naming_a_selected_region_has_no_region_error(region):
    _valid, codes = _prepared_codes(region)
    assert "unsupported_release_region" not in codes and "bad_region" not in codes


# --------------------------------------------------------------------------- CDK app


def _release_regions_module():
    path = PROJECT_ROOT / "infra" / "release_regions.py"
    spec = importlib.util.spec_from_file_location("infra_release_regions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cdk_gate_reads_the_same_seven_from_the_reviewed_matrix():
    module = _release_regions_module()
    assert module.selected_release_regions() == SEVEN
    for region in sorted(SEVEN):
        assert module.require_release_region(region) == region
    for region in UNSELECTED:
        with pytest.raises(module.UnsupportedReleaseRegionError, match="is not a selected production release Region"):
            module.require_release_region(region)


def test_cdk_gate_fails_closed_on_a_missing_or_mutated_matrix(tmp_path):
    module = _release_regions_module()
    with pytest.raises(module.UnsupportedReleaseRegionError, match="could not be read"):
        module.require_release_region("us-east-1", tmp_path / "absent.json")
    matrix = json.loads((PROJECT_ROOT / "release" / "agentcore-regions.json").read_text())
    for entry in matrix["regions"]:
        if entry["region"] == "us-west-1":
            entry["production_release"] = True
    mutated = tmp_path / "mutated.json"
    mutated.write_text(json.dumps(matrix))
    # The gate follows the file it is given; parity with the backend set is pinned separately,
    # so a mutation here is caught by test_every_copy_of_the_seven_is_the_same_set below.
    assert "us-west-1" in module.selected_release_regions(mutated)
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({**matrix, "schema_version": 1}))
    with pytest.raises(module.UnsupportedReleaseRegionError, match="schema version 2"):
        module.selected_release_regions(broken)


def test_cdk_app_refuses_an_unselected_region_before_synthesizing_anything(tmp_path):
    infra = PROJECT_ROOT / "infra"
    python = infra / ".venv" / "bin" / "python"
    if not python.exists():
        pytest.skip("infra virtualenv is not present on this host")
    source = (infra / "app.py").read_text(encoding="utf-8")
    assert source.index("require_release_region(aws_region)") < source.index("PlatformStack(")
    out = tmp_path / "cdk.out"
    result = subprocess.run(
        [str(python), "app.py"],
        cwd=infra,
        env={
            **os.environ,
            "CDK_OUTDIR": str(out),
            "CDK_CONTEXT_JSON": json.dumps(
                {"aws_region": "us-west-1", "environment_name": "production", "project_name": "acf"}
            ),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "aws_region=us-west-1 is not a selected production release Region" in result.stderr
    assert not list(out.glob("*.template.json")) if out.exists() else True


# --------------------------------------------------------------------------- evidence recorder matrix contract


def _evidence_module():
    path = PROJECT_ROOT / "scripts" / "release_evidence.py"
    spec = importlib.util.spec_from_file_location("release_evidence_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _matrix_mutation(mutate) -> dict:
    matrix = json.loads((PROJECT_ROOT / "release" / "agentcore-regions.json").read_text())
    mutate(matrix)
    return matrix


def test_evidence_matrix_contract_rejects_unreviewed_profile_shapes_and_loose_envelopes():
    module = _evidence_module()
    controls = json.loads((PROJECT_ROOT / "release" / "production-readiness-controls.json").read_text())
    baseline = json.loads((PROJECT_ROOT / "release" / "agentcore-regions.json").read_text())
    _control_map, region_map = module._validate_contracts(controls, baseline)
    assert {region for region, entry in region_map.items() if entry["production_release"]} == SEVEN

    def web_search_without_registry(matrix):
        for entry in matrix["regions"]:
            if entry["region"] == "eu-west-3":
                entry["web_search"] = True  # Paris: registry False + web_search True is an unreviewed shape

    def extra_envelope_key(matrix):
        matrix["production_envelope"]["experimental"] = True

    def missing_envelope_key(matrix):
        del matrix["production_envelope"]["gateway_provider"]

    def wrong_gateway_provider(matrix):
        matrix["production_envelope"]["gateway_provider"] = "litellm"

    def excluded_but_selected(matrix):
        for entry in matrix["regions"]:
            if entry["region"] == "us-west-1":
                entry["production_release"] = True

    def full_with_a_false_flag(matrix):
        for entry in matrix["regions"]:
            if entry["region"] == "us-east-1":
                entry["web_search"] = False

    def reduced_without_memory(matrix):
        for entry in matrix["regions"]:
            if entry["region"] == "eu-central-1":
                entry["memory"] = False

    def nothing_selected(matrix):
        for entry in matrix["regions"]:
            entry["production_release"] = False
            entry["profile"] = "excluded"

    for mutate in (
        web_search_without_registry,
        extra_envelope_key,
        missing_envelope_key,
        wrong_gateway_provider,
        excluded_but_selected,
        full_with_a_false_flag,
        reduced_without_memory,
        nothing_selected,
    ):
        with pytest.raises(module.EvidenceError):
            module._validate_contracts(controls, _matrix_mutation(mutate))


# --------------------------------------------------------------------------- one set, every copy


def test_every_copy_of_the_seven_is_the_same_set():
    """Backend, frontend, deploy.sh, the CDK matrix, and the CI strict-synth matrix must agree."""
    matrix = json.loads((PROJECT_ROOT / "release" / "agentcore-regions.json").read_text())
    from_matrix = {entry["region"] for entry in matrix["regions"] if entry["production_release"]}
    assert from_matrix == SEVEN

    frontend = (PROJECT_ROOT / "frontend" / "src" / "utils" / "awsRegion.ts").read_text(encoding="utf-8")
    block = re.search(r"SUPPORTED_SINGLE_REGION_RELEASES = \[(.*?)\] as const", frontend, re.S).group(1)
    assert set(re.findall(r'"([a-z0-9-]+)"', block)) == SEVEN

    deploy_sh = (PROJECT_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    listed = re.search(r"Supported single-Region deployments: ([a-z0-9-, ]+)\.", deploy_sh).group(1)
    assert {region.strip() for region in listed.split(",")} == SEVEN

    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "agentic-ai-self-service-ci.yml").read_text())
    assert set(workflow["jobs"]["strict-synth"]["strategy"]["matrix"]["region"]) == SEVEN

    assert _release_regions_module().selected_release_regions() == SEVEN
