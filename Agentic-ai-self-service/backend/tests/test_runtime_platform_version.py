"""AgentCore Runtime platform version (V1 default, V2 opt-in) across every surface it touches.

The contract under test:

* The Region set is exactly the ten Regions AWS documents for V2 (``runtime-how-it-works``,
  "Platform versions > Supported Regions", read 2026-11-12). Nothing is inferred from a
  neighbouring Region; London and Paris stay V1-only.
* ``RuntimeConfig.platformVersion`` accepts ``V1``/``V2``/absent and nothing else.
* A V2 request for a Region without V2 is refused before any AWS side effect: at the
  shared request model for an explicit ``targetRegion`` (422 through FastAPI) and at the
  live route for the resolved home Region (400, the same code the Web Search gate uses).
* ``create_agent_runtime`` forwards exactly what was asked -- ``platformVersion`` when set,
  and NOT EVEN THE KEY when unset, so the request to AWS is unchanged for every existing
  caller -- on both the create and the conflict-recovery update path.
* The CloudFormation export mirrors the live route: ``PlatformVersion`` present iff set.

Each refusal asserts the exact message so a wrong Region or a wrong feature name in the
error text is a failure, not a pass on "some 4xx".
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import yaml
from app import deployment_handler
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services import region_capabilities as capabilities
from app.services.cfn_template_generator import CfnTemplateGenerator
from app.services.runtime_deployer import create_agent_runtime
from fastapi import HTTPException
from pydantic import ValidationError

MODEL_ID = "us.anthropic.claude-sonnet-5"

#: The documented V2 Region list, spelled out here as the reviewed expectation rather than
#: read back from the module under test.
EXPECTED_V2_REGIONS = frozenset(
    {
        "ap-northeast-1",
        "ap-south-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "eu-central-1",
        "eu-north-1",
        "eu-west-1",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
)


# --------------------------------------------------------------------------- Region contract


def test_v2_region_set_is_exactly_the_documented_ten() -> None:
    assert capabilities.RUNTIME_PLATFORM_V2_SUPPORTED_REGIONS == EXPECTED_V2_REGIONS


@pytest.mark.parametrize("region", sorted(EXPECTED_V2_REGIONS))
def test_documented_regions_report_v2(region: str) -> None:
    assert capabilities.runtime_platform_v2_region_supported(region) is True
    assert capabilities.region_feature_profile(region).runtime_platform_v2 is True


@pytest.mark.parametrize("region", ["eu-west-2", "eu-west-3", "us-west-1", "ca-central-1", "eu-south-1", ""])
def test_other_regions_are_not_inferred_to_have_v2(region: str) -> None:
    assert capabilities.runtime_platform_v2_region_supported(region) is False
    assert capabilities.region_feature_profile(region).runtime_platform_v2 is False


def test_v2_availability_does_not_change_the_customer_facing_unsupported_feature_list() -> None:
    # London has Agent Registry but neither Web Search nor V2; only product features are listed.
    assert capabilities.region_feature_profile("eu-west-2").unsupported_features == (
        "Amazon Bedrock AgentCore Web Search",
    )


def test_release_selection_splits_into_v2_and_v1_only_regions() -> None:
    selected = capabilities.SUPPORTED_SINGLE_REGION_RELEASES
    assert selected & EXPECTED_V2_REGIONS == {"eu-central-1", "eu-west-1", "us-east-1", "us-east-2", "us-west-2"}
    assert selected - EXPECTED_V2_REGIONS == {"eu-west-2", "eu-west-3"}


def test_unavailable_message_names_the_region_the_default_and_only_selected_v2_alternatives() -> None:
    message = capabilities.feature_unavailable_message("runtime_platform_v2", "eu-west-2")
    assert message.startswith("AgentCore Runtime platform version V2 is not available in Europe (London) (eu-west-2).")
    assert "platform version V1 (the default)" in message
    alternatives = message.split("supported release Regions for V2:", 1)[1]
    for region in ("eu-central-1", "eu-west-1", "us-east-1", "us-east-2", "us-west-2"):
        assert region in alternatives
    # Documented V2 Regions that are NOT selected release Regions are never offered.
    for region in ("eu-north-1", "ap-south-1", "ap-southeast-1", "ap-southeast-2", "ap-northeast-1"):
        assert region not in alternatives
    assert "eu-west-2" not in alternatives


@pytest.mark.parametrize("value", [None, "V1"])
def test_require_accepts_v1_and_absent_everywhere(value: str | None) -> None:
    capabilities.require_runtime_platform_version_region(value, "eu-west-3")


def test_require_refuses_v2_outside_its_regions_with_the_exact_message() -> None:
    with pytest.raises(ValueError) as error:
        capabilities.require_runtime_platform_version_region("V2", "eu-west-3")
    assert str(error.value) == capabilities.feature_unavailable_message("runtime_platform_v2", "eu-west-3")


def test_require_admits_v2_in_a_documented_region() -> None:
    capabilities.require_runtime_platform_version_region("V2", "us-east-1")


def test_require_rejects_an_unknown_platform_version_as_a_contract_error() -> None:
    with pytest.raises(ValueError, match="Unknown AgentCore Runtime platform version: 'V3'"):
        capabilities.require_runtime_platform_version_region("V3", "us-east-1")


# --------------------------------------------------------------------------- request model


def _config(**extra) -> dict:
    return {"name": "platform_version_agent", "model": {"modelId": MODEL_ID}, **extra}


def _request(**extra) -> DeployRequest:
    return DeployRequest(nodeId="platform-version", config=_config(**extra.pop("config", {})), **extra)


def test_runtime_config_defaults_to_no_platform_version() -> None:
    assert RuntimeConfig(name="a", model={"modelId": MODEL_ID}).platform_version is None


@pytest.mark.parametrize("value", ["V1", "V2"])
def test_runtime_config_accepts_both_documented_versions_by_alias_and_by_name(value: str) -> None:
    assert (
        RuntimeConfig.model_validate(
            {"name": "a", "model": {"modelId": MODEL_ID}, "platformVersion": value}
        ).platform_version
        == value
    )
    assert RuntimeConfig(name="a", model={"modelId": MODEL_ID}, platform_version=value).platform_version == value


@pytest.mark.parametrize("value", ["v2", "V3", "2", "", "latest"])
def test_runtime_config_refuses_anything_but_v1_or_v2(value: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({"name": "a", "model": {"modelId": MODEL_ID}, "platformVersion": value})


def test_request_model_refuses_v2_for_an_explicit_v1_only_target_region() -> None:
    with pytest.raises(ValidationError) as error:
        _request(config={"platformVersion": "V2"}, targetRegion="eu-west-3")
    assert "AgentCore Runtime platform version V2 is not available in Europe (Paris) (eu-west-3)" in str(error.value)


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1", "eu-central-1"])
def test_request_model_admits_v2_for_an_explicit_documented_target_region(region: str) -> None:
    assert _request(config={"platformVersion": "V2"}, targetRegion=region).config.platform_version == "V2"


def test_request_model_leaves_an_omitted_target_region_to_the_live_route() -> None:
    # Export callers share this model and an omitted Region means nothing there.
    assert _request(config={"platformVersion": "V2"}).target_region is None


def test_request_model_does_not_region_gate_v1_or_absent() -> None:
    assert _request(config={"platformVersion": "V1"}, targetRegion="eu-west-3").config.platform_version == "V1"
    assert _request(targetRegion="eu-west-3").config.platform_version is None


def test_harness_requests_are_not_platform_version_gated() -> None:
    # A harness has no generated Runtime; the field is inert there and must not refuse.
    request = DeployRequest(
        nodeId="platform-version",
        deploymentMode="harness",
        config=_config(platformVersion="V2"),
        targetRegion="eu-west-3",
    )
    assert request.deployment_mode == "harness"


# --------------------------------------------------------------------------- live route


def _home(monkeypatch, region: str) -> None:
    monkeypatch.setattr(deployment_handler, "config", replace(deployment_handler.config, aws_region=region))


def test_live_route_refuses_v2_when_the_home_region_is_v1_only(monkeypatch) -> None:
    _home(monkeypatch, "eu-west-2")
    with pytest.raises(HTTPException) as error:
        deployment_handler._reject_unsupported_runtime_platform_version_region(
            _request(config={"platformVersion": "V2"})
        )
    assert error.value.status_code == 400
    assert str(error.value.detail) == capabilities.feature_unavailable_message("runtime_platform_v2", "eu-west-2")


def test_live_route_admits_v2_in_a_documented_home_region(monkeypatch) -> None:
    _home(monkeypatch, "us-east-1")
    deployment_handler._reject_unsupported_runtime_platform_version_region(_request(config={"platformVersion": "V2"}))


def test_live_route_prefers_an_explicit_target_region_over_the_home_region(monkeypatch) -> None:
    _home(monkeypatch, "eu-west-2")  # V1-only home, documented explicit target: admitted
    deployment_handler._reject_unsupported_runtime_platform_version_region(
        _request(config={"platformVersion": "V2"}, targetRegion="eu-west-1")
    )


@pytest.mark.parametrize("version", [None, "V1"])
def test_live_route_never_refuses_v1_or_absent(monkeypatch, version: str | None) -> None:
    _home(monkeypatch, "eu-west-3")
    config = {"platformVersion": version} if version else {}
    deployment_handler._reject_unsupported_runtime_platform_version_region(_request(config=config))


def test_live_route_gate_runs_before_any_write(monkeypatch) -> None:
    source = open(deployment_handler.__file__, encoding="utf-8").read()
    gate = source.index("_reject_unsupported_runtime_platform_version_region(request)")
    assert gate < source.index("store = _get_state_store()\n    store.create(state)")
    assert source.index("_reject_unsupported_managed_web_search_region(request)") < gate


# --------------------------------------------------------------------------- deployer wrapper


class _Control:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "agentRuntimeId": "runtime_pv_123",
            "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/runtime_pv_123",
            "status": "CREATING",
            "platformVersion": kwargs.get("platformVersion", "V1"),
        }


def _create(**extra) -> dict:
    control = _Control()
    create_agent_runtime(
        agentcore_ctrl=control,
        runtime_name="pv_runtime",
        role_arn="arn:aws:iam::123456789012:role/AgentCoreRuntime-pv",
        s3_bucket="artifacts",
        s3_key="code.zip",
        region="us-east-1",
        **extra,
    )
    assert len(control.calls) == 1
    return control.calls[0]


def test_wrapper_forwards_v2_verbatim() -> None:
    assert _create(platform_version="V2")["platformVersion"] == "V2"


def test_wrapper_forwards_v1_verbatim_when_explicitly_requested() -> None:
    assert _create(platform_version="V1")["platformVersion"] == "V1"


def test_wrapper_sends_no_platform_version_key_at_all_when_unset() -> None:
    # The request AWS sees must be unchanged for every caller that predates the field.
    assert "platformVersion" not in _create()
    assert "platformVersion" not in _create(platform_version=None)


def test_wrapper_source_forwards_the_same_value_on_the_conflict_update_path() -> None:
    import inspect

    import app.services.runtime_deployer as deployer

    source = inspect.getsource(deployer.create_agent_runtime)
    create_at = source.index('create_params["platformVersion"] = platform_version')
    update_at = source.index('update_params["platformVersion"] = platform_version')
    assert create_at < update_at
    # Both are conditional on the same request value, never a hard-coded version.
    assert source.count("if platform_version is not None:") == 2


# --------------------------------------------------------------------------- CloudFormation export


def _runtime_properties(**config_extra) -> dict:
    bundle = CfnTemplateGenerator().generate(_request(config=config_extra))
    template = yaml.safe_load(bundle.template_yaml)
    return template["Resources"]["AgentCoreRuntime"]["Properties"]


def test_export_mirrors_v2_on_the_runtime_resource() -> None:
    assert _runtime_properties(platformVersion="V2")["PlatformVersion"] == "V2"


def test_export_mirrors_an_explicit_v1() -> None:
    assert _runtime_properties(platformVersion="V1")["PlatformVersion"] == "V1"


def test_export_omits_the_property_when_unset_so_the_service_default_applies() -> None:
    assert "PlatformVersion" not in _runtime_properties()


def test_export_v2_is_region_gated_like_the_live_route() -> None:
    with pytest.raises(ValidationError):
        _request(config={"platformVersion": "V2"}, targetRegion="eu-west-2")
