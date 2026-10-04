"""F-10: the registry approval gate fails CLOSED on every failure, not only on the one it named.

``handle_deploy`` mapped ``RegistryQueryFailed`` to a 503, then caught every OTHER exception,
logged "integration gating skipped (registry check errored)" and let the deploy proceed. A
DynamoDB throttle inside a registry provider, a JSON decode error from a LiteLLM catalog, or an
attribute error in a provider's record shape silently removed the governance control. ARCC
cnt_dwzZ05hLnqhYXQ: an authorization decision must not default to allow.

These tests drive the REAL ``handle_deploy`` up to and through the gate. Everything ahead of the
gate is stubbed to pass; everything after it is a tripwire that fails the test if reached. The
positive control (a clean ``[]`` verdict trips the wire) proves the harness gets as far as the
gate, so a 503 in the other cases is the gate's doing and nothing upstream.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from app import deployment_handler as dh
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services import aws_agent_registry as ar
from botocore.exceptions import ClientError
from fastapi import HTTPException

MODEL_ID = "us.anthropic.claude-sonnet-5"


class _Proceeded(AssertionError):
    """Raised by every post-gate collaborator: reaching one means the deploy went ahead."""


def _request() -> DeployRequest:
    return DeployRequest(
        nodeId="node-1",
        config=RuntimeConfig(name="Gate Test", model={"modelId": MODEL_ID}),
        mcpServerConfig={"endpoint": "https://mcp.example/mcp"},
    )


class _RawRequest:
    def __init__(self) -> None:
        self.scope = {"aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "user-a"}}}}}}


@pytest.fixture()
def harness(monkeypatch):
    """Stub the pre-gate guards to pass and arm the post-gate tripwires."""

    async def _ok(_raw):
        return None

    monkeypatch.setattr(dh, "_reject_invalid_deploy_request", _ok)
    monkeypatch.setattr(dh, "_get_user_id", lambda _raw: "user-a")
    monkeypatch.setattr(dh, "_reject_unowned_flow", lambda *_a, **_k: None)
    monkeypatch.setattr(dh, "_reject_cfn_only_naming_profile", lambda *_a, **_k: None)
    monkeypatch.setattr(dh, "_reject_missing_provider_credential", lambda *_a, **_k: None)
    monkeypatch.setattr(dh, "_reject_mcp_inheriting_platform_otel", lambda *_a, **_k: None)

    def _tripwire(*_a, **_k):
        raise _Proceeded("the deploy proceeded past the registry gate")

    monkeypatch.setattr(dh, "_get_state_store", _tripwire)
    import app.services.agent_versions_store as avs

    monkeypatch.setattr(avs, "get_slots_store", _tripwire)
    monkeypatch.setattr(avs, "get_versions_store", _tripwire, raising=False)

    def _run(verdict):
        monkeypatch.setattr(ar, "unapproved_integrations", verdict)
        return asyncio.run(dh.handle_deploy(_request(), _RawRequest()))

    return _run


def test_positive_control_a_clean_verdict_reaches_the_tripwire(harness):
    """Without this the tests below could pass because the handler never reached the gate."""
    with pytest.raises(_Proceeded):
        harness(lambda _idents: [])


FAILURES = {
    "dynamodb throttle": lambda _i: (_ for _ in ()).throw(
        ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Query",
        )
    ),
    "json decode error": lambda _i: json.loads("{not json"),
    "attribute error in a provider": lambda _i: (None).status,  # type: ignore[attr-defined]
    "key error in a record shape": lambda _i: {}["records"],
    "type error": lambda _i: len(5),  # type: ignore[arg-type]
    "bare runtime error": lambda _i: (_ for _ in ()).throw(RuntimeError("provider exploded")),
}


@pytest.mark.parametrize("failure", sorted(FAILURES), ids=lambda k: k.replace(" ", "-"))
def test_any_gating_failure_is_a_503_and_the_deploy_does_not_proceed(harness, failure):
    with pytest.raises(HTTPException) as ei:
        harness(FAILURES[failure])
    assert ei.value.status_code == 503, f"{failure}: a gating failure must be an unknown verdict, not a skip"
    detail = str(ei.value.detail)
    assert "unknown" in detail.lower(), detail
    assert "Refusing the deploy" in detail, detail
    assert "not APPROVED" not in detail, "must not blame the caller's integrations for a platform fault"


def test_the_failure_reason_is_the_exception_type_not_its_text(harness):
    """A provider's raw message can echo request parameters; only the class name is served."""
    with pytest.raises(HTTPException) as ei:
        harness(lambda _i: (_ for _ in ()).throw(RuntimeError("FAKE-secret-in-message-1234")))
    assert "FAKE-secret-in-message-1234" not in str(ei.value.detail)
    assert "RuntimeError" in str(ei.value.detail)


def test_registry_query_failed_keeps_its_own_more_specific_503(harness):
    with pytest.raises(HTTPException) as ei:
        harness(lambda _i: (_ for _ in ()).throw(ar.RegistryQueryFailed("AccessDenied: ListRegistryRecords")))
    assert ei.value.status_code == 503
    assert "ListRegistryRecords" in str(ei.value.detail)


def test_a_genuine_denial_is_still_403(harness):
    with pytest.raises(HTTPException) as ei:
        harness(lambda idents: list(idents))
    assert ei.value.status_code == 403
    assert "not APPROVED" in str(ei.value.detail)


def test_federation_not_configured_is_the_only_skip(harness, monkeypatch):
    """The documented skip: ``unapproved_integrations`` answers ``[]`` when no registry is
    configured. It is a real verdict from the real function, not an exception path."""
    monkeypatch.setattr(ar, "get_registry", lambda: None)
    with pytest.raises(_Proceeded):
        harness(ar.unapproved_integrations)


def test_the_skip_wording_is_gone_from_the_handler():
    """The log line that documented the fail-open behaviour must not come back."""
    import inspect

    src = inspect.getsource(dh.handle_deploy)
    assert "integration gating skipped" not in src
