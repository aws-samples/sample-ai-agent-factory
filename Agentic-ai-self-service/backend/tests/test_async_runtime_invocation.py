"""Authenticated async Runtime turns escape API Gateway's timeout without replay.

The start route authorizes the runtime before writing, the worker revalidates
ownership before dispatch, and the poll/delete routes bind every scratch row to
its caller.  These tests pin those boundaries and the 540-second worker ceiling.
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import nullcontext

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from app import deployment_handler as dh  # noqa: E402
from app.models.deployment_models import TestRequest as RuntimeTestRequest  # noqa: E402
from app.models.deployment_models import TestResponse as RuntimeTestResponse  # noqa: E402
from app.services import runtime_invocation, step_clients  # noqa: E402

OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
OTHER = "54381418-7021-708e-4f3b-30505a2b82ec"
RUNTIME = "managed_search_runtime_123"
TEST_ID = "runtime-test-abc123def456"


def _client(sub: str | None = OWNER) -> TestClient:
    claims = {"cognito:groups": ["g-users-default"], **({"sub": sub} if sub else {})}
    event = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}

    async def inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await dh.deployment_app(scope, receive, send)

    return TestClient(inject)


def _record(owner: str = OWNER, **extra) -> dict:
    value = {
        "agentcore_runtime_name": "managed_search_runtime",
        "deployment_id": "dep-1",
        "resource_tags": {},
        "runtime_arn": f"arn:aws:bedrock-agentcore:{dh.config.aws_region}:123456789012:runtime/{RUNTIME}",
        "runtime_id": RUNTIME,
        "runtime_protocol": "HTTP",
        "status": "succeeded",
        "user_id": owner,
    }
    value.update(extra)
    return value


class Table:
    def __init__(self, item: dict | None = None):
        self.item = item
        self.puts: list[dict] = []
        self.gets: list[dict] = []
        self.deletes: list[dict] = []
        self.updates: list[dict] = []
        self.update_errors: list[Exception | None] = []
        self.delete_error: Exception | None = None

    def put_item(self, **kwargs):
        self.puts.append(kwargs)
        return {}

    def get_item(self, **kwargs):
        self.gets.append(kwargs)
        return {"Item": self.item} if self.item else {}

    def delete_item(self, **kwargs):
        self.deletes.append(kwargs)
        if self.delete_error:
            raise self.delete_error
        return {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        if self.update_errors:
            error = self.update_errors.pop(0)
            if error is not None:
                raise error
        return {}


@pytest.fixture
def table(monkeypatch):
    value = Table()
    monkeypatch.setattr(dh, "_get_deploy_table", lambda: value)
    return value


@pytest.fixture
def owned_runtime(monkeypatch):
    monkeypatch.setattr(dh, "_require_async_runtime_deployment", lambda runtime_id, caller_sub: _record())


@pytest.fixture
def accepted_lambda(monkeypatch):
    calls: list[dict] = []

    class Lambda:
        def invoke(self, **kwargs):
            calls.append(kwargs)
            return {"StatusCode": 202}

    monkeypatch.setattr(dh.boto3, "client", lambda service, **kwargs: Lambda())
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "acf-test-deployment")
    return calls


def test_start_writes_one_owner_bound_ttl_row_and_dispatches_once(table, owned_runtime, accepted_lambda):
    before = int(time.time())
    response = _client().post(
        "/api/test-runtime-async",
        json={"runtimeId": RUNTIME, "input": "Find current AWS news", "sessionId": "session-1"},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "running"
    assert body["testId"].startswith("runtime-test-")
    assert len(table.puts) == len(accepted_lambda) == 1
    put = table.puts[0]
    assert put["ConditionExpression"] == "attribute_not_exists(deployment_id)"
    assert put["Item"]["user_id"] == OWNER
    assert put["Item"]["status"] == "queued"
    assert 0 < put["Item"]["ttl"] - before <= 86460
    invocation = accepted_lambda[0]
    assert invocation["FunctionName"] == "acf-test-deployment"
    assert invocation["InvocationType"] == "Event"
    event = json.loads(invocation["Payload"])
    assert event["_async_runtime_test"] is True
    assert event["caller_sub"] == OWNER
    assert "Authorization" not in invocation["Payload"].decode()


def test_start_refuses_another_tenant_before_row_or_lambda_side_effect(monkeypatch, table, accepted_lambda):
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda _table, _runtime: _record())
    monkeypatch.setattr(dh, "_get_state_store", lambda: type("Store", (), {"_table": object()})())
    response = _client(OTHER).post("/api/test-runtime-async", json={"runtimeId": RUNTIME, "input": "hi"})
    assert response.status_code == 404
    assert table.puts == []
    assert accepted_lambda == []


def test_start_refuses_obo_without_persisting_a_bearer(monkeypatch, table, accepted_lambda):
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda _table, _runtime: _record())
    monkeypatch.setattr(dh, "_get_state_store", lambda: type("Store", (), {"_table": object()})())
    monkeypatch.setattr(dh, "deployment_requires_obo", lambda _state: True)
    response = _client().post("/api/test-runtime-async", json={"runtimeId": RUNTIME, "input": "hi"})
    assert response.status_code == 409
    assert table.puts == []
    assert accepted_lambda == []


def test_oversized_event_is_refused_before_row_or_dispatch(table, owned_runtime, accepted_lambda):
    history = [{"role": "user", "content": "x" * 3000} for _ in range(50)]
    response = _client().post(
        "/api/test-runtime-async",
        json={"runtimeId": RUNTIME, "input": "hi", "history": history},
    )
    assert response.status_code == 413
    assert table.puts == []
    assert accepted_lambda == []


def test_poll_uses_a_consistent_read_and_returns_the_owner_result(monkeypatch, table):
    table.item = {
        "deployment_id": TEST_ID,
        "status": "completed",
        "user_id": OWNER,
        "result_json": json.dumps(
            {
                "success": True,
                "response": "answer",
                "sessionId": "session-1",
                "toolReceipts": [{"name": "AmazonWebSearch___WebSearch", "status": "success"}],
            }
        ),
    }
    response = _client().get(f"/api/test-runtime-async/{TEST_ID}")
    assert response.status_code == 200, response.text
    assert response.json()["toolReceipts"][0]["name"] == "AmazonWebSearch___WebSearch"
    assert table.gets == [{"Key": {"deployment_id": TEST_ID}, "ConsistentRead": True}]


def test_poll_refuses_another_tenant_like_an_absent_row(monkeypatch, table):
    table.item = {"deployment_id": TEST_ID, "status": "completed", "user_id": OWNER, "result_json": "{}"}
    forbidden = _client(OTHER).get(f"/api/test-runtime-async/{TEST_ID}")
    table.item = None
    absent = _client(OTHER).get(f"/api/test-runtime-async/{TEST_ID}")
    assert forbidden.status_code == absent.status_code == 404
    assert forbidden.json() == absent.json()


def test_delete_is_owner_fenced(table):
    table.item = {"deployment_id": TEST_ID, "status": "completed", "user_id": OWNER}
    response = _client().delete(f"/api/test-runtime-async/{TEST_ID}")
    assert response.status_code == 200
    request = table.deletes[0]
    assert request["ConditionExpression"] == "#u = :u AND #s = :completed"
    assert request["ExpressionAttributeValues"][":u"] == OWNER
    assert request["ExpressionAttributeValues"][":completed"] == "completed"


def test_delete_refuses_a_still_running_owner_row(table):
    table.item = {"deployment_id": TEST_ID, "status": "invoking", "user_id": OWNER}
    response = _client().delete(f"/api/test-runtime-async/{TEST_ID}")
    assert response.status_code == 409
    assert table.deletes == []


def test_delete_hides_a_cross_tenant_row(table):
    table.item = {"deployment_id": TEST_ID, "status": "completed", "user_id": OWNER}
    response = _client(OTHER).delete(f"/api/test-runtime-async/{TEST_ID}")
    assert response.status_code == 404
    assert table.deletes == []


def test_delete_reports_a_completed_row_race_without_reclassifying_it_as_absent(table):
    table.item = {"deployment_id": TEST_ID, "status": "completed", "user_id": OWNER}
    table.delete_error = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "changed"}},
        "DeleteItem",
    )
    response = _client().delete(f"/api/test-runtime-async/{TEST_ID}")
    assert response.status_code == 409


def test_worker_revalidates_then_uses_the_bounded_long_timeout(monkeypatch, table):
    invocation: dict = {}
    settled: list[tuple[object, bool]] = []
    reservation = object()
    monkeypatch.setattr(dh, "_require_async_runtime_deployment", lambda runtime_id, caller_sub: _record())
    monkeypatch.setattr(dh, "_deployment_target_event", lambda state, region: {"target_region": region})
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: object())
    monkeypatch.setattr(dh, "reserve_invocation_budget", lambda **kwargs: reservation)
    monkeypatch.setattr(
        dh, "settle_invocation_budget", lambda value, *, dispatched: settled.append((value, dispatched))
    )

    def invoke(request, **kwargs):
        invocation.update(kwargs)
        kwargs["on_dispatch"]()
        return RuntimeTestResponse(
            success=True,
            response="answer",
            sessionId="session-1",
            toolReceipts=[{"name": "AmazonWebSearch___WebSearch", "status": "success"}],
        )

    monkeypatch.setattr(dh, "invoke_verified_http_runtime", invoke)
    result = dh._handle_async_runtime_test(
        {
            "_async_runtime_test": True,
            "caller_sub": OWNER,
            "request": {"runtimeId": RUNTIME, "input": "search", "sessionId": "session-1"},
            "test_id": TEST_ID,
        }
    )
    assert result == {
        "completed": True,
        "resultPersisted": True,
        "success": True,
        "testId": TEST_ID,
    }
    assert invocation["read_timeout_seconds"] == 540
    assert settled == [(reservation, True)]
    claim, update = table.updates
    assert claim["ConditionExpression"] == "#u = :u AND #s = :queued"
    assert claim["ExpressionAttributeValues"][":invoking"] == "invoking"
    assert update["ConditionExpression"] == "#u = :u AND #s = :invoking"
    stored = json.loads(update["ExpressionAttributeValues"][":result"])
    assert stored["toolReceipts"][0]["name"] == "AmazonWebSearch___WebSearch"


def test_duplicate_worker_delivery_never_invokes(monkeypatch, table):
    table.update_errors = [
        ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "already claimed"}},
            "UpdateItem",
        )
    ]
    invoked: list[bool] = []
    monkeypatch.setattr(dh, "invoke_verified_http_runtime", lambda *args, **kwargs: invoked.append(True))
    result = dh._handle_async_runtime_test(
        {
            "_async_runtime_test": True,
            "caller_sub": OWNER,
            "request": {"runtimeId": RUNTIME, "input": "search"},
            "test_id": TEST_ID,
        }
    )
    assert result == {"completed": False, "duplicateDelivery": True, "testId": TEST_ID}
    assert invoked == []


def test_result_store_failure_returns_successfully_instead_of_replaying(monkeypatch, table):
    table.update_errors = [None, RuntimeError("dynamodb unavailable after dispatch")]
    invocation_count = 0
    monkeypatch.setattr(dh, "_require_async_runtime_deployment", lambda runtime_id, caller_sub: _record())
    monkeypatch.setattr(dh, "_deployment_target_event", lambda state, region: {"target_region": region})
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: object())
    monkeypatch.setattr(dh, "reserve_invocation_budget", lambda **kwargs: object())
    monkeypatch.setattr(dh, "settle_invocation_budget", lambda value, *, dispatched: None)

    def invoke(request, **kwargs):
        nonlocal invocation_count
        invocation_count += 1
        kwargs["on_dispatch"]()
        return RuntimeTestResponse(success=True, response="answer")

    monkeypatch.setattr(dh, "invoke_verified_http_runtime", invoke)
    result = dh._handle_async_runtime_test(
        {
            "_async_runtime_test": True,
            "caller_sub": OWNER,
            "request": {"runtimeId": RUNTIME, "input": "search"},
            "test_id": TEST_ID,
        }
    )
    assert result == {"completed": False, "resultPersisted": False, "testId": TEST_ID}
    assert invocation_count == 1


def test_handler_dispatches_the_async_runtime_sentinel(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(dh, "_handle_async_runtime_test", lambda event: calls.append(event) or {"ok": True})
    event = {"_async_runtime_test": True, "test_id": TEST_ID}
    assert dh.handler(event, None) == {"ok": True}
    assert calls == [event]


@pytest.mark.parametrize("value", [0, -1, 541, True, 1.5])
def test_runtime_invocation_timeout_override_is_bounded(value):
    with pytest.raises(ValueError, match="read_timeout_seconds"):
        runtime_invocation.invoke_verified_http_runtime(
            RuntimeTestRequest(runtimeId=RUNTIME, input="hi"),
            caller_sub=OWNER,
            deployment_state={},
            runtime_id=RUNTIME,
            runtime_arn="arn",
            region="eu-west-1",
            target_session=object(),
            promote_policy=lambda state, region: True,
            invoke_harness=lambda *args, **kwargs: {},
            resolve_memory_identity=lambda *args: None,
            gateway_session=lambda session: nullcontext(),
            get_gateway_token=lambda info: "token",
            read_timeout_seconds=value,
        )
