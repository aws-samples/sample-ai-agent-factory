"""MCP runtimes refuse HTTP-envelope triggers before every side effect.

The trigger dispatcher invokes an AgentCore HTTP agent with a prompt payload.
A persisted MCP runtime speaks bearer-authenticated JSON-RPC instead, so
registering any trigger type for it would create durable infrastructure that
can never deliver successfully.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.models.deployment_models import (
    DeploymentState,
    DeploymentStatusEnum,
)
from app.routers import triggers
from app.services import runtime_target_context
from app.services.agent_versions_store import AgentVersion, RuntimeSlots
from app.services.auth import get_caller_sub
from app.services.trigger_store import RuntimeClaim
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

_OWNER = "sub-alice"
_RUNTIME_NAME = "standalone_mcp"
_DEPLOYMENT_ID = "dep-standalone-mcp"
_VERSION_ID = "v1"
_RUNTIME_ID = "standalone_mcp-AbCdEf1234"
_RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{_RUNTIME_ID}"


def _claim(*, owner: str = _OWNER) -> RuntimeClaim:
    return RuntimeClaim(
        slot=RuntimeSlots(
            runtime_name=_RUNTIME_NAME,
            owner_sub=owner,
            production_version_id=_VERSION_ID,
        ),
        version=AgentVersion(
            runtime_name=_RUNTIME_NAME,
            version_id=_VERSION_ID,
            owner_sub=owner,
            created_at=datetime.now(timezone.utc).isoformat(),
            deployment_id=_DEPLOYMENT_ID,
            agentcore_runtime_name="standalone_mcp",
            runtime_id=_RUNTIME_ID,
            runtime_arn=_RUNTIME_ARN,
            status="succeeded",
        ),
        target_runtime_arn=_RUNTIME_ARN,
    )


def _mcp_deployment() -> DeploymentState:
    return DeploymentState(
        deployment_id=_DEPLOYMENT_ID,
        user_id=_OWNER,
        status=DeploymentStatusEnum.SUCCEEDED,
        started_at=datetime.now(timezone.utc),
        runtime_id=_RUNTIME_ID,
        runtime_arn=_RUNTIME_ARN,
        version_id=_VERSION_ID,
        runtime_protocol="MCP",
        target_region="us-east-1",
    )


def _client(caller_sub: str = _OWNER) -> TestClient:
    app = FastAPI()
    app.include_router(triggers.router)
    app.dependency_overrides[get_caller_sub] = lambda: caller_sub
    return TestClient(app, raise_server_exceptions=False)


def _wire_mcp_authority(monkeypatch) -> MagicMock:
    """Cover the neutral store seam and the local aliases a router may import."""

    deployment_store = MagicMock()
    deployment_store.get.return_value = _mcp_deployment()
    monkeypatch.setattr(
        runtime_target_context,
        "_deployment_store",
        deployment_store,
    )
    monkeypatch.setattr(
        triggers,
        "get_deployment_store",
        lambda: deployment_store,
        raising=False,
    )
    monkeypatch.setattr(
        triggers,
        "_get_deployment_store",
        lambda: deployment_store,
        raising=False,
    )
    monkeypatch.setattr(
        triggers,
        "DeploymentStateStore",
        lambda *_args, **_kwargs: deployment_store,
        raising=False,
    )
    monkeypatch.setattr(
        triggers,
        "resolve_owned_deployment_runtime_target",
        lambda *_args, **_kwargs: SimpleNamespace(protocol="MCP"),
        raising=False,
    )
    return deployment_store


_VALID_TRIGGER_REQUESTS = [
    pytest.param(
        {"type": "cron", "schedule": "cron(0 12 * * ? *)"},
        id="cron",
    ),
    pytest.param(
        {
            "type": "eventbridge",
            "pattern": {"source": ["aws.ec2"]},
        },
        id="eventbridge",
    ),
    pytest.param(
        {
            "type": "s3",
            "pattern": {"source": ["aws.s3"]},
        },
        id="s3",
    ),
    pytest.param({"type": "webhook"}, id="webhook"),
]


@pytest.mark.parametrize("request_body", _VALID_TRIGGER_REQUESTS)
def test_every_trigger_type_refuses_mcp_before_side_effects(
    monkeypatch,
    request_body,
):
    monkeypatch.setattr(
        triggers,
        "_resolve_owned_runtime_claim",
        lambda *_args, **_kwargs: _claim(),
    )
    _wire_mcp_authority(monkeypatch)

    trigger_store = MagicMock(side_effect=AssertionError("trigger persistence was reached"))
    webhook_secret = MagicMock(side_effect=AssertionError("webhook secret creation was reached"))
    aws_client = MagicMock(side_effect=AssertionError("an AWS trigger side effect was reached"))
    provision = MagicMock(side_effect=AssertionError("trigger provisioning was reached"))
    monkeypatch.setattr(triggers, "get_trigger_store", trigger_store)
    monkeypatch.setattr(triggers, "_store_webhook_secret", webhook_secret)
    monkeypatch.setattr(triggers.boto3, "client", aws_client)
    monkeypatch.setattr(triggers, "provision_trigger", provision)

    response = _client().post(
        f"/api/runtimes/{_RUNTIME_NAME}/triggers",
        json=request_body,
    )

    assert response.status_code == 409, response.text
    detail = str(response.json().get("detail", "")).lower()
    assert "mcp" in detail
    assert "trigger" in detail
    trigger_store.assert_not_called()
    webhook_secret.assert_not_called()
    aws_client.assert_not_called()
    provision.assert_not_called()


def test_protocol_authority_failure_is_fail_closed_before_side_effects(
    monkeypatch,
):
    """An unreadable protocol cannot be guessed as HTTP.

    DeploymentState is created before the AgentVersion row and the protocol read is
    strongly consistent, so a missing/unreadable row is not an eventual-consistency
    success case. Falling through would provision durable HTTP trigger infrastructure
    for a runtime that may actually be MCP, precisely the broken configuration this
    gate exists to prevent.
    """

    monkeypatch.setattr(
        triggers,
        "_resolve_owned_runtime_claim",
        lambda *_args, **_kwargs: _claim(),
    )
    protocol_lookup = MagicMock(
        side_effect=HTTPException(
            status_code=503,
            detail="Could not verify this resource right now. Try again shortly.",
        )
    )
    monkeypatch.setattr(
        triggers,
        "resolve_owned_deployment_runtime_target",
        protocol_lookup,
    )

    trigger_store = MagicMock(side_effect=AssertionError("trigger persistence was reached"))
    webhook_secret = MagicMock(side_effect=AssertionError("webhook secret creation was reached"))
    aws_client = MagicMock(side_effect=AssertionError("an AWS trigger side effect was reached"))
    provision = MagicMock(side_effect=AssertionError("trigger provisioning was reached"))
    monkeypatch.setattr(triggers, "get_trigger_store", trigger_store)
    monkeypatch.setattr(triggers, "_store_webhook_secret", webhook_secret)
    monkeypatch.setattr(triggers.boto3, "client", aws_client)
    monkeypatch.setattr(triggers, "provision_trigger", provision)

    response = _client().post(
        f"/api/runtimes/{_RUNTIME_NAME}/triggers",
        json={"type": "cron", "schedule": "cron(0 12 * * ? *)"},
    )

    assert response.status_code == 503, response.text
    protocol_lookup.assert_called_once_with(_DEPLOYMENT_ID, _OWNER)
    trigger_store.assert_not_called()
    webhook_secret.assert_not_called()
    aws_client.assert_not_called()
    provision.assert_not_called()


def test_request_validation_precedes_the_mcp_protocol_refusal(monkeypatch):
    monkeypatch.setattr(
        triggers,
        "_resolve_owned_runtime_claim",
        lambda *_args, **_kwargs: _claim(),
    )
    deployment_store = _wire_mcp_authority(monkeypatch)
    deployment_store.get.side_effect = AssertionError("protocol lookup ran before request validation")

    response = _client().post(
        f"/api/runtimes/{_RUNTIME_NAME}/triggers",
        json={"type": "cron"},
    )

    assert response.status_code == 400, response.text
    assert "schedule" in str(response.json().get("detail", "")).lower()
    deployment_store.get.assert_not_called()


def test_ownership_precedes_the_mcp_protocol_refusal(monkeypatch):
    def _not_owned(*_args, **_kwargs):
        raise HTTPException(status_code=404, detail="Not found")

    monkeypatch.setattr(
        triggers,
        "_resolve_owned_runtime_claim",
        _not_owned,
    )
    deployment_store = _wire_mcp_authority(monkeypatch)
    deployment_store.get.side_effect = AssertionError("protocol lookup disclosed a foreign runtime")

    response = _client("sub-mallory").post(
        f"/api/runtimes/{_RUNTIME_NAME}/triggers",
        json={"type": "cron", "schedule": "cron(0 12 * * ? *)"},
    )

    assert response.status_code == 404, response.text
    deployment_store.get.assert_not_called()


def test_mcp_trigger_list_remains_available_for_cleanup(monkeypatch):
    monkeypatch.setattr(
        triggers,
        "_resolve_owned_runtime_claim",
        lambda *_args, **_kwargs: _claim(),
    )
    deployment_store = _wire_mcp_authority(monkeypatch)
    trigger_store = MagicMock()
    trigger_store.list_for_runtime.return_value = []
    monkeypatch.setattr(triggers, "get_trigger_store", lambda: trigger_store)

    response = _client().get(
        f"/api/runtimes/{_RUNTIME_NAME}/triggers",
    )

    assert response.status_code == 200, response.text
    assert response.json() == []
    trigger_store.list_for_runtime.assert_called_once_with(_RUNTIME_NAME)
    deployment_store.get.assert_not_called()
