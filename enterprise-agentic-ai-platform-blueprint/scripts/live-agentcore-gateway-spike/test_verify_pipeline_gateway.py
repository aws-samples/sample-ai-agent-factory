"""Tests for the read-only pipeline Gateway verifier."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway_spike import SpikeError
from verify_pipeline_gateway import PipelineGatewayVerifier, REQUIRED_TAGS


class FakeEvidence:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def add(self, name: str, **details) -> None:
        self.events.append((name, details))


class FakeControl:
    def __init__(self, environment: str) -> None:
        self.environment = environment

    @staticmethod
    def get_gateway(**_kwargs) -> dict:
        return {
            "status": "READY",
            "authorizerType": "CUSTOM_JWT",
            "gatewayArn": "arn:aws:bedrock-agentcore:eu-west-1:111111111111:gateway/example",
            "ResponseMetadata": {"RequestId": "gateway-request"},
        }

    def list_tags_for_resource(self, **_kwargs) -> dict:
        return {
            "tags": {
                **{key: "value" for key in REQUIRED_TAGS},
                "environment": self.environment,
            }
        }

    @staticmethod
    def get_gateway_target(**_kwargs) -> dict:
        return {
            "status": "READY",
            "name": "agenticai-inference-nonprod-bedrock",
            "credentialProviderConfigurations": [
                {"credentialProviderType": "GATEWAY_IAM_ROLE"}
            ],
            "ResponseMetadata": {"RequestId": "target-request"},
        }

    @staticmethod
    def get_gateway_rate_limit(**_kwargs) -> dict:
        return {
            "status": "ACTIVE",
            "ResponseMetadata": {"RequestId": "rate-limit-request"},
        }


def verifier(*, actual: str, expected: str) -> PipelineGatewayVerifier:
    instance = object.__new__(PipelineGatewayVerifier)
    instance.expected_environment = expected
    instance.outputs = {
        "GatewayIdentifier": "gateway-id",
        "GatewayArn": "arn:aws:bedrock-agentcore:eu-west-1:111111111111:gateway/example",
        "InferenceTargetId": "target-id",
        "InferenceTargetName": "agenticai-inference-nonprod-bedrock",
        "RateLimitId": "rate-limit-id",
    }
    instance.config = SimpleNamespace(
        model="agenticai-inference-nonprod-bedrock/openai.gpt-oss-120b"
    )
    instance.control = FakeControl(actual)
    instance.evidence = FakeEvidence()
    return instance


@pytest.mark.parametrize("environment", ["nonprod", "prod"])
def test_control_plane_accepts_explicit_matching_environment(environment: str) -> None:
    subject = verifier(actual=environment, expected=environment)
    subject.verify_control_plane()
    assert subject.evidence.events[-1][0] == "pipeline_control_plane_verified"


def test_control_plane_rejects_environment_mismatch() -> None:
    subject = verifier(actual="nonprod", expected="prod")
    with pytest.raises(SpikeError, match="explicit expected environment"):
        subject.verify_control_plane()
