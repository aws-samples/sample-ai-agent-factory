"""Model-provider API keys enter the platform once and leave only as ARNs."""

from __future__ import annotations

import logging

from app.routers import provider_credentials
from app.services.auth import get_caller_sub
from app.services.resource_ownership import owner_sub_hash
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

OWNER = "54381418-7021-708e-4f3b-30505a2b82ec"
REGION = "us-east-1"
ACCOUNT = "111122223333"


class _Secrets:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_secret(self, **kwargs):
        self.calls.append(kwargs)
        return {"ARN": (f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{kwargs['Name']}-AbCdEf")}


def _client(monkeypatch, secrets: _Secrets | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(provider_credentials.router)
    app.dependency_overrides[get_caller_sub] = lambda: OWNER
    monkeypatch.setenv("PROJECT_NAME", "provider-router-tests")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("APP_AWS_REGION", REGION)
    if secrets is not None:
        monkeypatch.setattr(
            provider_credentials.boto3,
            "client",
            lambda service, **kwargs: secrets,
        )
    return TestClient(app)


def test_store_returns_only_arn_and_binds_secret_to_caller_and_stack(monkeypatch):
    secrets = _Secrets()
    client = _client(monkeypatch, secrets)
    raw_key = "sk-fake-provider-key-never-echo"

    response = client.post(
        "/api/provider-credentials",
        json={"provider": "openai", "api_key": raw_key},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"secret_arn"}
    assert raw_key not in response.text

    assert len(secrets.calls) == 1
    call = secrets.calls[0]
    owner_hash = owner_sub_hash(OWNER)
    assert call["Name"].startswith(f"agentcore-provider/openai/{owner_hash}-")
    assert OWNER not in call["Name"]
    assert call["SecretString"] == raw_key
    tags = {tag["Key"]: tag["Value"] for tag in call["Tags"]}
    assert tags["AgentCoreStack"] == f"provider-router-tests-local-{REGION}"
    assert tags["ManagedBy"] == "agentcore-flows"
    assert tags["OwnerSubHash"] == owner_hash
    assert tags["Purpose"] == "model-provider-api-key"
    assert tags["Provider"] == "openai"
    assert OWNER not in tags.values()


def test_unknown_or_keyless_provider_is_rejected_before_aws(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        provider_credentials.boto3,
        "client",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("validation should run before AWS")),
    )

    response = client.post(
        "/api/provider-credentials",
        json={"provider": "bedrock", "api_key": "not-used"},
    )
    assert response.status_code == 422


def test_blank_key_is_rejected_before_aws(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        provider_credentials.boto3,
        "client",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("validation should run before AWS")),
    )

    response = client.post(
        "/api/provider-credentials",
        json={"provider": "openai", "api_key": "   "},
    )
    assert response.status_code == 422


def test_storage_failure_is_generic_and_never_logs_or_echoes_the_key(
    monkeypatch,
    caplog,
):
    raw_key = "sk-fake-key-that-must-not-leak"

    class _FailingSecrets:
        def create_secret(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                "CreateSecret",
            )

    client = _client(monkeypatch, _FailingSecrets())
    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/api/provider-credentials",
            json={"provider": "openai", "api_key": raw_key},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Could not store the model-provider credential"}
    assert raw_key not in response.text
    assert raw_key not in caplog.text
