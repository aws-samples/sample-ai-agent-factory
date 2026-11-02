"""Exact version-slot invocation and authority-chain tests.

The slot route must not accept any target selector from the caller.  It reads a
strongly-consistent slot, version, and deployment chain, freezes the target
session, then invokes the exact ARN returned by that chain.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.routers import versions as versions_router
from app.services import rbac
from app.services import runtime_target_context as rtc
from app.services.agent_versions_store import AgentVersion, RuntimeSlots
from app.services.auth import get_caller_sub
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
OTHER = "54381418-7021-708e-4f3b-30505a2b82ec"
NAME = "orders_probe"
V1 = "01a0d26eb4459a9516d12064ac4a8687"
V2 = "01a0d26f76e7ecc2611fea5a2f6852b3"
ACCOUNT = "222222222222"
REGION = "eu-west-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/AgentFactoryDeploymentRole"


def _runtime_id(version_id: str) -> str:
    return f"orders_{version_id[-8:]}"


def _runtime_arn(version_id: str) -> str:
    return f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{_runtime_id(version_id)}"


def _version(version_id: str, **overrides) -> AgentVersion:
    values = {
        "runtime_name": NAME,
        "version_id": version_id,
        "owner_sub": OWNER,
        "created_at": f"2026-09-25T12:00:0{version_id == V2}+00:00",
        "deployment_id": f"dep-{version_id[-8:]}",
        "agentcore_runtime_name": f"{NAME}_{version_id[-8:]}",
        "runtime_id": _runtime_id(version_id),
        "runtime_arn": _runtime_arn(version_id),
        "status": "succeeded",
    }
    values.update(overrides)
    return AgentVersion(**values)


def _deployment(version_id: str, **overrides) -> SimpleNamespace:
    values = {
        "deployment_id": f"dep-{version_id[-8:]}",
        "version_id": version_id,
        "runtime_id": _runtime_id(version_id),
        "runtime_arn": _runtime_arn(version_id),
        "user_id": OWNER,
        "status": "succeeded",
        "delete_status": None,
        "runtime_protocol": "HTTP",
        "deployment_mode": "runtime",
        "target_account_id": ACCOUNT,
        "target_region": REGION,
        "target_role_arn": ROLE,
        "gateway_result": None,
        "policy_result": None,
        "memory_result": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Agentcore:
    def __init__(self) -> None:
        self.invocations: list[dict] = []

    def invoke_agent_runtime(self, **kwargs):
        self.invocations.append(kwargs)
        runtime_id = kwargs["agentRuntimeArn"].rsplit("/", 1)[-1]
        return {
            "response": json.dumps({"response": f"answer:{runtime_id}"}).encode(),
            "runtimeSessionId": kwargs.get("runtimeSessionId") or f"session-{runtime_id}",
            "statusCode": 200,
        }


class _Session:
    def __init__(self, agentcore: _Agentcore) -> None:
        self.agentcore = agentcore
        self.client_calls: list[tuple[str, dict]] = []

    def client(self, service: str, **kwargs):
        self.client_calls.append((service, kwargs))
        if service != "bedrock-agentcore":
            raise AssertionError(f"unexpected service: {service}")
        return self.agentcore


class _Harness:
    def __init__(self, monkeypatch, *, owner: str = OWNER) -> None:
        self.slot = RuntimeSlots(
            runtime_name=NAME,
            owner_sub=owner,
            production_version_id=V1,
            staging_version_id=V2,
        )
        self.versions = {V1: _version(V1), V2: _version(V2)}
        self.deployments = {
            f"dep-{V1[-8:]}": _deployment(V1),
            f"dep-{V2[-8:]}": _deployment(V2),
        }
        self.slots_store = MagicMock()
        self.slots_store.get.side_effect = lambda runtime_name, **_kwargs: self.slot if runtime_name == NAME else None
        self.versions_store = MagicMock()
        self.versions_store.get.side_effect = lambda runtime_name, version_id, **_kwargs: (
            self.versions.get(version_id) if runtime_name == NAME else None
        )
        self.versions_store.list_for_runtime.side_effect = lambda runtime_name: (
            [self.versions[V2], self.versions[V1]] if runtime_name == NAME else []
        )
        self.deployments_store = MagicMock()
        self.deployments_store.get.side_effect = lambda deployment_id, **_kwargs: self.deployments.get(deployment_id)
        self.agentcore = _Agentcore()
        self.session = _Session(self.agentcore)
        self.target_events: list[dict] = []

        monkeypatch.setattr(rtc, "get_slots_store", lambda: self.slots_store)
        monkeypatch.setattr(rtc, "get_versions_store", lambda: self.versions_store)
        monkeypatch.setattr(rtc, "get_deployment_store", lambda: self.deployments_store)
        monkeypatch.setattr(
            rtc.step_clients,
            "session_for_event",
            lambda event: self.target_events.append(dict(event)) or self.session,
        )
        monkeypatch.setattr(versions_router, "get_slots_store", lambda: self.slots_store)
        monkeypatch.setattr(versions_router, "get_versions_store", lambda: self.versions_store)
        monkeypatch.setattr(
            versions_router,
            "get_deployment_store",
            lambda: self.deployments_store,
        )

        def _write(_runtime_name, *, expected, new, require_version):
            assert expected is self.slot
            assert require_version.version_id in self.versions
            self.slot = new

        monkeypatch.setattr(
            versions_router,
            "set_slot_pointers_atomically",
            _write,
        )
        monkeypatch.setattr(rbac, "has_scopes", lambda _request, _required: True)
        monkeypatch.setattr(
            versions_router,
            "workforce_token_expectations",
            lambda _region: ("https://issuer.example", "workforce-client"),
        )
        monkeypatch.setattr(versions_router, "reserve_invocation_budget", lambda **_kwargs: None)
        monkeypatch.setattr(versions_router, "settle_invocation_budget", lambda *_args, **_kwargs: None)

        app = FastAPI()
        app.include_router(versions_router.router)
        app.dependency_overrides[get_caller_sub] = lambda: OWNER
        self.client = TestClient(app)

    def invoke(self, slot: str, **body):
        payload = {"input": f"probe-{slot}", **body}
        return self.client.post(
            f"/api/runtimes/{NAME}/slots/{slot}/invoke",
            json=payload,
        )


@pytest.fixture
def harness(monkeypatch) -> _Harness:
    return _Harness(monkeypatch)


def test_production_and_staging_invoke_distinct_exact_targets(harness):
    production = harness.invoke("production")
    staging = harness.invoke("staging")

    assert production.status_code == 200, production.text
    assert staging.status_code == 200, staging.text
    assert production.json() == {
        "success": True,
        "response": f"answer:{_runtime_id(V1)}",
        "error": None,
        "sessionId": f"session-{_runtime_id(V1)}",
        "requestId": None,
        "arn": _runtime_arn(V1),
        "logs": None,
        "traceId": None,
        "toolReceipts": None,
        "identityEvidence": None,
        "runtimeName": NAME,
        "slot": "production",
        "versionId": V1,
        "deploymentId": f"dep-{V1[-8:]}",
        "runtimeId": _runtime_id(V1),
    }
    assert staging.json()["versionId"] == V2
    assert staging.json()["runtimeId"] == _runtime_id(V2)
    assert staging.json()["arn"] == _runtime_arn(V2)
    assert [call["agentRuntimeArn"] for call in harness.agentcore.invocations] == [
        _runtime_arn(V1),
        _runtime_arn(V2),
    ]


def test_only_a_production_gated_staging_route_marks_evaluation_content(harness):
    evaluation_key = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/evaluation-key"
    harness.versions[V2] = replace(
        harness.versions[V2],
        production_gate_required=True,
        evaluation_kms_key_arn=evaluation_key,
    )
    harness.deployments[f"dep-{V2[-8:]}"] = _deployment(
        V2,
        evaluation_kms_key_arn=evaluation_key,
    )

    production = harness.invoke("production", sessionId="production-session")
    staging = harness.invoke("staging", sessionId="staging-session")

    assert production.status_code == 200, production.text
    assert staging.status_code == 200, staging.text
    production_payload, staging_payload = [json.loads(call["payload"]) for call in harness.agentcore.invocations]
    assert "_agentcore_evaluation_capture" not in production_payload
    assert staging_payload["_agentcore_evaluation_capture"] is True
    assert staging_payload["prompt"] == "probe-staging"
    assert staging_payload["session_id"] == "staging-session"

    # The public schema is the authority boundary: a caller cannot turn a
    # production invocation into a content-bearing evaluation span.
    injected = harness.client.post(
        f"/api/runtimes/{NAME}/slots/production/invoke",
        json={
            "input": "must remain redacted",
            "_agentcore_evaluation_capture": True,
        },
    )
    assert injected.status_code == 422
    assert len(harness.agentcore.invocations) == 2


def test_ungated_staging_route_cannot_mark_evaluation_content(harness):
    response = harness.invoke("staging", sessionId="ungated-staging-session")

    assert response.status_code == 200, response.text
    payload = json.loads(harness.agentcore.invocations[-1]["payload"])
    assert "_agentcore_evaluation_capture" not in payload


@pytest.mark.parametrize("status_code", [500, 302, "200", True])
def test_explicit_unsuccessful_or_malformed_runtime_status_fails_closed(
    harness,
    status_code,
):
    secret_body = "internal-runtime-detail-that-must-not-leak"
    harness.agentcore.invoke_agent_runtime = MagicMock(
        return_value={
            "response": {"response": secret_body},
            "runtimeSessionId": "untrusted-session",
            "statusCode": status_code,
        }
    )

    response = harness.invoke("production")

    assert response.status_code == 200, response.text
    assert response.json()["success"] is False
    assert response.json()["response"] is None
    assert response.json()["error"] == "Runtime invocation failed."
    assert response.json()["sessionId"] is None
    assert secret_body not in response.text


def test_unsuccessful_runtime_stream_is_closed_without_reading_or_leaking(harness):
    response_stream = MagicMock()
    harness.agentcore.invoke_agent_runtime = MagicMock(
        return_value={
            "response": response_stream,
            "runtimeSessionId": "untrusted-session",
            "statusCode": 500,
        }
    )

    response = harness.invoke("production")

    assert response.status_code == 200, response.text
    assert response.json()["success"] is False
    assert response.json()["error"] == "Runtime invocation failed."
    response_stream.read.assert_not_called()
    response_stream.close.assert_called_once_with()


def test_structured_runtime_body_is_normalized_without_turning_success_into_an_error(
    harness,
):
    harness.agentcore.invoke_agent_runtime = MagicMock(
        return_value={
            "response": {"response": "structured-answer"},
            "runtimeSessionId": "structured-session",
            "statusCode": 200,
        }
    )

    response = harness.invoke("production")

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert response.json()["response"] == "structured-answer"
    assert response.json()["sessionId"] == "structured-session"


def test_target_account_region_and_session_are_frozen_by_the_deployment(harness):
    response = harness.invoke("production", sessionId="caller-session")

    assert response.status_code == 200, response.text
    assert harness.target_events == [
        {
            "target_account_id": ACCOUNT,
            "target_region": REGION,
            "target_role_arn": ROLE,
        }
    ]
    service, kwargs = harness.session.client_calls[-1]
    assert service == "bedrock-agentcore"
    assert kwargs["region_name"] == REGION
    invocation = harness.agentcore.invocations[-1]
    assert invocation["agentRuntimeArn"] == _runtime_arn(V1)
    assert invocation["runtimeSessionId"] == "caller-session"
    assert json.loads(invocation["payload"])["session_id"] == "caller-session"


def test_missing_slot_pointer_is_404_without_reading_a_version(harness):
    harness.slot = replace(harness.slot, staging_version_id=None)
    harness.versions_store.reset_mock()

    response = harness.invoke("staging")

    assert response.status_code == 404
    harness.versions_store.get.assert_not_called()
    assert harness.agentcore.invocations == []


def test_slot_owner_is_checked_before_any_downstream_authority_read(monkeypatch):
    harness = _Harness(monkeypatch, owner=OTHER)
    harness.versions_store.reset_mock()
    harness.deployments_store.reset_mock()

    response = harness.invoke("production")

    assert response.status_code == 404
    harness.versions_store.get.assert_not_called()
    harness.deployments_store.get.assert_not_called()
    assert harness.target_events == []
    assert harness.agentcore.invocations == []


@pytest.mark.parametrize(
    ("mutation", "expected_unread"),
    [
        (lambda h: h.versions.__setitem__(V1, _version(V1, status="failed")), "deployment"),
        (
            lambda h: h.deployments.__setitem__(
                f"dep-{V1[-8:]}",
                _deployment(V1, runtime_id="redirected-runtime"),
            ),
            "session",
        ),
    ],
)
def test_corrupt_version_or_deployment_binding_fails_closed(
    harness,
    mutation,
    expected_unread,
):
    mutation(harness)

    response = harness.invoke("production")

    assert response.status_code == 503
    assert harness.agentcore.invocations == []
    if expected_unread == "deployment":
        harness.deployments_store.get.assert_not_called()
    else:
        assert harness.target_events == []


def test_mcp_wrong_door_is_409_before_session_policy_or_invoke(harness):
    harness.deployments[f"dep-{V1[-8:]}"] = _deployment(
        V1,
        runtime_protocol="MCP",
        policy_result={
            "mode": "LOG_ONLY",
            "enforce_pending": {"engine_id": "eng-1"},
        },
    )

    response = harness.invoke("production")

    assert response.status_code == 409
    assert "MCP" in response.json()["detail"]
    assert harness.target_events == []
    assert harness.agentcore.invocations == []


@pytest.mark.parametrize(
    "forbidden",
    [
        {"runtimeId": "caller-target"},
        {"deploymentId": "caller-deployment"},
        {"arn": _runtime_arn(V2)},
        {"accountId": ACCOUNT},
        {"target_region": "us-east-1"},
        {"roleArn": ROLE},
        {"simulated": True},
        {"warmup": True},
        {"endpoint": "DEFAULT"},
    ],
)
def test_request_cannot_supply_any_target_authority(monkeypatch, forbidden):
    resolver = MagicMock()
    monkeypatch.setattr(versions_router, "resolve_owned_runtime_slot_target", resolver)
    monkeypatch.setattr(rbac, "has_scopes", lambda _request, _required: True)
    app = FastAPI()
    app.include_router(versions_router.router)
    app.dependency_overrides[get_caller_sub] = lambda: OWNER

    response = TestClient(app).post(
        f"/api/runtimes/{NAME}/slots/production/invoke",
        json={"input": "hi", **forbidden},
    )

    assert response.status_code == 422
    resolver.assert_not_called()


def test_stable_client_alias_invokes_exact_production_slot(harness):
    response = harness.client.post(
        f"/api/runtimes/{NAME}/invoke",
        json={"input": "hello from client"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["slot"] == "production"
    assert response.json()["versionId"] == V1
    assert response.json()["deploymentId"] == f"dep-{V1[-8:]}"
    assert harness.agentcore.invocations[-1]["agentRuntimeArn"] == _runtime_arn(V1)


def test_stable_client_alias_refuses_slot_or_target_injection(harness):
    before = list(harness.agentcore.invocations)
    response = harness.client.post(
        f"/api/runtimes/{NAME}/invoke",
        json={
            "input": "try staging",
            "slot": "staging",
            "runtimeId": V2,
        },
    )

    assert response.status_code == 422
    assert harness.agentcore.invocations == before


def test_slot_invoke_requires_the_invoke_scope(monkeypatch):
    monkeypatch.setenv("RBAC_ENFORCE", "true")
    resolver = MagicMock()
    monkeypatch.setattr(versions_router, "resolve_owned_runtime_slot_target", resolver)
    app = FastAPI()
    app.include_router(versions_router.router)
    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": OWNER,
                        "cognito:groups": [],
                    }
                }
            }
        }
    }

    async def _inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await app(scope, receive, send)

    response = TestClient(_inject).post(
        f"/api/runtimes/{NAME}/slots/production/invoke",
        json={"input": "hi"},
    )

    assert response.status_code == 403
    resolver.assert_not_called()


def test_promote_then_rollback_changes_the_actual_invoked_arn(harness):
    before = harness.invoke("production")
    staged = harness.invoke("staging")
    promoted = harness.client.post(
        f"/api/runtimes/{NAME}/versions/{V2}/promote",
        json={"slot": "production"},
    )
    after_promote = harness.invoke("production")
    rolled_back = harness.client.post(f"/api/runtimes/{NAME}/rollback")
    after_rollback = harness.invoke("production")

    assert before.json()["versionId"] == V1
    assert staged.json()["versionId"] == V2
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["promoted_version_id"] == V2
    assert after_promote.json()["versionId"] == V2
    assert after_promote.json()["deploymentId"] == f"dep-{V2[-8:]}"
    assert after_promote.json()["arn"] == _runtime_arn(V2)
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["promoted_version_id"] == V1
    assert after_rollback.json()["versionId"] == V1
    assert after_rollback.json()["deploymentId"] == f"dep-{V1[-8:]}"
    assert after_rollback.json()["arn"] == _runtime_arn(V1)
    assert [call["agentRuntimeArn"] for call in harness.agentcore.invocations] == [
        _runtime_arn(V1),
        _runtime_arn(V2),
        _runtime_arn(V2),
        _runtime_arn(V1),
    ]


def test_direct_slot_resolver_rejects_an_invalid_slot_before_any_read(monkeypatch):
    slots = MagicMock()
    monkeypatch.setattr(rtc, "get_slots_store", lambda: slots)

    with pytest.raises(HTTPException) as exc:
        rtc.resolve_owned_runtime_slot_target(NAME, "canary", OWNER)

    assert exc.value.status_code == 400
    slots.get.assert_not_called()


def _obo_token(
    *,
    exp: int | None = None,
    sub: str = OWNER,
    issuer: str = "https://issuer.example",
    client_id: str = "workforce-client",
    token_use: str = "access",
) -> str:
    import base64
    import time

    header = base64.urlsafe_b64encode(b'{"alg":"RS256","kid":"k"}').rstrip(b"=").decode()
    claims = {
        "sub": sub,
        "iss": issuer,
        "exp": exp if exp is not None else int(time.time()) + 300,
        "client_id": client_id,
        "token_use": token_use,
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


def _enable_obo(harness: _Harness) -> None:
    deployment = harness.deployments[f"dep-{V1[-8:]}"]
    deployment.gateway_result = {
        "obo_required": True,
        "client_info": {"provider": "workforce_jwt"},
    }
    deployment.runtime_endpoint = _runtime_arn(V1) + "/runtime-endpoint/DEFAULT"


def test_authorized_obo_slot_forwards_user_bearer_to_exact_version(harness, monkeypatch):
    from app.services import runtime_invocation

    _enable_obo(harness)
    token = _obo_token(exp=9999999999)
    captured = {}

    def _invoke(**kwargs):
        captured.update(kwargs)
        kwargs["on_dispatch"]()
        return {
            "statusCode": 200,
            "response": json.dumps(
                {
                    "response": "delegated answer",
                    "identity_evidence": {
                        "subject": OWNER,
                        "issuer": "https://issuer.example",
                        "expires_at": 9999999999,
                        "correlation_id": "trace-1",
                        "gateway_exchange": "ON_BEHALF_OF_TOKEN_EXCHANGE",
                        "downstream_principal": None,
                    },
                }
            ).encode(),
            "runtimeSessionId": "delegated-session",
        }

    monkeypatch.setattr(runtime_invocation, "invoke_agent_runtime_with_workforce_token", _invoke)
    response = harness.client.post(
        f"/api/runtimes/{NAME}/slots/production/invoke",
        json={"input": "who am I"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert response.json()["versionId"] == V1
    assert response.json()["identityEvidence"] == {
        "subject": OWNER,
        "issuer": "https://issuer.example",
        "expires_at": 9999999999,
        "correlation_id": "trace-1",
        "gateway_exchange": "ON_BEHALF_OF_TOKEN_EXCHANGE",
        "downstream_principal": None,
    }
    assert captured["runtime_arn"] == _runtime_arn(V1)
    assert captured["agentcore_client"] is harness.agentcore
    assert captured["deployment_state"]["runtime_endpoint"].endswith("/runtime-endpoint/DEFAULT")
    assert captured["bearer_token"] == token
    assert callable(captured["on_dispatch"])
    assert token not in response.text
    assert harness.agentcore.invocations == []  # signed wrapper was intercepted before the SDK call


@pytest.mark.parametrize(
    "authorization",
    [None, "Basic dXNlcjpwYXNz", "AWS4-HMAC-SHA256 Credential=AKIA/x", "Bearer junk"],
)
def test_unauthorized_obo_slot_never_invokes_runtime(harness, monkeypatch, authorization):
    from app.services import runtime_invocation

    _enable_obo(harness)
    invoke = MagicMock()
    monkeypatch.setattr(runtime_invocation, "invoke_agent_runtime_with_workforce_token", invoke)
    headers = {"Authorization": authorization} if authorization else {}

    response = harness.client.post(
        f"/api/runtimes/{NAME}/slots/production/invoke",
        json={"input": "who am I"},
        headers=headers,
    )

    assert response.status_code == 401
    invoke.assert_not_called()
    assert harness.agentcore.invocations == []


@pytest.mark.parametrize(
    "token",
    [
        _obo_token(sub=OTHER),
        _obo_token(issuer="https://other.example"),
        _obo_token(client_id="other-client"),
        _obo_token(token_use="id"),
    ],
)
def test_substituted_obo_claims_never_invoke_runtime(harness, monkeypatch, token):
    from app.services import runtime_invocation

    _enable_obo(harness)
    invoke = MagicMock()
    monkeypatch.setattr(runtime_invocation, "invoke_agent_runtime_with_workforce_token", invoke)

    response = harness.client.post(
        f"/api/runtimes/{NAME}/slots/production/invoke",
        json={"input": "who am I"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    invoke.assert_not_called()
    assert harness.agentcore.invocations == []


def test_slot_invocation_reserves_before_runtime_and_commits_success(harness, monkeypatch):
    reservation = object()
    reserve = MagicMock(return_value=reservation)
    settle = MagicMock()
    monkeypatch.setattr(versions_router, "reserve_invocation_budget", reserve)
    monkeypatch.setattr(versions_router, "settle_invocation_budget", settle)

    response = harness.invoke("production")

    assert response.status_code == 200
    reserve.assert_called_once_with(
        owner_sub=OWNER,
        runtime_name=NAME,
        resource_tags={},
    )
    settle.assert_called_once_with(reservation, dispatched=True)
    assert len(harness.agentcore.invocations) == 1


def test_slot_budget_429_never_invokes_runtime(harness, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        versions_router,
        "reserve_invocation_budget",
        MagicMock(side_effect=HTTPException(status_code=429, detail="hard budget exhausted")),
    )
    settle = MagicMock()
    monkeypatch.setattr(versions_router, "settle_invocation_budget", settle)

    response = harness.invoke("production")

    assert response.status_code == 429
    assert harness.agentcore.invocations == []
    settle.assert_not_called()


def test_failed_slot_response_still_commits_dispatched_reservation(harness, monkeypatch):
    reservation = object()
    monkeypatch.setattr(versions_router, "reserve_invocation_budget", lambda **_kwargs: reservation)
    settle = MagicMock()
    monkeypatch.setattr(versions_router, "settle_invocation_budget", settle)
    harness.agentcore.invoke_agent_runtime = MagicMock(return_value={"statusCode": 500, "response": b"internal"})

    response = harness.invoke("production")

    assert response.status_code == 200
    assert response.json()["success"] is False
    settle.assert_called_once_with(reservation, dispatched=True)


def test_slot_failure_before_dispatch_releases_reservation(harness, monkeypatch):
    reservation = object()
    monkeypatch.setattr(versions_router, "reserve_invocation_budget", lambda **_kwargs: reservation)
    settle = MagicMock()
    monkeypatch.setattr(versions_router, "settle_invocation_budget", settle)
    monkeypatch.setattr(
        versions_router,
        "invoke_verified_http_runtime",
        MagicMock(side_effect=HTTPException(status_code=503, detail="local validation failed")),
    )

    response = harness.invoke("production")

    assert response.status_code == 503
    settle.assert_called_once_with(reservation, dispatched=False)
    assert harness.agentcore.invocations == []
