"""An audience-bound external IdP has to be deployable, and its audience has to stick.

Both authorizer builders sent ``allowedAudiences``. The service model's member is
``allowedAudience`` (a list), so botocore's param validation rejected the whole
CreateGateway / UpdateAgentRuntime the moment an operator configured an audience: the
feature could never deploy. These validate against the INSTALLED botocore model, not
against a hand-written dict, and the first test is the baseline that proves the
validator used here does reject the old key.
"""

from __future__ import annotations

import socket
import urllib.request
from io import BytesIO
from unittest.mock import MagicMock, patch

import botocore.session
import pytest
from app.services import gateway_deployer as gd
from botocore.validate import ParamValidator

AUDIENCE = "api://orders-agents"
DISCOVERY = "https://idp.example.com/.well-known/openid-configuration"
_PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))]


def _errors(operation: str, params: dict) -> str:
    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    report = ParamValidator().validate(params, model.operation_model(operation).input_shape)
    return report.generate_report() if report.has_errors() else ""


def _create_gateway_params(authorizer: dict) -> dict:
    return {
        "name": "orders",
        "roleArn": "arn:aws:iam::123456789012:role/AgentCoreGateway-orders",
        "protocolType": "MCP",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": authorizer,
    }


def _authorizer(key: str) -> dict:
    return {"customJWTAuthorizer": {"discoveryUrl": DISCOVERY, "allowedClients": ["cid"], key: [AUDIENCE]}}


@pytest.mark.parametrize("operation", ["CreateGateway", "UpdateGateway"])
def test_baseline_the_model_rejects_the_plural_and_accepts_the_singular(operation):
    extra = {"gatewayIdentifier": "gw-1"} if operation == "UpdateGateway" else {}
    assert "allowedAudiences" in _errors(
        operation, {**extra, **_create_gateway_params(_authorizer("allowedAudiences"))}
    )
    assert _errors(operation, {**extra, **_create_gateway_params(_authorizer("allowedAudience"))}) == ""


def _external_config(audience: str | None):
    doc = BytesIO(b'{"token_endpoint": "https://idp.example.com/oauth2/token"}')
    identity = {"provider": "custom", "client_id": "cid", "discovery_url": DISCOVERY}
    if audience is not None:
        identity["audience"] = audience
    with (
        patch("socket.getaddrinfo", return_value=_PUBLIC),
        patch.object(
            urllib.request, "urlopen", return_value=MagicMock(__enter__=lambda s: doc, __exit__=lambda *a: None)
        ),
    ):
        return gd._create_external_oauth_config(identity, region="us-east-1")


def test_an_external_idp_audience_builds_a_valid_gateway_authorizer():
    out = _external_config(AUDIENCE)
    authorizer = out["authorizer_config"]
    assert _errors("CreateGateway", _create_gateway_params(authorizer)) == ""
    assert authorizer["customJWTAuthorizer"]["allowedAudience"] == [AUDIENCE]
    assert out["client_info"]["audience"] == AUDIENCE


def test_no_audience_sends_no_audience_member():
    out = _external_config(None)
    assert "allowedAudience" not in out["authorizer_config"]["customJWTAuthorizer"]
    assert _errors("CreateGateway", _create_gateway_params(out["authorizer_config"])) == ""


def test_deploy_gateway_sends_a_create_request_the_model_accepts():
    """Through deploy_gateway, so the builder's output is what reaches the client."""
    sent: list[dict] = []

    class _Ctrl(MagicMock):
        pass

    ctrl = _Ctrl()
    ctrl.list_gateways.return_value = {"items": []}

    def _create_gateway(**kw):
        sent.append(kw)
        raise RuntimeError("stop after the create request")

    ctrl.create_gateway.side_effect = _create_gateway
    iam = MagicMock()
    iam.exceptions.EntityAlreadyExistsException = type("E", (Exception,), {})
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/AgentCoreGateway-orders"}}
    with (
        patch.object(gd, "_create_agentcore_control_client", return_value=ctrl),
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd, "_create_cognito_client", return_value=MagicMock()),
        patch.object(gd.boto3, "client", return_value=MagicMock()),
        patch.object(gd.time, "sleep"),
        patch.object(gd, "cleanup_gateway_resources", return_value=[]),
        patch.object(gd, "_create_external_oauth_config", return_value=_external_config(AUDIENCE)),
    ):
        out = gd.deploy_gateway(
            {"name": "orders"},
            "us-east-1",
            identity_config={
                "provider": "custom",
                "client_id": "cid",
                "discovery_url": DISCOVERY,
                "audience": AUDIENCE,
            },
        )

    assert out["success"] is False and "stop after the create request" in out["error"]
    assert len(sent) == 1
    assert _errors("CreateGateway", sent[0]) == ""
    assert sent[0]["authorizerConfiguration"]["customJWTAuthorizer"]["allowedAudience"] == [AUDIENCE]


def test_configure_jwt_auth_sends_an_update_the_model_accepts(monkeypatch):
    ctrl = MagicMock()
    ctrl.get_agent_runtime.return_value = {
        "status": "READY",
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/a:1"}
        },
        "roleArn": "arn:aws:iam::123456789012:role/rt",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
    }
    monkeypatch.setattr(gd, "_create_agentcore_control_client", lambda region: ctrl)
    monkeypatch.setattr(gd.time, "sleep", lambda *_a: None)
    client_info = {"provider": "custom", "client_id": "cid", "discovery_url": DISCOVERY, "audience": AUDIENCE}

    out = gd.configure_jwt_auth("rt-1", {"client_info": client_info}, "us-east-1")

    assert out["success"] is True, out
    sent = ctrl.update_agent_runtime.call_args.kwargs
    assert _errors("UpdateAgentRuntime", sent) == ""
    assert sent["authorizerConfiguration"]["customJWTAuthorizer"]["allowedAudience"] == [AUDIENCE]
